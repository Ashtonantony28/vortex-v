"""
test_core.py — cocotb directed tests + co-sim for the Vortex-V M1 RV64I core.

Run with:   python3 -m pytest verif/core/ -q   (from the repo root)

The pytest entry point (`test_vortex_core`) uses the cocotb runner to build the
RTL with Icarus Verilog and run every @cocotb.test() in this module. The core is
driven directly by cocotb (no SystemVerilog TB wrapper): this file contains a
cocotb-driven memory model that serves `imem_rdata` combinationally, handles the
`dmem` port, and watches stores to TOHOST_ADDR to end each run.

Microarchitecture under test: single-cycle datapath with a registered retire
interface — exactly one instruction commits per clock, surfaced on the dbg_*
ports. The oracle rebuilds architectural register state from the dbg retire
stream and checks it against hand-computed expected values, plus the final
memory image and the tohost PASS code. One optional test co-simulates against the
Python ISS at model/iss/rv64i.py if it exists (import is guarded).
"""

from pathlib import Path

import cocotb
import pytest

from cores import CORES, build_and_test

from rvasm import (Asm, DATA_BASE, DATA_SIZE, MASK64, OPC_CUSTOM0, OPC_OP, OPC_OPW,
                   RESET_PC, TOHOST_ADDR)
from harness import _rd_bytes, cosim_lockstep, final_regs, run_program, sext



# ===========================================================================
# Directed tests
# ===========================================================================
@cocotb.test()
async def arith_sequence(dut):
    """Arithmetic/logical/shift + *W + LUI, store result, halt PASS."""
    a = Asm()
    a.addi(1, 0, 10)          # x1 = 10
    a.addi(2, 0, 20)          # x2 = 20
    a.add(3, 1, 2)            # x3 = 30
    a.sub(4, 3, 1)            # x4 = 20
    a.slli(5, 1, 3)           # x5 = 80
    a.xori(6, 5, 15)          # x6 = 95
    a.addiw(7, 1, -1)         # x7 = 9
    a.sltiu(8, 0, 1)          # x8 = 1
    a.la(30, DATA_BASE)              # x30 = 0x80002000
    a.sw(6, 30, 0)                   # mem[0x80002000] = 95
    a.la(31, TOHOST_ADDR)            # x31 = 0x80001000
    a.addi(9, 0, 1)
    a.sw(9, 31, 0)                   # tohost = 1 -> halt
    retires, mem, state = await run_program(dut, a.assemble())

    assert state["tohost"] == 1, f"tohost={state['tohost']!r} (expected 1 PASS)"
    regs = final_regs(retires)
    assert regs[3] == 30, regs[3]
    assert regs[4] == 20, regs[4]
    assert regs[5] == 80, regs[5]
    assert regs[6] == 95, regs[6]
    assert regs[7] == 9, regs[7]
    assert regs[8] == 1, regs[8]
    assert _rd_bytes(mem, DATA_BASE, 4) == 95, _rd_bytes(mem, DATA_BASE, 4)
    # first retired instruction must be at RESET_PC and write x1=10
    assert retires[0]["pc"] == RESET_PC
    assert retires[0]["rd"] == 1 and retires[0]["data"] == 10
    dut._log.info("arith_sequence: %d instructions retired, PASS", len(retires))


@cocotb.test()
async def sum_loop_branch(dut):
    """Sum 1..10 with a taken BLT backward branch; expect 55."""
    a = Asm()
    a.addi(1, 0, 0)           # sum = 0
    a.addi(2, 0, 1)           # i = 1
    a.addi(3, 0, 11)          # N+1 = 11
    a.label("loop")
    a.add(1, 1, 2)            # sum += i
    a.addi(2, 2, 1)           # i += 1
    a.blt(2, 3, "loop")       # if i < 11 goto loop
    a.la(30, DATA_BASE)
    a.sw(1, 30, 0)                   # store 55
    a.la(31, TOHOST_ADDR)
    a.addi(9, 0, 1)
    a.sw(9, 31, 0)                   # halt PASS
    retires, mem, state = await run_program(dut, a.assemble())

    assert state["tohost"] == 1, state["tohost"]
    regs = final_regs(retires)
    assert regs[1] == 55, f"sum={regs[1]} (expected 55)"
    assert _rd_bytes(mem, DATA_BASE, 4) == 55
    # branch taken 9 times, not taken once -> loop body runs 10 times
    assert regs[2] == 11, regs[2]
    dut._log.info("sum_loop_branch: sum=%d over %d retires, PASS", regs[1], len(retires))


@cocotb.test()
async def load_store_roundtrip(dut):
    """SD/LD round trip + byte store with signed/unsigned load extension."""
    a = Asm()
    a.la(30, DATA_BASE)       # base
    a.addi(1, 0, 0x123)              # x1 = 291
    a.sd(1, 30, 0)                   # mem[base] = 291 (8 bytes)
    a.ld(3, 30, 0)                   # x3 = 291
    a.addi(4, 0, -1)                 # x4 = 0xFFFF..FF
    a.sb(4, 30, 8)                   # mem[base+8] = 0xFF
    a.lbu(5, 30, 8)                  # x5 = 255
    a.lb(6, 30, 8)                   # x6 = -1 (sign extended)
    a.sw(3, 30, 16)                  # mem[base+16] = 291
    a.la(31, TOHOST_ADDR)
    a.addi(9, 0, 1)
    a.sw(9, 31, 0)                   # halt PASS
    retires, mem, state = await run_program(dut, a.assemble())

    assert state["tohost"] == 1, state["tohost"]
    regs = final_regs(retires)
    assert regs[3] == 0x123, hex(regs[3])
    assert regs[5] == 255, regs[5]
    assert regs[6] == (-1 & MASK64), hex(regs[6])
    assert _rd_bytes(mem, DATA_BASE, 8) == 0x123
    assert _rd_bytes(mem, DATA_BASE + 8, 1) == 0xFF
    assert _rd_bytes(mem, DATA_BASE + 16, 4) == 0x123
    dut._log.info("load_store_roundtrip: PASS (%d retires)", len(retires))


@cocotb.test()
async def jal_jalr_call(dut):
    """JAL to a leaf, JALR return; verify link register and control flow."""
    a = Asm()
    a.jal(1, "func")                 # x1 = return addr (pc+4)
    a.label("after")
    a.addi(5, 0, 99)                 # executed after return (sentinel)
    a.la(30, DATA_BASE)
    a.sw(10, 30, 0)                  # store func result (7)
    a.sw(5, 30, 8)                   # store sentinel (99)
    a.la(31, TOHOST_ADDR)
    a.addi(9, 0, 1)
    a.sw(9, 31, 0)                   # halt PASS (program ends here)
    a.label("func")
    a.addi(10, 0, 7)                 # x10 = 7
    a.jalr(0, 1, 0)                  # return to x1 (rd=x0 discards link)
    words = a.assemble()
    retires, mem, state = await run_program(dut, words)

    assert state["tohost"] == 1, state["tohost"]
    regs = final_regs(retires)
    after_pc = a.labels["after"]
    assert regs[1] == after_pc, (hex(regs[1]), hex(after_pc))   # JAL link = addr after call
    assert regs[10] == 7, regs[10]
    assert regs[5] == 99, regs[5]
    assert _rd_bytes(mem, DATA_BASE, 4) == 7
    assert _rd_bytes(mem, DATA_BASE + 8, 4) == 99
    # JALR with rd=x0 must not report a writeback
    assert all(not (r["wbv"] and r["rd"] == 0) for r in retires)
    dut._log.info("jal_jalr_call: link=%s PASS", hex(regs[1]))


@cocotb.test()
async def wordops_and_shifts(dut):
    """RV64 *W sign-extension semantics and shift behavior."""
    a = Asm()
    # ADDW overflow out of 32 bits sign-extends: 0x7FFFFFFF + 1 = 0x80000000 -> sext = 0xFFFFFFFF80000000
    a.lui(1, 0x80000)                # x1 = 0xFFFFFFFF80000000 (lui sign-extends in RV64)
    a.addiw(2, 0, -1)                # x2 = -1 (0xFFFF..FF)
    a.addw(3, 1, 2)                  # (0x80000000 + 0xFFFFFFFF) low32 = 0x7FFFFFFF -> x3=0x7FFFFFFF
    a.slliw(4, 2, 4)                 # (-1)<<4 low32 = 0xFFFFFFF0 -> sext = 0xFFFFFFFFFFFFFFF0
    a.srliw(5, 1, 4)                 # 0x80000000>>4 = 0x08000000 (zero-extended word) -> 0x08000000
    a.sraiw(6, 1, 4)                 # 0x80000000 >>> 4 = 0xF8000000 -> sext 0xFFFFFFFFF8000000
    a.la(30, DATA_BASE)
    a.sd(3, 30, 0)
    a.la(31, TOHOST_ADDR)
    a.addi(9, 0, 1)
    a.sw(9, 31, 0)
    retires, mem, state = await run_program(dut, a.assemble())

    assert state["tohost"] == 1, state["tohost"]
    regs = final_regs(retires)
    assert regs[1] == 0xFFFF_FFFF_8000_0000, hex(regs[1])   # LUI sign-extends in RV64
    assert regs[3] == 0x7FFF_FFFF, hex(regs[3])
    assert regs[4] == 0xFFFF_FFFF_FFFF_FFF0, hex(regs[4])
    assert regs[5] == 0x0800_0000, hex(regs[5])
    assert regs[6] == 0xFFFF_FFFF_F800_0000, hex(regs[6])
    dut._log.info("wordops_and_shifts: PASS")


@cocotb.test()
async def muldiv_ops(dut):
    """RV64M multiply/divide directed test.

    Exercises MUL/MULH/MULHU/DIV/REM/DIVU/REMU plus a *W op and the two
    divide-by-zero forms, with negative operands. Also checks the multi-cycle
    stall contract: every instruction (incl. each multi-cycle divide) produces
    exactly ONE retire pulse — i.e. no duplicate retire PCs.
    """
    a = Asm()
    a.addi(1, 0, 6)           # x1 = 6
    a.addi(2, 0, 7)           # x2 = 7
    a.mul(3, 1, 2)            # x3 = 42
    a.addi(4, 0, -7)          # x4 = -7
    a.addi(5, 0, 2)           # x5 = 2
    a.div(6, 4, 5)            # x6 = -3  (round toward zero)
    a.rem(7, 4, 5)            # x7 = -1  (sign of dividend)
    a.addi(8, 0, 20)          # x8 = 20
    a.addi(9, 0, 6)           # x9 = 6
    a.divu(10, 8, 9)          # x10 = 3
    a.remu(11, 8, 9)          # x11 = 2
    a.div(12, 1, 0)           # x12 = 6/0  -> all ones
    a.rem(13, 1, 0)           # x13 = 6%0  -> dividend 6
    a.addi(14, 0, -1)         # x14 = -1
    a.mulh(15, 14, 14)        # x15 = high64(-1 * -1 = 1) = 0
    a.mulhu(16, 14, 14)       # x16 = high64((2^64-1)^2) = 0xFFFF..FE
    a.mulw(17, 4, 5)          # x17 = (-7*2) in 32 bits = -14 -> sext
    a.la(30, DATA_BASE)
    a.sd(3, 30, 0)                   # store 42
    a.la(31, TOHOST_ADDR)
    a.addi(28, 0, 1)
    a.sw(28, 31, 0)                  # halt PASS
    retires, mem, state = await run_program(dut, a.assemble(), max_cycles=4000)

    assert state["tohost"] == 1, state["tohost"]
    regs = final_regs(retires)
    assert regs[3] == 42, regs[3]
    assert regs[6] == (-3 & MASK64), hex(regs[6])
    assert regs[7] == (-1 & MASK64), hex(regs[7])
    assert regs[10] == 3, regs[10]
    assert regs[11] == 2, regs[11]
    assert regs[12] == MASK64, hex(regs[12])
    assert regs[13] == 6, regs[13]
    assert regs[15] == 0, hex(regs[15])
    assert regs[16] == MASK64 - 1, hex(regs[16])
    assert regs[17] == (-14 & MASK64), hex(regs[17])
    assert _rd_bytes(mem, DATA_BASE, 8) == 42

    # Retire contract: each instruction retires exactly once (no double-pulse
    # while the divider stalls). In this straight-line program every PC is unique.
    pcs = [r["pc"] for r in retires]
    assert len(pcs) == len(set(pcs)), "duplicate retire PC => stall/retire contract broken"
    dut._log.info("muldiv_ops: %d instructions retired, PASS", len(retires))


# ===========================================================================
# Xmlacc Tier-1 directed tests (custom ML extension, opcode CUSTOM0)
# ===========================================================================
def _pack8(lanes):
    """Pack 8 int8 lanes (signed or unsigned) into a 64-bit register value."""
    assert len(lanes) == 8
    u = 0
    for i, v in enumerate(lanes):
        u |= (int(v) & 0xFF) << (8 * i)
    return u


def _s8(x):
    x &= 0xFF
    return x - 256 if x >= 128 else x


def _ref_dot(a, b, a_signed, b_signed):
    """Spec reference dot product over 8 lanes (pure Python int)."""
    acc = 0
    for i in range(8):
        la = _s8(a[i]) if a_signed else (a[i] & 0xFF)
        lb = _s8(b[i]) if b_signed else (b[i] & 0xFF)
        acc += la * lb
    return acc


def _ref_rqs(x, sh):
    """Spec reference requantize: returns (int8_result, sat)."""
    x = x & MASK64
    xs = x - (1 << 64) if x >= (1 << 63) else x
    rnd = (1 << (sh - 1)) if sh > 0 else 0
    t = (xs + rnd) >> sh
    if t < -128:
        return -128, True
    if t > 127:
        return 127, True
    return t, False


@cocotb.test()
async def xmlacc_dot8_directed(dut):
    """mlacc.dot8 signed*signed int8 dot product, sign-extended to 64."""
    la = [1, -2, 3, -4, 5, -6, 7, -8]
    lb = [8, 7, 6, 5, 4, 3, 2, 1]
    A, B = _pack8(la), _pack8(lb)
    a = Asm()
    a.la(30, DATA_BASE)
    a.ld(1, 30, 0); a.ld(2, 30, 8)
    a.dot8(3, 1, 2)
    a.sd(3, 30, 16)
    a.la(31, TOHOST_ADDR); a.addi(9, 0, 1); a.sw(9, 31, 0)
    data = [(DATA_BASE, 8, A), (DATA_BASE + 8, 8, B)]
    retires, mem, state = await run_program(dut, a.assemble(), data=data)
    assert state["tohost"] == 1, state["tohost"]
    exp = _ref_dot([v & 0xFF for v in la], [v & 0xFF for v in lb], True, True)
    assert sext(final_regs(retires)[3], 64) == exp, (final_regs(retires)[3], exp)
    assert sext(_rd_bytes(mem, DATA_BASE + 16, 8), 64) == exp
    dut._log.info("xmlacc_dot8_directed: dot=%d PASS", exp)


@cocotb.test()
async def xmlacc_dot8u_dot8su_directed(dut):
    """mlacc.dot8u (unsigned*unsigned) and mlacc.dot8su (signed*unsigned)."""
    la = [250, 10, 20, 200, 5, 6, 7, 255]
    lb = [255, 1, 2, 100, 40, 3, 2, 255]
    A, B = _pack8(la), _pack8(lb)
    a = Asm()
    a.la(30, DATA_BASE)
    a.ld(1, 30, 0); a.ld(2, 30, 8)
    a.dot8u(3, 1, 2)
    a.dot8su(4, 1, 2)   # rs1 signed, rs2 unsigned
    a.la(31, TOHOST_ADDR); a.addi(9, 0, 1); a.sw(9, 31, 0)
    data = [(DATA_BASE, 8, A), (DATA_BASE + 8, 8, B)]
    retires, _mem, state = await run_program(dut, a.assemble(), data=data)
    assert state["tohost"] == 1, state["tohost"]
    regs = final_regs(retires)
    exp_u = _ref_dot(la, lb, False, False)
    exp_su = _ref_dot(la, lb, True, False)
    assert regs[3] == (exp_u & MASK64), (regs[3], exp_u)
    assert sext(regs[4], 64) == exp_su, (regs[4], exp_su)
    dut._log.info("xmlacc_dot8u/su: dot8u=%d dot8su=%d PASS", exp_u, exp_su)


@cocotb.test()
async def xmlacc_macc_accumulate(dut):
    """mlacc.macc: rd = rs3 + signed dot8(rs1,rs2) — the GEMM accumulate op.

    Also proves the R4 3rd-read-port path and the one-pulse-per-retire contract
    (each PC in this straight-line program appears exactly once).
    """
    la = [2, 2, 2, 2, 2, 2, 2, 2]
    lb = [3, 3, 3, 3, 3, 3, 3, 3]
    A, B = _pack8(la), _pack8(lb)   # dot = 8*6 = 48 per macc
    acc0 = 1000
    a = Asm()
    a.la(30, DATA_BASE)
    a.ld(1, 30, 0); a.ld(2, 30, 8); a.ld(4, 30, 16)
    a.macc(5, 1, 2, 4)      # 1000 + 48 = 1048
    a.macc(5, 1, 2, 5)      # 1048 + 48 = 1096  (accumulate chain into x5)
    a.macc(5, 1, 2, 5)      # 1096 + 48 = 1144
    a.sd(5, 30, 24)
    a.la(31, TOHOST_ADDR); a.addi(9, 0, 1); a.sw(9, 31, 0)
    data = [(DATA_BASE, 8, A), (DATA_BASE + 8, 8, B), (DATA_BASE + 16, 8, acc0)]
    retires, mem, state = await run_program(dut, a.assemble(), data=data)
    assert state["tohost"] == 1, state["tohost"]
    assert final_regs(retires)[5] == 1144, final_regs(retires)[5]
    assert _rd_bytes(mem, DATA_BASE + 24, 8) == 1144
    pcs = [r["pc"] for r in retires]
    assert len(pcs) == len(set(pcs)), "duplicate retire PC => macc broke retire contract"
    dut._log.info("xmlacc_macc_accumulate: acc=1144 over %d retires PASS", len(retires))


@cocotb.test()
async def xmlacc_rqs_round_sat_sticky(dut):
    """mlacc.rqs rounding, arithmetic shift, clamp, and the mlstat SAT sticky."""
    a = Asm()
    a.la(30, DATA_BASE)
    a.ld(1, 30, 0)          # x1 = big positive int32 -> saturates +127
    a.ld(2, 30, 8)          # x2 = big negative       -> saturates -128
    a.addi(5, 0, 4)         # shift amount = 4
    a.addi(6, 0, 1)         # shift amount = 1
    a.rqs(10, 1, 5)         # saturate high -> +127, SAT set
    a.rqs(11, 2, 5)         # saturate low  -> -128, SAT stays set
    a.addi(7, 0, 5)         # x7 = 5
    a.rqs(12, 7, 6)         # (5+1)>>1 = 3, no saturation (sticky stays 1)
    a.la(31, TOHOST_ADDR); a.addi(9, 0, 1); a.sw(9, 31, 0)
    data = [(DATA_BASE, 8, 100000 & MASK64), (DATA_BASE + 8, 8, (-100000) & MASK64)]
    retires, _mem, state = await run_program(dut, a.assemble(), data=data)
    assert state["tohost"] == 1, state["tohost"]
    regs = final_regs(retires)
    assert sext(regs[10], 64) == 127, regs[10]
    assert sext(regs[11], 64) == -128, regs[11]
    assert sext(regs[12], 64) == 3, regs[12]
    assert state["mlstat_sat"] == 1, "SAT sticky should be set after clamping"
    # cross-check against the pure-Python reference
    assert _ref_rqs(100000, 4) == (127, True)
    assert _ref_rqs((-100000) & MASK64, 4) == (-128, True)
    assert _ref_rqs(5, 1) == (3, False)
    dut._log.info("xmlacc_rqs_round_sat_sticky: SAT=%d PASS", state["mlstat_sat"])


@cocotb.test()
async def xmlacc_rqs_no_sat(dut):
    """A requantize that never clamps leaves mlstat.SAT clear."""
    a = Asm()
    a.addi(1, 0, 500)       # small value, shift 3 -> ~63, in int8 range
    a.addi(2, 0, 3)
    a.rqs(10, 1, 2)
    a.la(31, TOHOST_ADDR); a.addi(9, 0, 1); a.sw(9, 31, 0)
    retires, _mem, state = await run_program(dut, a.assemble())
    assert state["tohost"] == 1, state["tohost"]
    exp, sat = _ref_rqs(500, 3)
    assert sext(final_regs(retires)[10], 64) == exp, exp
    assert sat is False and state["mlstat_sat"] == 0, "no clamp -> SAT must stay 0"
    dut._log.info("xmlacc_rqs_no_sat: rqs=%d SAT=0 PASS", exp)


INT64_MAX = (1 << 63) - 1
INT64_MIN = 1 << 63          # as an unsigned 64-bit pattern


@cocotb.test()
async def xmlacc_rqs_round_overflow(dut):
    """rqs rounding must not overflow: rs1 + (1<<(sh-1)) needs 65 bits (KNOWN-ISSUES I5).

    With rs1 near INT64_MAX a 64-bit add wraps negative before the shift. The
    spec/golden model compute in unbounded precision, so e.g. INT64_MAX with sh=63
    must give 1 (not -1) and with sh=1 must saturate HIGH to +127 (not -128).
    """
    d = DATA_BASE
    data = [(d + 0, 8, INT64_MAX), (d + 8, 8, INT64_MIN)]
    cases_nosat = [(10, 0, 63), (11, 0, 62), (12, 8, 63)]   # (rd, data offset, sh)
    a = Asm()
    a.la(30, DATA_BASE)
    a.ld(1, 30, 0); a.ld(2, 30, 8)
    a.addi(5, 0, 63); a.addi(6, 0, 62)
    a.rqs(10, 1, 5)      # INT64_MAX, sh=63 -> 1
    a.rqs(11, 1, 6)      # INT64_MAX, sh=62 -> 2
    a.rqs(12, 2, 5)      # INT64_MIN, sh=63 -> -1
    a.la(31, TOHOST_ADDR); a.addi(9, 0, 1); a.sw(9, 31, 0)
    retires, _mem, state = await run_program(dut, a.assemble(), data=data)
    assert state["tohost"] == 1, state["tohost"]
    regs = final_regs(retires)
    srcs = {0: INT64_MAX, 8: INT64_MIN}
    for rd, off, sh in cases_nosat:
        exp, sat = _ref_rqs(srcs[off], sh)
        assert sat is False
        got = sext(regs[rd], 64)
        assert got == exp, "rqs(0x%016X, sh=%d): RTL=%d expected=%d" % (srcs[off], sh, got, exp)
    assert state["mlstat_sat"] == 0, "none of these clamp, SAT must stay 0"

    # Second program: INT64_MAX with sh=1 rounds to 2^62 and must clamp HIGH.
    a = Asm()
    a.la(30, DATA_BASE)
    a.ld(1, 30, 0)
    a.addi(5, 0, 1)
    a.rqs(10, 1, 5)      # INT64_MAX, sh=1 -> +127, SAT
    a.la(31, TOHOST_ADDR); a.addi(9, 0, 1); a.sw(9, 31, 0)
    retires, _mem, state = await run_program(dut, a.assemble(), data=data)
    assert state["tohost"] == 1, state["tohost"]
    exp, sat = _ref_rqs(INT64_MAX, 1)
    assert (exp, sat) == (127, True)
    got = sext(final_regs(retires)[10], 64)
    assert got == 127, "rqs(INT64_MAX, sh=1): RTL=%d expected=+127" % got
    assert state["mlstat_sat"] == 1, "clamp fired, SAT must be set"
    dut._log.info("xmlacc_rqs_round_overflow: 65-bit rounding correct PASS")


# ===========================================================================
# Optional co-simulation against the Python ISS (guarded)
# ===========================================================================
@cocotb.test()
async def mispredict_squashes_waiting_jalr(dut):
    """A mispredicted branch whose wrong path holds a JALR still waiting on a slow
    divide: the squash must drop the JALR and release the front end (ADR 0008)."""
    a = Asm()
    a.addi(1, 0, 4000)
    a.addi(2, 0, 3)
    a.div(5, 1, 2)                   # slow producer of the JALR's base register
    a.mul(10, 2, 0)                  # x10 = 0, a few cycles late: the wrong path
                                     # dispatches before the branch can resolve
    a.beq(10, 0, "target")           # always taken; a cold predictor says not-taken
    a.jalr(0, 5, 0)                  # wrong path: waits on x5, must be squashed
    a.addi(6, 0, 99)                 # wrong path
    a.label("target")
    a.addi(7, 0, 42)
    a.add(8, 5, 7)                   # waits for the divide on the right path
    a.la(31, 0x8000_1000)
    a.addi(9, 0, 1)
    a.sw(9, 31, 0)
    await cosim_lockstep(dut, "mispredict_jalr", a.assemble(), max_cycles=1000,
                         mem_check=(DATA_BASE, DATA_SIZE))


@cocotb.test()
async def cosim_against_iss(dut):
    """Lockstep co-sim of the RTL against the golden ISS (model/iss/rv64i.py).

    The ISS is part of this repo, so a failure to import or construct it is a
    test FAILURE, never a silent pass: this is the core correctness gate. Every
    program also compares the whole data region RTL vs ISS at the end.
    """

    # Co-simulate several representative programs: ALU/*W, a taken-branch loop,
    # a load/store round trip, and a JAL/JALR call. Each runs on a fresh Hart.
    def prog_arith():
        a = Asm()
        a.addi(1, 0, 10); a.addi(2, 0, 20); a.add(3, 1, 2); a.sub(4, 3, 1)
        a.slli(5, 1, 3); a.xori(6, 5, 15); a.addiw(7, 1, -1)
        a.la(30, DATA_BASE); a.sw(6, 30, 0)
        a.la(31, TOHOST_ADDR); a.addi(9, 0, 1); a.sw(9, 31, 0)
        return a

    def prog_loop():
        a = Asm()
        a.addi(1, 0, 0); a.addi(2, 0, 1); a.addi(3, 0, 11)
        a.label("loop"); a.add(1, 1, 2); a.addi(2, 2, 1); a.blt(2, 3, "loop")
        a.la(30, DATA_BASE); a.sw(1, 30, 0)
        a.la(31, TOHOST_ADDR); a.addi(9, 0, 1); a.sw(9, 31, 0)
        return a

    def prog_ldst():
        a = Asm()
        a.la(30, DATA_BASE); a.addi(1, 0, 0x123); a.sd(1, 30, 0); a.ld(3, 30, 0)
        a.addi(4, 0, -1); a.sb(4, 30, 8); a.lbu(5, 30, 8); a.lb(6, 30, 8)
        a.sw(3, 30, 16)
        a.la(31, TOHOST_ADDR); a.addi(9, 0, 1); a.sw(9, 31, 0)
        return a

    def prog_call():
        a = Asm()
        a.jal(1, "func"); a.label("after"); a.addi(5, 0, 99)
        a.la(30, DATA_BASE); a.sw(10, 30, 0); a.sw(5, 30, 8)
        a.la(31, TOHOST_ADDR); a.addi(9, 0, 1); a.sw(9, 31, 0)
        a.label("func"); a.addi(10, 0, 7); a.jalr(0, 1, 0)
        return a

    def prog_muldiv():
        # RV64M: multiply (incl. the three MULH* signedness variants), signed /
        # unsigned divide and remainder with negative operands, both divide-by-
        # zero forms, signed overflow (most-negative / -1), and the *W variants.
        # Every result is compared retire-by-retire against the (edge-case
        # verified) ISS, so this genuinely cross-checks the multi-cycle unit.
        a = Asm()
        a.addi(1, 0, -7)          # x1 = -7
        a.addi(2, 0, 3)           # x2 = 3
        a.addi(3, 0, -1)          # x3 = -1
        a.addi(4, 0, 0)           # x4 = 0
        a.mul(5, 1, 2)            # -21
        a.mulh(6, 1, 3)           # high64 signed(-7 * -1)
        a.mulhu(7, 3, 3)          # high64 unsigned((2^64-1)^2)
        a.mulhsu(8, 3, 2)         # high64 signed(-1) x unsigned(3)
        a.div(9, 1, 2)            # -7/3  = -2
        a.rem(10, 1, 2)           # -7%3  = -1
        a.divu(11, 1, 2)          # (2^64-7)/3
        a.remu(12, 1, 2)          # (2^64-7)%3
        a.div(13, 1, 4)           # /0 -> -1
        a.rem(14, 1, 4)           # %0 -> -7
        a.addi(15, 0, 1)
        a.slli(15, 15, 63)        # x15 = most-negative (0x8000...0)
        a.div(16, 15, 3)          # most-neg / -1 -> dividend (overflow)
        a.rem(17, 15, 3)          # -> 0
        a.mulw(18, 1, 2)          # (-7*3) low32 sext
        a.divw(19, 1, 2)          # -7/3 in 32 bits
        a.remw(20, 1, 2)          # -7%3 in 32 bits
        a.divuw(21, 1, 2)         # unsigned 32-bit
        a.remuw(22, 1, 2)
        a.la(30, DATA_BASE); a.sd(5, 30, 0)
        a.la(31, TOHOST_ADDR); a.addi(28, 0, 1); a.sw(28, 31, 0)
        return a

    def prog_xmlacc():
        # Xmlacc Tier-1: an int8 GEMM row (dot8 across a 3-tile row accumulated
        # with macc), the three dot signedness variants, and a requantize that
        # saturates (exercising the mlstat SAT sticky). Operands come from the
        # data region (like real activation/weight tiles). Returns (Asm, data).
        #
        # Row: W (weights) * X (activations) summed over three 8-lane tiles,
        # accumulated into x5 with macc; then dot8u/dot8su variants; then rqs.
        x0t = _pack8([1, -2, 3, -4, 5, -6, 7, -8])
        w0t = _pack8([2, 2, 2, 2, 2, 2, 2, 2])
        x1t = _pack8([10, 20, -30, 40, -50, 60, -70, 80])
        w1t = _pack8([1, -1, 1, -1, 1, -1, 1, -1])
        x2t = _pack8([100, -100, 50, -50, 25, -25, 12, -12])
        w2t = _pack8([3, 3, 3, 3, 3, 3, 3, 3])
        xu = _pack8([250, 10, 20, 200, 5, 6, 7, 255])
        wu = _pack8([255, 1, 2, 100, 40, 3, 2, 255])
        big = 100000 & MASK64
        d = DATA_BASE
        data = [(d + 0, 8, x0t), (d + 8, 8, w0t), (d + 16, 8, x1t), (d + 24, 8, w1t),
                (d + 32, 8, x2t), (d + 40, 8, w2t), (d + 48, 8, xu), (d + 56, 8, wu),
                (d + 64, 8, big)]
        a = Asm()
        a.la(30, DATA_BASE)
        a.ld(1, 30, 0); a.ld(2, 30, 8)       # tile0 x,w
        a.ld(3, 30, 16); a.ld(4, 30, 24)     # tile1 x,w
        a.ld(6, 30, 32); a.ld(7, 30, 40)     # tile2 x,w
        a.addi(5, 0, 0)                       # row accumulator = 0
        a.dot8(8, 1, 2)                       # standalone dot for comparison
        a.macc(5, 1, 2, 5)                    # acc += dot(tile0)
        a.macc(5, 3, 4, 5)                    # acc += dot(tile1)
        a.macc(5, 6, 7, 5)                    # acc += dot(tile2)  -> full row
        a.ld(10, 30, 48); a.ld(11, 30, 56)   # unsigned operands
        a.dot8u(12, 10, 11)
        a.dot8su(13, 10, 11)
        a.ld(14, 30, 64)                      # big int32
        a.addi(15, 0, 4)                      # shift = 4
        a.rqs(16, 14, 15)                     # requantize -> saturates (+127), SAT=1
        a.addi(17, 0, 6)
        a.addi(18, 0, 1)
        a.rqs(19, 17, 18)                     # (6+1)>>1 = 3, no clamp
        a.sd(5, 30, 72)                       # store the GEMM-row result
        a.la(31, TOHOST_ADDR); a.addi(28, 0, 1); a.sw(28, 31, 0)
        return a, data

    def prog_rqs_edges():
        # rqs at the 64-bit extremes: rounding must be computed in 65 bits (I5).
        d = DATA_BASE
        data = [(d + 0, 8, (1 << 63) - 1), (d + 8, 8, 1 << 63)]
        a = Asm()
        a.la(30, DATA_BASE)
        a.ld(1, 30, 0); a.ld(2, 30, 8)
        for i, sh in enumerate((63, 62, 32, 2, 1)):
            a.addi(5, 0, sh)
            a.rqs(10 + i, 1, 5)              # INT64_MAX
            a.rqs(20 + i, 2, 5)              # INT64_MIN
        a.la(31, TOHOST_ADDR); a.addi(28, 0, 1); a.sw(28, 31, 0)
        return a, data

    def prog_rqs_random():
        # Seeded sweep: full 64-bit rs1 (biased toward the extremes) x every shift
        # 0..63, so rqs rounding/clamp corners beyond the hand-picked ones are hit.
        import random
        rng = random.Random(0x5EED_0005)
        n = 64
        vals = []
        for i in range(n):
            kind = i % 4
            if kind == 0:
                v = rng.getrandbits(64)                          # anywhere
            elif kind == 1:
                v = ((1 << 63) - 1 - rng.getrandbits(20))        # just below INT64_MAX
            elif kind == 2:
                v = (1 << 63) + rng.getrandbits(20)              # just above INT64_MIN
            else:
                v = rng.getrandbits(40) - (1 << 39)              # int40-ish around 0
            vals.append(v & MASK64)
        d = DATA_BASE
        data = [(d + 8 * i, 8, v) for i, v in enumerate(vals)]
        a = Asm()
        a.la(30, DATA_BASE)
        for i in range(n):
            a.ld(1, 30, 8 * i)
            a.addi(5, 0, i % 64)             # every shift amount 0..63 is used
            a.rqs(10, 1, 5)
        a.la(31, TOHOST_ADDR); a.addi(28, 0, 1); a.sw(28, 31, 0)
        return a, data

    # (name, builder, max_cycles) — divides are multi-cycle so give muldiv room.
    programs = [("arith", prog_arith, 2000), ("loop", prog_loop, 2000),
                ("ldst", prog_ldst, 2000), ("call", prog_call, 2000),
                ("muldiv", prog_muldiv, 6000), ("xmlacc", prog_xmlacc, 2000),
                ("rqs_edges", prog_rqs_edges, 2000),
                ("rqs_random", prog_rqs_random, 4000)]

    n_xmlacc_compared = 0
    for name, builder, max_cycles in programs:
        built = builder()
        if isinstance(built, tuple):
            asm, data = built
        else:
            asm, data = built, None
        words = asm.assemble()
        retires, state, iss = await cosim_lockstep(
            dut, name, words, data=data, max_cycles=max_cycles,
            mem_check=(DATA_BASE, DATA_SIZE))

        # Count RV64M ops actually fetched/executed (opcode OP/OP-32 & funct7=1),
        # proving the mul/div path ran rather than being optimized/skipped.
        n_m = sum(1 for w in words
                  if (w & 0x7F) in (OPC_OP, OPC_OPW) and ((w >> 25) & 0x7F) == 0x01)
        if name == "muldiv":
            assert n_m >= 15, f"muldiv co-sim executed only {n_m} M instructions"

        # Count Xmlacc (CUSTOM0) ops and cross-check the mlstat SAT sticky: the
        # RTL exposes it on dbg_mlstat_sat, the ISS tracks it in hart.mlstat.
        n_xm = sum(1 for w in words if (w & 0x7F) == OPC_CUSTOM0)
        if name == "xmlacc":
            assert n_xm >= 5, f"xmlacc co-sim executed only {n_xm} Xmlacc ops"
            n_xmlacc_compared = len(retires)
            iss_sat = 1 if (getattr(iss, "mlstat", 0) & 1) else 0
            assert state["mlstat_sat"] == iss_sat, \
                f"mlstat SAT RTL={state['mlstat_sat']} ISS={iss_sat}"
            assert iss_sat == 1, "xmlacc program should have set SAT (saturating rqs)"
        dut._log.info("cosim[%s]: %d instructions matched ISS bit-for-bit "
                      "(%d RV64M, %d Xmlacc)", name, len(retires), n_m, n_xm)

    dut._log.info("cosim: Xmlacc co-sim compared %d retires incl. all 5 Tier-1 ops",
                  n_xmlacc_compared)


# ===========================================================================
# pytest entry point: build with Icarus and run all cocotb tests
# ===========================================================================
@pytest.mark.parametrize("toplevel", CORES)
def test_vortex_core(toplevel):
    """Build `toplevel` with Icarus and run every cocotb test in this module on it."""
    build_and_test(toplevel, "test_core", Path(__file__).parent)
