"""Misaligned load, store and jump-target traps (ADR 0016), on every core configuration.

Every program runs under lockstep co-sim against the ISS, with the data region
compared at the end, and the tests check the recorded mcause/mepc/mtval against
values computed here.

Programs:
  * access - misaligned lh/lhu/lw/lwu/ld/sh/sw/sd at several offsets trap with
             mcause 4 (load) or 6 (store) and mtval = the address; the load's rd
             and the stored-to memory stay unchanged. Aligned and byte accesses
             in between do not trap.
  * jumps  - jal and a taken branch with an offset of 2 mod 4, and jalr to a
             target 2 mod 4 (also after bit 0 is cleared), trap with mcause 0,
             mepc = the jump, mtval = the target, and no link write; a not-taken
             branch with such an offset does not trap; jalr to target + 1 lands on
             the target.
  * flush  - a misaligned load whose address waits on a divide, with a long run
             of younger loads, stores, multiplies, Xmlacc ops and branches behind
             it: the out-of-order core has them in flight when the load faults,
             and must discard all of them (no store lands, no register changes).
             Alternate iterations are aligned and run normally.
  * wrong_path - misaligned loads and jalrs on the not-taken path of an untrained
             always-taken branch, so the speculating cores execute them
             speculatively. Nothing may trap.

Run (from the repo root):  python3 -m pytest verif/core/test_misaligned.py -q
"""
from pathlib import Path

import pytest

try:
    import cocotb
except ImportError:  # pragma: no cover - cocotb is a hard dependency of verif/
    cocotb = None

from cores import CORES, build_and_test
from rvasm import Asm, DATA_BASE, DATA_SIZE, MASK64, RESET_PC, TOHOST_ADDR

CSR_MTVEC, CSR_MEPC, CSR_MCAUSE, CSR_MTVAL = 0x305, 0x341, 0x342, 0x343
CAUSE_FETCH, CAUSE_LOAD, CAUSE_STORE = 0, 4, 6
MARK = 0x5A
REC = DATA_BASE + 0x800        # the handler appends (mcause, mepc, mtval) here
REC_BYTES = 24
PATTERN = [(DATA_BASE + 8 * i, 8, 0x0102030405060708 * (i + 1)) for i in range(16)]
FLUSH_LOOPS = 6

# (op, offset from DATA_BASE): every one is misaligned for its size
MIS_ACCESSES = [("lh", 1), ("lhu", 3), ("lw", 2), ("lwu", 5), ("ld", 4), ("ld", 7),
                ("lw", 1), ("sh", 1), ("sw", 6), ("sd", 4), ("sd", 1), ("sw", 3)]
OK_ACCESSES = [("lh", 2), ("lw", 4), ("ld", 8), ("lb", 1), ("lbu", 7), ("sb", 0x23),
               ("sh", 0x2A), ("sw", 0x2C)]   # stores away from the misaligned ones


def _handler(a):
    """Record the trap and return to the instruction after it."""
    a.label("handler")
    a.csrr(20, CSR_MCAUSE); a.sd(20, 30, 0)
    a.csrr(21, CSR_MEPC);   a.sd(21, 30, 8)
    a.csrr(22, CSR_MTVAL);  a.sd(22, 30, 16)
    a.addi(30, 30, REC_BYTES)
    a.addi(21, 21, 4)
    a.csrw(CSR_MEPC, 21)
    a.mret()


def _prologue(a):
    a.la_label(9, "handler")
    a.csrw(CSR_MTVEC, 9)
    a.la(30, REC)
    a.la(1, DATA_BASE)


def _end(a):
    a.jal(0, "end")
    _handler(a)
    a.label("end")
    a.la(29, TOHOST_ADDR)
    a.addi(28, 0, 1)
    a.sw(28, 29, 0)


def access_program():
    a = Asm()
    _prologue(a)
    a.addi(4, 0, MARK)
    a.addi(3, 0, -1)                           # store data: all ones
    expect = []
    oks = iter(OK_ACCESSES)
    for k, (op, off) in enumerate(MIS_ACCESSES):
        a.label(f"mis{k}")
        if op.startswith("l"):
            getattr(a, op)(4, 1, off)          # x4 must keep MARK
            expect.append((CAUSE_LOAD, f"mis{k}", DATA_BASE + off))
        else:
            getattr(a, op)(3, 1, off)
            expect.append((CAUSE_STORE, f"mis{k}", DATA_BASE + off))
        ok = next(oks, None)
        if ok:
            op2, off2 = ok
            if op2.startswith("l"):
                getattr(a, op2)(5, 1, off2)
            else:
                getattr(a, op2)(3, 1, off2)
    a.sd(4, 1, 0x100)                          # x4 still MARK
    _end(a)
    a.assemble()
    return a.assemble(), [(c, a.labels[lbl], t) for c, lbl, t in expect]


def jumps_program():
    a = Asm()
    _prologue(a)
    a.addi(5, 0, MARK)
    a.addi(7, 0, MARK)
    expect = []
    a.label("j0"); a.jal_off(5, 6)                       # jal x5, +6
    expect.append((CAUSE_FETCH, "j0", ("j0", 6)))
    # After a trap, execution resumes at the next instruction, which then dispatches
    # in slot 0: a nop there puts each following jump in slot 1 of a pair.
    a.nop()
    a.label("j1"); a.beq_off(0, 0, 10)                   # taken, +10
    expect.append((CAUSE_FETCH, "j1", ("j1", 10)))
    a.bne_off(0, 0, 6)                                   # not taken: no trap
    a.la_label(6, "tgt")
    a.addi(6, 6, 2)
    a.label("j2"); a.jalr(7, 6, 0)                       # tgt + 2 (slot 0)
    expect.append((CAUSE_FETCH, "j2", ("tgt", 2)))
    a.nop()
    a.label("j3"); a.jalr(7, 6, 1)                       # tgt + 3 -> tgt + 2
    expect.append((CAUSE_FETCH, "j3", ("tgt", 2)))
    a.nop()
    a.label("j4"); a.jal_off(5, -2)                      # backwards, -2
    expect.append((CAUSE_FETCH, "j4", ("j4", -2)))
    a.jalr(7, 6, -1)                                     # tgt + 1 -> tgt: fine
    a.addi(5, 0, 1)                                      # skipped
    a.label("tgt")
    a.sd(5, 1, 0x100)                                    # x5 still MARK
    a.sd(7, 1, 0x108)                                    # the last jalr's link
    _end(a)
    a.assemble()
    L = a.labels
    return a.assemble(), [(c, L[lbl], L[t[0]] + t[1]) for c, lbl, t in expect], L


def flush_program():
    a = Asm()
    _prologue(a)
    a.addi(10, 0, 3)
    a.addi(11, 0, 1)
    a.addi(16, 0, 0)                           # k
    a.addi(17, 0, FLUSH_LOOPS)
    a.label("loop")
    a.andi(12, 16, 1)                          # odd k: offset 3 (misaligned), even: 0
    a.mul(12, 12, 10)
    a.div(2, 12, 11)                           # slow: the load's address waits on it
    a.add(8, 1, 2)
    a.label("victim")
    a.lw(4, 8, 0)                              # faults on odd k
    # Younger work the out-of-order core runs ahead on; all discarded on a fault
    a.addi(13, 16, 100)
    a.sd(13, 1, 0x200)
    a.ld(14, 1, 8)
    a.mul(15, 14, 13)
    a.dot8(18, 14, 15)
    a.beq(13, 0, "never")
    a.sd(15, 1, 0x208)
    a.addi(19, 19, 1)
    a.macc(18, 14, 15, 18)
    a.sd(18, 1, 0x210)
    a.label("never")
    a.sd(4, 1, 0x218)
    a.addi(16, 16, 1)
    a.blt(16, 17, "loop")
    a.sd(19, 1, 0x220)
    _end(a)
    a.assemble()
    return a.assemble(), a.labels["victim"]


def wrong_path_program():
    a = Asm()
    _prologue(a)
    a.la_label(6, "after")
    a.addi(6, 6, 2)                            # misaligned jalr target
    a.addi(16, 0, 0)
    a.addi(17, 0, 8)
    a.label("loop")
    a.beq(0, 0, "after")                       # always taken; predicted not taken at first
    a.lw(4, 1, 1)                              # wrong path only
    a.jalr(0, 6, 0)                            # wrong path only
    a.sd(4, 1, 2)
    a.label("after")
    a.addi(16, 16, 1)
    a.blt(16, 17, "loop")
    _end(a)
    return a.assemble()


if cocotb is not None:
    from harness import cosim_lockstep, final_regs

    async def _run(dut, name, words):
        retires, state, iss = await cosim_lockstep(dut, name, words, data=PATTERN,
                                                   max_cycles=20000,
                                                   mem_check=(DATA_BASE, DATA_SIZE))
        return final_regs(retires), iss

    def _mem64(iss, addr):
        return iss.mem.load(addr, 8) & MASK64

    def _records(iss):
        n = (iss.x[30] - REC) // REC_BYTES
        return [tuple(_mem64(iss, REC + REC_BYTES * k + 8 * j) for j in range(3))
                for k in range(n)]

    @cocotb.test()
    async def access(dut):
        words, expect = access_program()
        _, iss = await _run(dut, "access", words)
        assert _records(iss) == expect, (_records(iss), expect)
        assert _mem64(iss, DATA_BASE + 0x100) == MARK
        for k in range(2):                     # the misaligned stores hit only these words
            assert _mem64(iss, DATA_BASE + 8 * k) == PATTERN[k][2], "a misaligned store wrote"

    @cocotb.test()
    async def jumps(dut):
        words, expect, labels = jumps_program()
        _, iss = await _run(dut, "jumps", words)
        assert _records(iss) == expect, ([tuple(map(hex, r)) for r in _records(iss)],
                                         [tuple(map(hex, r)) for r in expect])
        assert _mem64(iss, DATA_BASE + 0x100) == MARK, "a faulting jal wrote its link"
        assert _mem64(iss, DATA_BASE + 0x108) == labels["tgt"] - 4   # jalr at tgt - 8

    @cocotb.test()
    async def flush(dut):
        words, victim = flush_program()
        _, iss = await _run(dut, "flush", words)
        faults = FLUSH_LOOPS // 2
        assert _records(iss) == [(CAUSE_LOAD, victim, DATA_BASE + 3)] * faults
        assert _mem64(iss, DATA_BASE + 0x220) == FLUSH_LOOPS   # every younger op ran once

    @cocotb.test()
    async def wrong_path(dut):
        _, iss = await _run(dut, "wrong_path", wrong_path_program())
        assert _records(iss) == [], _records(iss)
        assert _mem64(iss, DATA_BASE) == PATTERN[0][2]


@pytest.mark.parametrize("toplevel", CORES)
def test_misaligned_traps(toplevel):
    build_and_test(toplevel, "test_misaligned", Path(__file__).parent)
