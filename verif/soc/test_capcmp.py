"""Unit test of the capture/compare timer, rtl/soc/vortex_capcmp.sv (ADR 0017).

Random register traffic and capture-pin toggles drive the RTL and the reference
model in verif/soc/capcmp_model.py side by side; every cycle the register read,
the compare outputs and the interrupt must agree. Small builds (a 12-bit timer, a
4-entry FIFO, 3 slots, single-channel corners) reach timer wrap, a full FIFO and
busy slots quickly. Directed tests then check the rules Redline relies on against
hand-worked cycle numbers rather than the model: a compare already in the past
fires at once, a pulse lasts exactly its length, a full FIFO drops and counts.

Run (from the repo root):  python3 -m pytest verif/soc/test_capcmp.py -q
"""
import json
import os
import random
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))

from capcmp_model import (ACT_CLEAR, ACT_PULSE, ACT_SET, CTRL_IE_BIT, R_CAP_DATA, R_CAP_OVF,  # noqa: E402
                          R_CAP_RD, R_CAP_WR, R_CMP_ARG, R_CMP_FLUSH, R_CMP_PUSH, R_CMP_STATUS,
                          R_CTRL, R_OUT, R_PRESC, R_TIME, CapCmp)

ALL_REGS = [R_TIME, R_PRESC, R_CTRL, R_CAP_WR, R_CAP_RD, R_CAP_DATA, R_CAP_OVF, R_CMP_ARG,
            R_CMP_PUSH, R_CMP_STATUS, R_OUT, R_CMP_FLUSH, 0x60, 0x78]

# name -> RTL parameters (the model takes the same values)
CONFIGS = {
    "default": {},
    "small_wrap": {"TW": 12, "N_CAP": 3, "CAP_DEPTH": 4, "N_OUT": 5, "N_CMP": 3, "PRESC_RST": 2},
    "single": {"TW": 8, "N_CAP": 1, "CAP_DEPTH": 2, "N_OUT": 1, "N_CMP": 1, "PRESC_RST": 0},
}
RANDOM_CYCLES = 60_000

try:
    import cocotb
    from cocotb.clock import Clock
    from cocotb.triggers import FallingEdge, ReadOnly, RisingEdge
except ImportError:  # pragma: no cover
    cocotb = None


def _model():
    p = json.loads(os.environ.get("CAPCMP_PARAMS", "{}"))
    return CapCmp(tw=p.get("TW", 32), n_cap=p.get("N_CAP", 2), depth=p.get("CAP_DEPTH", 32),
                  n_out=p.get("N_OUT", 8), n_cmp=p.get("N_CMP", 8), presc=p.get("PRESC_RST", 49))


if cocotb is not None:
    class Bench:
        """Drives the DUT on the falling edge, checks before the rising edge, steps the model."""

        def __init__(self, dut):
            self.dut, self.m, self.cyc = dut, _model(), 0

        async def reset(self):
            dut = self.dut
            cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
            dut.rst_n.value = 0
            dut.we.value = 0
            dut.addr.value = 0
            dut.wdata.value = 0
            dut.cap_in.value = 0
            for _ in range(2):
                await RisingEdge(dut.clk)
            await FallingEdge(dut.clk)
            dut.rst_n.value = 1

        async def cycle(self, we=0, addr=R_TIME, wdata=0, cap_in=None):
            """One clock: drive, compare the combinational outputs, clock both."""
            dut, m = self.dut, self.m
            if cap_in is None:
                cap_in = m.sync1          # hold the pins
            dut.we.value, dut.addr.value, dut.wdata.value, dut.cap_in.value = we, addr, wdata, cap_in
            await ReadOnly()
            ctx = f"cycle {self.cyc} we={we} addr={addr:#x} wdata={wdata:#x}"
            got = int(dut.rdata.value)
            assert got == m.read(addr), f"{ctx}: rdata {got:#x}, model {m.read(addr):#x}"
            assert int(dut.cmp_out.value) == m.outputs(), \
                f"{ctx}: cmp_out {int(dut.cmp_out.value):#x}, model {m.outputs():#x}"
            assert int(dut.irq.value) == m.irq(), f"{ctx}: irq {int(dut.irq.value)}, model {m.irq()}"
            await RisingEdge(dut.clk)
            m.step(we, addr, wdata, cap_in)
            await FallingEdge(dut.clk)
            self.cyc += 1
            return got

        async def read(self, addr):
            return await self.cycle(addr=addr)

        async def write(self, addr, wdata):
            await self.cycle(we=1, addr=addr, wdata=wdata)

        async def idle(self, n):
            for _ in range(n):
                await self.cycle()

        def push_word(self, tick, out, act):
            return (act << 40) | (out << 32) | (tick & self.m.tmask)

    def _random_write(rng, m):
        """A register write biased towards traffic that makes things happen."""
        r = rng.random()
        if r < 0.30:
            tick = (m.time + rng.randint(-8, 40)) & m.tmask
            if rng.random() < 0.05:
                tick = rng.getrandbits(m.tw)               # anywhere, including far past
            out = rng.randrange(m.n_out + 1) if rng.random() < 0.1 else rng.randrange(m.n_out)
            act = rng.choice([ACT_PULSE, ACT_SET, ACT_CLEAR, ACT_PULSE, 0, rng.randrange(8)])
            return R_CMP_PUSH, (act << 40) | (out << 32) | tick | (rng.getrandbits(16) << 48)
        if r < 0.42:
            arg = rng.choice([0, 1, 2, rng.randint(0, 30), rng.getrandbits(m.tw)])
            return R_CMP_ARG, arg | (rng.getrandbits(8) << 40)
        if r < 0.62:
            step = rng.choice([1, 1, 1, 2, rng.randint(0, m.depth + 2), rng.getrandbits(m.tw)])
            return R_CAP_RD, (m.cap_rd + step) & m.tmask
        if r < 0.68:
            ctrl = rng.getrandbits(2 * m.n_cap) | (rng.getrandbits(1) << CTRL_IE_BIT) | (rng.getrandbits(8) << 20)
            return R_CTRL, ctrl
        if r < 0.72:
            return R_PRESC, rng.choice([0, 0, 1, 2, 3, rng.getrandbits(16)]) | (rng.getrandbits(4) << 16)
        if r < 0.78:
            return R_OUT, rng.getrandbits(32)
        if r < 0.82:
            return R_CAP_OVF, rng.getrandbits(8)
        if r < 0.86:
            return R_CMP_STATUS, 0
        if r < 0.89:
            return R_CMP_FLUSH, 0
        return rng.choice(ALL_REGS), rng.getrandbits(64)

    @cocotb.test()
    async def capcmp_random(dut):
        """Random traffic: RTL and model agree on every read, output and irq."""
        b = Bench(dut)
        await b.reset()
        rng = random.Random(0xCA9C)
        m = b.m
        # Keep the timer fast: a long prescaler would leave compares idle for the whole run
        await b.write(R_PRESC, 1)
        pin_rate = 0.15
        cap = 0
        captures = ovf_seen = out_changes = 0
        for i in range(int(os.environ.get("CAPCMP_CYCLES", RANDOM_CYCLES))):
            if i % 2000 == 0:
                pin_rate = rng.choice([0.02, 0.15, 0.5])   # bursts that overrun the FIFO, and quiet
            wr0, outs0 = m.cap_wr, m.outputs()
            for c in range(m.n_cap):
                if rng.random() < pin_rate:
                    cap ^= 1 << c
            if rng.random() < 0.35:
                addr, wdata = _random_write(rng, m)
                await b.cycle(we=1, addr=addr, wdata=wdata, cap_in=cap)
            else:
                await b.cycle(addr=rng.choice(ALL_REGS), cap_in=cap)
            captures += m.cap_wr != wr0
            ovf_seen += m.cap_ovf != 0
            out_changes += m.outputs() != outs0
        dut._log.info("captures=%d cycles with overflow=%d output changes=%d", captures, ovf_seen, out_changes)
        # Coverage: the run must have exercised capture, overflow and the outputs
        assert captures > 100 and ovf_seen > 0 and out_changes > 100

    @cocotb.test()
    async def capcmp_past_due_fires_at_once(dut):
        """A compare at a tick already passed drives its output on the next clock."""
        b = Bench(dut)
        await b.reset()
        await b.write(R_PRESC, 0)                          # one tick per clock
        await b.idle(20)
        now = await b.read(R_TIME)
        await b.write(R_CMP_PUSH, b.push_word(now - 5, 0, ACT_SET))
        assert await b.read(R_CMP_STATUS) & 0xFF == 1     # pending this clock
        assert int(dut.cmp_out.value) & 1 == 1             # fired: registered output is high
        assert await b.read(R_CMP_STATUS) & 0xFF == 0     # and the slot is free again

    @cocotb.test()
    async def capcmp_pulse_length(dut):
        """A pulse of length L at tick T is high for exactly L ticks, from the edge that ends tick T."""
        b = Bench(dut)
        await b.reset()
        await b.write(R_PRESC, 0)
        now = await b.read(R_TIME)
        start, length = now + 10, 7
        await b.write(R_CMP_ARG, length)
        await b.write(R_CMP_PUSH, b.push_word(start, 0, ACT_PULSE))
        high = []
        for _ in range(40):
            t = await b.read(R_TIME)       # TIME before this edge
            if int(dut.cmp_out.value) & 1:  # output after it
                high.append(t)
        # the slot fires in the clock where TIME == start and the output rises at that edge;
        # it falls at the edge of the clock where TIME == start + length
        assert high == list(range(start, start + length)), high

    @cocotb.test()
    async def capcmp_full_fifo_drops(dut):
        """Edges past a full FIFO are dropped and counted; the oldest entries survive."""
        b = Bench(dut)
        await b.reset()
        m = b.m
        await b.write(R_PRESC, 0)
        cap = 0
        for _ in range(m.depth + 3):
            cap ^= 1                                       # rising edge every other clock
            await b.cycle(cap_in=cap)
            cap ^= 1
            await b.cycle(cap_in=cap)
        await b.idle(4)
        assert await b.read(R_CAP_WR) == m.depth
        assert await b.read(R_CAP_OVF) == 3
        first = await b.read(R_CAP_DATA)
        assert first >> 63 == 1 and (first >> 32) & 0xFF == 1
        await b.write(R_CAP_RD, 1000)                      # past CAP_WR: pops all
        assert await b.read(R_CAP_RD) == m.depth
        assert await b.read(R_CAP_DATA) == 0
        await b.write(R_CAP_OVF, 0)
        assert await b.read(R_CAP_OVF) == 0


def _params(config):
    p = dict(CONFIGS[config])
    return p


@pytest.mark.parametrize("config", list(CONFIGS))
def test_capcmp_matches_model(config):
    try:
        from cocotb_tools.runner import get_runner
    except ImportError:  # older cocotb
        from cocotb.runner import get_runner
    build_dir = HERE / "sim_build" / f"capcmp_{config}"
    params = _params(config)
    os.environ["PYTHONPATH"] = os.pathsep.join([str(HERE), os.environ.get("PYTHONPATH", "")])
    runner = get_runner(os.getenv("SIM", "icarus"))
    runner.build(sources=[str(ROOT / "rtl/soc/vortex_capcmp.sv")], hdl_toplevel="vortex_capcmp",
                 build_dir=str(build_dir), parameters=params, always=True, timescale=("1ns", "1ps"))
    runner.test(hdl_toplevel="vortex_capcmp", test_module="test_capcmp", test_dir=str(HERE),
                build_dir=str(build_dir), timescale=("1ns", "1ps"),
                extra_env={"CAPCMP_PARAMS": json.dumps(params)})
