"""Run a bare-metal C program on the Vortex-V SoC (ADR 0011).

sw/hello is compiled with riscv64-unknown-elf-gcc and loaded into the SoC RAM.
The test decodes the SoC's UART line (8N1) and checks what the program prints:
a greeting, a device-ordering check, an int8 dot product computed on the Xmlacc unit and checked by the
program against plain C, and the cycle counter. It also checks the LEDs and the
tohost pass flag.

sw/capcmp runs on SoCs built with the capture/compare timer (CAPCMP=1, ADR 0017):
the bench drives crank and cam edges, the firmware captures them under the
external interrupt and schedules injector and coil outputs in hardware, and the
bench checks each capture's tick and each output edge to the exact timer tick.

Run (from the repo root):  python3 -m pytest verif/soc -q
Needs riscv64-unknown-elf-gcc (scripts/setup-toolchain.sh); skipped without it
unless CI is set.
"""
import os
import shutil
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "sw"))
sys.path.insert(0, str(ROOT / "verif" / "core"))

CLKS_PER_BIT = 4          # fast UART for simulation
MAX_CYCLES = 60_000
EXPECTED_DOT = 0x2e8     # sum of a[i]*b[i] in sw/hello/hello.c (744)

# SoC builds: the in-order core, the default OoO core, and every OoO feature on
SOC_CONFIGS = {
    "soc_inorder": {"CORE_OOO": 0},
    "soc_ooo": {"CORE_OOO": 1},
    "soc_ooo_full": {"CORE_OOO": 1, "WIDTH": 2, "SPEC_BRANCHES": 1, "LSQ": 1, "XM_LATENCY": 3},
    # block-RAM-style memory (ADR 0012): what the Arty A7 top builds
    "soc_ooo_sync": {"CORE_OOO": 1, "WIDTH": 2, "SPEC_BRANCHES": 1, "LSQ": 1, "XM_LATENCY": 3,
                     "SYNC_MEM": 1, "XM_PORTS": 2},
}

try:
    import cocotb
    from cocotb.clock import Clock
    from cocotb.triggers import FallingEdge, RisingEdge
except ImportError:  # pragma: no cover
    cocotb = None

# CAPCMP=1 builds for sw/capcmp; a short prescaler keeps the simulation short
CC_PRESC = 4
CAPCMP_CONFIGS = {
    "soc_inorder_cc": {"CORE_OOO": 0, "CAPCMP": 1, "CAP_PRESC": CC_PRESC},
    "soc_ooo_cc": {"CORE_OOO": 1, "CAPCMP": 1, "CAP_PRESC": CC_PRESC},
    "soc_ooo_sync_cc": dict(SOC_CONFIGS["soc_ooo_sync"], CAPCMP=1, CAP_PRESC=CC_PRESC),
}
# Must match sw/capcmp/capcmp.c
DWELL_AT, INJ_AT, INJ_LEN, SPARK_AT, LATE_OUT, LATE_LEN = 120, 150, 40, 200, 3, 10
CC_EDGES = [0, 0, 1, 0, 0]          # capture channel of each edge: crank, crank, cam, crank, crank
CC_EDGE_GAP = 2000                  # cycles between edges (> SPARK_AT ticks)
CC_MAX_CYCLES = 60_000

if cocotb is not None:
    async def _uart_rx(dut, text):
        """Decode the UART line (8N1, CLKS_PER_BIT) into text, forever."""
        while True:
            await FallingEdge(dut.clk)
            if int(dut.uart_tx.value) != 0:
                continue
            byte = 0
            for _ in range(CLKS_PER_BIT // 2):           # to the middle of the start bit
                await FallingEdge(dut.clk)
            for bit in range(9):
                for _ in range(CLKS_PER_BIT):
                    await FallingEdge(dut.clk)
                if bit < 8:
                    byte |= int(dut.uart_tx.value) << bit
                else:
                    assert int(dut.uart_tx.value) == 1, "UART framing error"
            text.append(byte)

    @cocotb.test()
    async def soc_capcmp(dut):
        """Crank/cam captures under the external interrupt; outputs on the exact tick."""
        cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
        dut.cap_in.value = 0
        dut.rst_n.value = 0
        for _ in range(3):
            await RisingEdge(dut.clk)
        await FallingEdge(dut.clk)
        dut.rst_n.value = 1
        cc = dut.g_capcmp.u_capcmp
        text = bytearray()
        cocotb.start_soon(_uart_rx(dut, text))

        cyc = 0
        changes = []                                     # (output, level, tick)
        pushes = []                                      # tick of each CMP_PUSH write
        last_out = 0

        async def clock():
            nonlocal cyc, last_out
            if int(cc.wr_push.value):
                pushes.append(int(cc.time_q.value))
            await FallingEdge(dut.clk)
            cyc += 1
            out = int(dut.cmp_out.value)
            for o in range(8):
                if (out ^ last_out) >> o & 1:
                    changes.append((o, out >> o & 1, int(cc.time_q.value)))
            last_out = out

        while int(dut.leds.value) != 1:
            assert cyc < CC_MAX_CYCLES, f"firmware never got ready; UART {text.decode()!r}"
            await clock()
        late = changes[:]
        changes.clear()

        cap = 0
        driven = []                                      # (channel, expected tick)
        for ch in CC_EDGES:
            cap |= 1 << ch
            dut.cap_in.value = cap
            await clock()
            await clock()                                # two synchroniser flops
            driven.append((ch, int(cc.time_q.value)))    # the tick latched at the next edge
            for _ in range(50):
                await clock()
            cap &= ~(1 << ch)
            dut.cap_in.value = cap
            for _ in range(CC_EDGE_GAP - 52):
                await clock()
        while int(dut.done.value) == 0:
            assert cyc < CC_MAX_CYCLES, f"no tohost store; UART {text.decode()!r}"
            await clock()
        for _ in range(12 * CLKS_PER_BIT):               # the last UART frame
            await clock()

        out = text.decode("ascii", errors="replace")
        dut._log.info("UART output:\n%s", out)
        lines = out.splitlines()
        assert int(getattr(dut, "pass").value) == 1, f"program reported failure; UART: {out!r}"
        assert lines[0] == "capcmp" and lines[-1] == " capcmp ok", lines
        t0 = int(lines[1].split()[1], 16)
        caps = [int(line.split()[1], 16) for line in lines[2:-1]]
        assert caps == [(1 << 63) | ((ch + 1) << 32) | ts for ch, ts in driven], \
            ([hex(c) for c in caps], driven)
        # The late pulse: armed 5 ticks in the past, it fires in the clock after the push
        # (the output then rises within a tick of the push) and lasts its full length
        (o0, l0, r), (o1, l1, f) = late
        assert (o0, l0, o1, l1) == (LATE_OUT, 1, LATE_OUT, 0) and f - r == LATE_LEN, late
        assert t0 <= pushes[0] and r - pushes[0] in (0, 1), (t0, pushes[0], late)
        expect = []
        for i, ts in enumerate(ts for ch, ts in driven if ch == 0):
            inj, coil = i % 4, 4 + i % 4
            expect += [(coil, 1, ts + DWELL_AT), (inj, 1, ts + INJ_AT),
                       (inj, 0, ts + INJ_AT + INJ_LEN), (coil, 0, ts + SPARK_AT)]
        assert sorted(changes, key=lambda c: (c[2], c[0])) == sorted(expect, key=lambda c: (c[2], c[0])), \
            (changes, expect)

    @cocotb.test()
    async def soc_hello(dut):
        """Reset, run until tohost, decode the UART text."""
        cocotb.start_soon(Clock(dut.clk, 10, units="ns").start())
        dut.rst_n.value = 0
        for _ in range(3):
            await RisingEdge(dut.clk)
        await FallingEdge(dut.clk)
        dut.rst_n.value = 1

        text = bytearray()
        start = None          # cycle the current frame's start bit was first seen
        byte = 0
        cyc = 0
        done_at = None
        while cyc < MAX_CYCLES:
            await RisingEdge(dut.clk)
            await FallingEdge(dut.clk)
            cyc += 1
            line = int(dut.uart_tx.value)
            if start is None:
                if line == 0:
                    start, byte = cyc, 0
            else:
                t = cyc - start
                bit, phase = divmod(t, CLKS_PER_BIT)
                if phase == CLKS_PER_BIT // 2:
                    if bit == 0:
                        assert line == 0, f"glitch on the start bit at cycle {cyc}"
                    elif 1 <= bit <= 8:
                        byte |= line << (bit - 1)
                    elif bit == 9:
                        assert line == 1, f"framing error (stop bit low) at cycle {cyc}"
                        text.append(byte)
                        start = None
            if done_at is None and int(dut.done.value) == 1:
                done_at = cyc
            # stop once the program is done and the last frame has been sent
            if done_at is not None and start is None and cyc > done_at + 12 * CLKS_PER_BIT:
                break
        out = text.decode("ascii", errors="replace")
        dut._log.info("UART output:\n%s", out)
        assert done_at is not None, f"no tohost store within {MAX_CYCLES} cycles; UART: {out!r}"
        assert int(getattr(dut, "pass").value) == 1, \
            f"program reported failure; UART: {out!r}"
        lines = out.splitlines()
        assert lines[0] == "Vortex-V SoC: hello", lines
        assert lines[1] == ". mmio order ok", lines
        assert lines[2] == f"dot32 = {EXPECTED_DOT:016x} xmlacc ok", lines
        assert lines[3] == " csr ok", lines
        assert lines[4] == " irq ok", lines
        assert lines[5].startswith("cycles = ") and int(lines[5][9:], 16) > 0, lines
        assert int(dut.leds.value) == 0xA


def _require_gcc():
    if shutil.which("riscv64-unknown-elf-gcc") is None:
        if os.environ.get("CI"):
            pytest.fail("riscv64-unknown-elf-gcc is required in CI")
        pytest.skip("riscv64-unknown-elf-gcc not installed")


def _run_program(program, config, params, testcase):
    _require_gcc()
    from build import build
    from cores import rtl_sources
    try:
        from cocotb_tools.runner import get_runner
    except ImportError:  # older cocotb
        from cocotb.runner import get_runner

    build_dir = HERE / "sim_build" / config
    hexf = build(program, build_dir / "sw")
    soc_srcs = [str(ROOT / line.strip()) for line in (ROOT / "rtl/soc/files.f").read_text()
                .splitlines() if line.strip() and not line.strip().startswith("//")]
    params = dict(params, CLKS_PER_BIT=CLKS_PER_BIT, INIT_FILE=f'"{hexf}"')
    os.environ["PYTHONPATH"] = os.pathsep.join([str(HERE), os.environ.get("PYTHONPATH", "")])
    runner = get_runner(os.getenv("SIM", "icarus"))
    runner.build(sources=rtl_sources() + soc_srcs, hdl_toplevel="vortex_soc",
                 build_dir=str(build_dir), parameters=params, always=True,
                 timescale=("1ns", "1ps"))
    runner.test(hdl_toplevel="vortex_soc", test_module="test_soc", test_dir=str(HERE),
                build_dir=str(build_dir), timescale=("1ns", "1ps"), testcase=testcase)


@pytest.mark.parametrize("config", list(SOC_CONFIGS))
def test_soc_runs_hello(config):
    _run_program("hello", config, SOC_CONFIGS[config], "soc_hello")


@pytest.mark.parametrize("config", list(CAPCMP_CONFIGS))
def test_soc_capcmp_schedules_outputs(config):
    _run_program("capcmp", config, CAPCMP_CONFIGS[config], "soc_capcmp")
