"""Machine timer (ADR 0015) and external (ADR 0017) interrupts, on every core configuration.

The core harness models MTIMECMP at MTIMECMP_ADDR and drives the core's
irq_timer input high while the cycle count >= it, as the SoC does; irq_ext
follows bit 0 of the last store to EXTIRQ_ADDR, standing in for the SoC's
capture/compare timer. Every
program runs under lockstep co-sim: each interrupt the RTL takes is taken in
the ISS at the same point, which must find it enabled (mstatus.MIE and
mie.MTIE), and every later retire must still match. The tests add explicit
checks of the values the privileged spec fixes.

Programs:
  * entry - mip.MTIP follows the timer; nothing is taken while mstatus.MIE or
            mie.MTIE is clear; once both are set the interrupt is taken before
            the next instruction with mcause = 2^63 + 7, mepc = that
            instruction, mtval = 0, MPIE = 1, MIE = 0; mret returns and sets MIE.
  * race  - the timer is armed -4..107 cycles from an mcycle read before a csrci that
            clears mstatus.MIE, behind a store, a divide, a load, a branch, a
            multiply and a loop, so the interrupt arrives at every point around
            them and the disable. It may be taken before the csrci but never
            after it; a store it preempts must not have happened (the handler
            reads that location), and the handler's own multiply must not get
            the preempted divide's result.
  * ext   - mip.MEIP follows irq_ext; nothing is taken while mie.MEIE is clear;
            with both interrupts pending the external one goes first
            (mcause = 2^63 + 11), then the timer; mie keeps only MTIE and MEIE.

Random programs with interrupts every 48-111 cycles: verif/crv
(test_random_cosim_irq).

Run (from the repo root):  python3 -m pytest verif/core/test_irq.py -q
"""
from pathlib import Path

import pytest

try:
    import cocotb
except ImportError:  # pragma: no cover - cocotb is a hard dependency of verif/
    cocotb = None

from cores import CORES, build_and_test
from rvasm import Asm, DATA_BASE, DATA_SIZE, EXTIRQ_ADDR, MASK64, MTIMECMP_ADDR, TOHOST_ADDR

CSR_MSTATUS, CSR_MIE, CSR_MTVEC = 0x300, 0x304, 0x305
CSR_MEPC, CSR_MCAUSE, CSR_MTVAL, CSR_MIP = 0x341, 0x342, 0x343, 0x344
CSR_MCYCLE = 0xB00
MSTATUS_MPP_M = 0x1800
MIE, MPIE = 1 << 3, 1 << 7
MTIE = MTIP = 1 << 7
CAUSE_M_TIMER_IRQ = (1 << 63) | 7
MEIE = MEIP = 1 << 11
CAUSE_M_EXT_IRQ = (1 << 63) | 11
R_EXT = 27                      # EXTIRQ_ADDR pointer
RACE_STEPS = 112
RACE_EARLY = 4                  # armings already in the past: taken at the next boundary

R_CMP, R_DATA = 31, 30          # MTIMECMP pointer, data pointer (handler appends here)


def _end(a):
    a.la(29, TOHOST_ADDR)
    a.addi(28, 0, 1)
    a.sw(28, 29, 0)


def _disarm(a, tmp):
    """MTIMECMP = all ones, then wait until mip.MTIP reads clear (it is registered)."""
    a.addi(tmp, 0, -1)
    a.sd(tmp, R_CMP, 0)
    lbl = f"disarm{len(a.items)}"
    a.label(lbl)
    a.csrr(tmp, CSR_MIP)
    a.bne(tmp, 0, lbl)


def entry_program():
    a = Asm()
    a.la(R_DATA, DATA_BASE)
    a.la(R_CMP, MTIMECMP_ADDR)
    a.la_label(1, "handler")
    a.csrw(CSR_MTVEC, 1)
    a.addi(1, 0, 0x55)
    a.csrw(CSR_MTVAL, 1)                      # the interrupt must clear it
    a.addi(1, 0, MTIE)
    a.csrw(CSR_MIE, 1)                        # MTIE on, MIE still off
    a.sd(0, R_CMP, 0)                         # MTIMECMP = 0: timer fires now
    a.label("poll")
    a.csrr(5, CSR_MIP)                        # wait for MTIP
    a.beq(5, 0, "poll")
    a.div(6, 1, 1)                            # a few instructions with it pending
    a.csrr(6, CSR_MCAUSE)                     # 0: nothing taken with MIE off
    a.csrc(CSR_MIE, 1)                        # MTIE off
    a.csrrsi(0, CSR_MSTATUS, MIE)             # MIE on: still nothing (MTIE off)
    a.ld(2, R_DATA, 0)
    a.mul(2, 2, 2)
    a.csrr(7, CSR_MCAUSE)                     # 0
    a.csrs(CSR_MIE, 1)                        # MTIE on: taken before "after"
    a.label("after")
    a.addi(8, 0, 1)
    a.csrr(9, CSR_MSTATUS)                    # mret set MIE again
    a.csrr(10, CSR_MIP)                       # handler disarmed: 0
    a.csrrci(0, CSR_MSTATUS, MIE)
    for i, r in enumerate((5, 6, 7, 8, 9, 10)):
        a.sd(r, R_DATA, 8 * (8 + i))
    a.jal(0, "done")
    a.label("handler")                        # record, disarm, return
    a.csrr(20, CSR_MCAUSE); a.sd(20, R_DATA, 0)
    a.csrr(21, CSR_MEPC);   a.sd(21, R_DATA, 8)
    a.csrr(22, CSR_MTVAL);  a.sd(22, R_DATA, 16)
    a.csrr(23, CSR_MSTATUS); a.sd(23, R_DATA, 24)
    a.csrr(24, CSR_MIP);    a.sd(24, R_DATA, 32)
    _disarm(a, 25)
    a.mret()
    a.label("done")
    _end(a)
    return a.assemble(), a.labels["after"]


def race_program():
    a = Asm()
    a.la(R_DATA, DATA_BASE)
    a.la(R_CMP, MTIMECMP_ADDR)
    a.la_label(1, "handler")
    a.csrw(CSR_MTVEC, 1)
    a.addi(1, 0, MTIE)
    a.csrw(CSR_MIE, 1)
    a.addi(10, 0, -RACE_EARLY)                # k = arming delay; the first few are already due
    a.addi(11, 0, RACE_STEPS - RACE_EARLY)
    a.addi(12, 0, 7)
    a.label("loop")
    a.csrrsi(0, CSR_MSTATUS, MIE)
    a.csrr(2, CSR_MCYCLE)
    a.add(2, 2, 10)
    a.sd(2, R_CMP, 0)                         # fires about k cycles from the mcycle read
    a.sd(10, R_DATA, 24)                      # the handler reads it: a store the interrupt
    a.div(3, 2, 12)                           # preempts must not happen early; and a divide
    a.ld(4, R_DATA, 0)                        # the interrupt may wait for, but not split
    a.bne(4, 0, "skip")                       # never taken
    a.addi(4, 4, 1)
    a.label("skip")
    a.mul(4, 3, 12)
    a.addi(7, 0, 6)                           # a short loop: on the speculating cores the
    a.label("spin")                           # csrci must not already be dispatched when
    a.addi(7, 7, -1)                          # an early interrupt arrives
    a.bne(7, 0, "spin")
    a.csrrci(0, CSR_MSTATUS, MIE)             # nothing may be taken after this
    a.addi(5, 0, 1)
    a.sd(5, R_DATA, 8)
    _disarm(a, 6)
    a.addi(10, 10, 1)
    a.blt(10, 11, "loop")
    a.jal(0, "done")
    a.label("handler")                        # count, disarm, return
    a.mul(22, 12, 12)                         # 49, whatever the divide unit was doing
    a.ld(23, R_DATA, 24)                      # k of the last store that ran
    a.add(22, 22, 23)
    a.sd(22, R_DATA, 32)
    a.ld(20, R_DATA, 16)
    a.addi(20, 20, 1)
    a.sd(20, R_DATA, 16)
    _disarm(a, 21)
    a.mret()
    a.label("done")
    _end(a)
    return a.assemble()


def ext_program():
    a = Asm()
    a.la(R_DATA, DATA_BASE)
    a.la(R_CMP, MTIMECMP_ADDR)
    a.la(R_EXT, EXTIRQ_ADDR)
    a.la_label(1, "handler")
    a.csrw(CSR_MTVEC, 1)
    a.addi(15, 0, 0)                          # handler log index (bytes)
    a.addi(1, 0, -1)
    a.csrw(CSR_MIE, 1)                        # all ones: only MTIE and MEIE stick
    a.csrr(5, CSR_MIE)
    a.csrw(CSR_MIE, 0)
    a.addi(1, 0, 1)
    a.sd(1, R_EXT, 0)                         # raise irq_ext
    a.label("poll")
    a.csrr(6, CSR_MIP)                        # wait for MEIP
    a.beq(6, 0, "poll")
    a.csrrsi(0, CSR_MSTATUS, MIE)             # MIE on, MEIE off: nothing taken
    a.div(7, 1, 1)
    a.csrr(7, CSR_MCAUSE)                     # 0
    a.addi(2, 0, MTIE)
    a.csrw(CSR_MIE, 2)                        # MTIE on, timer not armed: nothing taken
    a.ld(3, R_DATA, 0)
    a.csrrci(0, CSR_MSTATUS, MIE)
    a.sd(0, R_CMP, 0)                         # arm the timer: both pending, MIE off
    a.label("poll2")
    a.csrr(8, CSR_MIP)
    a.srli(9, 8, 7)
    a.andi(9, 9, 1)
    a.beq(9, 0, "poll2")
    a.addi(2, 0, 1)
    a.slli(2, 2, 11)
    a.ori(2, 2, MTIE)
    a.csrw(CSR_MIE, 2)                        # MEIE | MTIE
    a.csrrsi(0, CSR_MSTATUS, MIE)             # external first, then the timer
    a.label("after")
    a.addi(10, 0, 1)
    a.csrrci(0, CSR_MSTATUS, MIE)
    for i, r in enumerate((5, 6, 7, 8, 10, 15)):
        a.sd(r, R_DATA, 8 * (8 + i))
    a.jal(0, "done")
    a.label("handler")                        # log mcause and mepc, clear the source, return
    a.csrr(20, CSR_MCAUSE)
    a.add(21, R_DATA, 15)
    a.sd(20, 21, 128)
    a.csrr(22, CSR_MEPC)
    a.sd(22, 21, 136)
    a.addi(15, 15, 16)
    a.blt(20, 0, "is_irq")
    a.jal(0, "done")                          # an exception: the log shows it
    a.label("is_irq")
    a.andi(20, 20, 0xFF)
    a.addi(23, 0, 11)
    a.bne(20, 23, "timer")
    a.sd(0, R_EXT, 0)                         # drop irq_ext and wait for MEIP to clear
    a.label("ext_wait")
    a.csrr(24, CSR_MIP)
    a.srli(24, 24, 11)
    a.bne(24, 0, "ext_wait")
    a.mret()
    a.label("timer")
    _disarm(a, 25)
    a.mret()
    a.label("done")
    _end(a)
    return a.assemble(), a.labels["after"]


if cocotb is not None:
    from harness import cosim_lockstep

    async def _run(dut, name, words):
        retires, state, iss = await cosim_lockstep(dut, name, words, max_cycles=20000,
                                                   mem_check=(DATA_BASE, DATA_SIZE))
        return state, iss

    def _mem64(iss, addr):
        return iss.mem.load(addr, 8) & MASK64

    @cocotb.test()
    async def entry(dut):
        words, after = entry_program()
        state, iss = await _run(dut, "entry", words)
        assert len(state["irqs"]) == 1, f"{len(state['irqs'])} interrupts taken, expected 1"
        got = [_mem64(iss, DATA_BASE + 8 * i) for i in range(5)]
        assert got == [CAUSE_M_TIMER_IRQ, after, 0, MSTATUS_MPP_M | MPIE, MTIP], \
            [hex(v) for v in got]
        polled, cause0, cause1, marker, status, mip = (
            _mem64(iss, DATA_BASE + 8 * (8 + i)) for i in range(6))
        assert polled == MTIP and cause0 == 0 and cause1 == 0 and marker == 1 and mip == 0
        assert status == MSTATUS_MPP_M | MPIE | MIE, f"mstatus after mret {status:#x}"

    @cocotb.test()
    async def race(dut):
        state, iss = await _run(dut, "race", race_program())
        taken = _mem64(iss, DATA_BASE + 16)
        assert taken == len(state["irqs"]), f"handler ran {taken}, RTL took {len(state['irqs'])}"
        # Early delays always land before the disable; late ones never arrive in time.
        assert 0 < taken < RACE_STEPS, f"{taken} of {RACE_STEPS} armings were taken"
        dut._log.info("race: %d of %d armings taken before the disable", taken, RACE_STEPS)


    @cocotb.test()
    async def ext(dut):
        words, after = ext_program()
        state, iss = await _run(dut, "ext", words)
        assert len(state["irqs"]) == 2, f"{len(state['irqs'])} interrupts taken, expected 2"
        mie_all, polled, cause0, mip_both, marker, logged = (
            _mem64(iss, DATA_BASE + 8 * (8 + i)) for i in range(6))
        assert mie_all == MTIE | MEIE, f"mie reads {mie_all:#x}"
        assert polled == MEIP and cause0 == 0 and mip_both == MEIP | MTIP and marker == 1
        assert logged == 32, f"handler ran {logged // 16} times"
        log = [_mem64(iss, DATA_BASE + 128 + 8 * i) for i in range(4)]
        # the external interrupt first, at "after"; the timer straight after its mret
        assert log == [CAUSE_M_EXT_IRQ, after, CAUSE_M_TIMER_IRQ, after], [hex(v) for v in log]


@pytest.mark.parametrize("toplevel", CORES)
def test_timer_interrupt(toplevel):
    build_and_test(toplevel, "test_irq", Path(__file__).parent)
