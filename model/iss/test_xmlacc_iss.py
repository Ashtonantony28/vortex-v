"""Directed + randomized tests for the Xmlacc Tier-1 ops in the RV64IM ISS.

Covers mlacc.dot8 / dot8u / dot8su / macc / rqs added to rv64i.py step()
(opcode CUSTOM0). Every ISS result is cross-checked bit-for-bit against the
numpy golden model at model/xmlacc_model.py (the oracle). Also covers the
mlstat SAT sticky and the minimal mlstat/mlcfg CSR read/W1C path.
"""

from __future__ import annotations

import os
import random
import sys

import pytest

# rv64i is a sibling module; xmlacc_model lives one level up in model/.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # model/
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "isa"))

import xmlacc_model as gold          # noqa: E402  (the oracle)
import xmlacc_isa as isa             # noqa: E402  (authoritative encoder)
from rv64i import Memory, Hart, u64, to_signed, RESET_PC, MASK64  # noqa: E402


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def pack(lanes):
    """Pack 8 lane values (signed or unsigned bytes) into a 64-bit reg value."""
    assert len(lanes) == 8
    u = 0
    for i, v in enumerate(lanes):
        u |= (int(v) & 0xFF) << (8 * i)
    return u


# minimal extra encoders for the halt/CSR programs
_OP_IMM = 0b0010011
_OP_SYSTEM = 0b1110011


def addi(rd, rs1, imm):
    return ((imm & 0xFFF) << 20) | (rs1 << 15) | (0b000 << 12) | (rd << 7) | _OP_IMM


def ecall():
    return _OP_SYSTEM


def csrrs(rd, csr, rs1):
    return ((csr & 0xFFF) << 20) | (rs1 << 15) | (0b010 << 12) | (rd << 7) | _OP_SYSTEM


def csrrw(rd, csr, rs1):
    return ((csr & 0xFFF) << 20) | (rs1 << 15) | (0b001 << 12) | (rd << 7) | _OP_SYSTEM


def csrrwi(rd, csr, zimm):
    return ((csr & 0xFFF) << 20) | (zimm << 15) | (0b101 << 12) | (rd << 7) | _OP_SYSTEM


def run_xmlacc(mnemonic, regs, **fields):
    """Encode one Xmlacc instr, preload source regs, step once, return (hart, rec)."""
    word = isa.encode(mnemonic, **fields)
    mem = Memory(base=RESET_PC)
    mem.load_words([word], base=RESET_PC)
    h = Hart(mem=mem, reset_pc=RESET_PC)
    for r, v in regs.items():
        h.x[r] = u64(v)
    rec = h.step()
    return h, rec


# --------------------------------------------------------------------------
# directed per-op tests (hand values)
# --------------------------------------------------------------------------
def test_dot8_basic():
    a = pack([1, 2, 3, 4, 5, 6, 7, 8])
    b = pack([1, 1, 1, 1, 1, 1, 1, 1])
    h, rec = run_xmlacc("mlacc.dot8", {1: a, 2: b}, rd=3, rs1=1, rs2=2)
    assert rec["rd"] == 3
    assert rec["rd_val"] == u64(gold.dot8(a, b)) == 36


def test_dot8_signed_negative():
    a = pack([-128, -128, -128, -128, -128, -128, -128, -128])
    b = pack([127, 127, 127, 127, 127, 127, 127, 127])
    h, rec = run_xmlacc("mlacc.dot8", {1: a, 2: b}, rd=5, rs1=1, rs2=2)
    exp = gold.dot8(a, b)          # 8 * (-128*127) = -130048
    assert exp == -130048
    assert rec["rd_val"] == u64(exp)
    assert to_signed(rec["rd_val"]) == -130048


def test_dot8u_basic():
    a = pack([255, 255, 255, 255, 255, 255, 255, 255])
    b = pack([255, 255, 255, 255, 255, 255, 255, 255])
    h, rec = run_xmlacc("mlacc.dot8u", {1: a, 2: b}, rd=4, rs1=1, rs2=2)
    exp = gold.dot8u(a, b)          # 8 * 255*255 = 520200
    assert exp == 520200
    assert rec["rd_val"] == u64(exp)


def test_dot8su_mixed():
    a = pack([-1, -2, 3, 4, -5, 6, -7, 8])      # signed activations
    b = pack([200, 10, 20, 30, 40, 50, 60, 70])  # unsigned weights
    h, rec = run_xmlacc("mlacc.dot8su", {1: a, 2: b}, rd=7, rs1=1, rs2=2)
    assert rec["rd_val"] == u64(gold.dot8su(a, b))


def test_macc_accumulate():
    a = pack([1, 2, 3, 4, 5, 6, 7, 8])
    b = pack([2, 2, 2, 2, 2, 2, 2, 2])
    acc0 = 1000
    h, rec = run_xmlacc("mlacc.macc", {1: a, 2: b, 4: acc0}, rd=3, rs1=1, rs2=2, rs3=4)
    assert rec["rd_val"] == u64(gold.macc(a, b, acc0))
    assert to_signed(rec["rd_val"]) == 1000 + 2 * 36  # 1072


def test_macc_negative_accumulator():
    a = pack([-10] * 8)
    b = pack([10] * 8)
    acc0 = u64(-5)
    h, rec = run_xmlacc("mlacc.macc", {1: a, 2: b, 4: acc0}, rd=3, rs1=1, rs2=2, rs3=4)
    assert rec["rd_val"] == u64(gold.macc(a, b, acc0))
    assert to_signed(rec["rd_val"]) == -5 + 8 * (-100)  # -805


# --------------------------------------------------------------------------
# rqs: rounding, arithmetic shift, saturation, SAT sticky
# --------------------------------------------------------------------------
def test_rqs_round_half_up():
    # sh=1: t = (x + 1) >> 1 (round half up). x=5 -> 3, x=-5 -> -2.
    for x, exp in [(5, 3), (4, 2), (-5, -2), (-4, -2), (0, 0), (1, 1), (-1, 0)]:
        h, rec = run_xmlacc("mlacc.rqs", {1: u64(x), 2: 1}, rd=3, rs1=1, rs2=2)
        gold_rd, gold_sat = gold.rqs(u64(x), 1)
        assert rec["rd_val"] == u64(gold_rd)
        assert to_signed(rec["rd_val"]) == exp, (x, exp, to_signed(rec["rd_val"]))
        assert h.mlstat_sat == gold_sat


def test_rqs_sh_zero_identity_then_clamp():
    # sh=0 -> no shift, no rounding; value just clamped to int8.
    h, rec = run_xmlacc("mlacc.rqs", {1: u64(100), 2: 0}, rd=3, rs1=1, rs2=2)
    assert to_signed(rec["rd_val"]) == 100
    assert not h.mlstat_sat
    h, rec = run_xmlacc("mlacc.rqs", {1: u64(1000), 2: 0}, rd=3, rs1=1, rs2=2)
    assert to_signed(rec["rd_val"]) == 127   # clamp high
    assert h.mlstat_sat


def test_rqs_saturation_both_ends():
    # Large positive after shift -> +127 sat; large negative -> -128 sat.
    h, rec = run_xmlacc("mlacc.rqs", {1: u64(100000), 2: 4}, rd=3, rs1=1, rs2=2)
    gold_rd, gold_sat = gold.rqs(u64(100000), 4)
    assert rec["rd_val"] == u64(gold_rd)
    assert to_signed(rec["rd_val"]) == 127 and h.mlstat_sat and gold_sat

    h, rec = run_xmlacc("mlacc.rqs", {1: u64(-100000), 2: 4}, rd=3, rs1=1, rs2=2)
    gold_rd, gold_sat = gold.rqs(u64(-100000), 4)
    assert rec["rd_val"] == u64(gold_rd)
    assert to_signed(rec["rd_val"]) == -128 and h.mlstat_sat and gold_sat


def test_rqs_no_saturation_no_sticky():
    h, rec = run_xmlacc("mlacc.rqs", {1: u64(500), 2: 3}, rd=3, rs1=1, rs2=2)
    gold_rd, gold_sat = gold.rqs(u64(500), 3)
    assert rec["rd_val"] == u64(gold_rd)
    assert not gold_sat and not h.mlstat_sat


def test_rqs_sticky_persists_across_ops():
    # Run a saturating rqs, then a non-saturating one; SAT must remain set.
    word_sat = isa.encode("mlacc.rqs", rd=3, rs1=1, rs2=2)
    word_ok = isa.encode("mlacc.rqs", rd=4, rs1=1, rs2=2)
    mem = Memory(base=RESET_PC)
    mem.load_words([word_sat, word_ok], base=RESET_PC)
    h = Hart(mem=mem, reset_pc=RESET_PC)
    h.x[1] = u64(100000)
    h.x[2] = 4
    h.step()
    assert h.mlstat_sat
    h.x[1] = u64(10)
    h.x[2] = 1
    h.step()
    assert h.mlstat_sat  # still sticky


# --------------------------------------------------------------------------
# mlstat CSR read / W1C clear path
# --------------------------------------------------------------------------
def test_mlstat_csr_read_and_w1c_clear():
    prog = [
        isa.encode("mlacc.rqs", rd=3, rs1=1, rs2=2),  # saturate -> SAT=1
        csrrs(10, 0x801, 0),      # x10 = mlstat (read, no write since rs1=x0)
        csrrwi(0, 0x801, 1),      # W1C bit0: clear SAT
        csrrs(11, 0x801, 0),      # x11 = mlstat after clear
        ecall(),
    ]
    mem = Memory(base=RESET_PC)
    mem.load_words(prog, base=RESET_PC)
    h = Hart(mem=mem, reset_pc=RESET_PC)
    h.x[1] = u64(100000)
    h.x[2] = 4
    h.run(max_steps=10)
    assert h.x[10] & 1 == 1       # SAT was observed set
    assert h.x[11] & 1 == 0       # W1C cleared it
    assert not h.mlstat_sat


def test_mlcfg_raz_wi():
    prog = [csrrw(10, 0x800, 1), csrrs(11, 0x800, 0), ecall()]
    mem = Memory(base=RESET_PC)
    mem.load_words(prog, base=RESET_PC)
    h = Hart(mem=mem, reset_pc=RESET_PC)
    h.x[1] = 0xDEAD
    h.run(max_steps=10)
    assert h.x[10] == 0 and h.x[11] == 0   # reads as zero, writes ignored


# --------------------------------------------------------------------------
# randomized cross-check against the numpy golden model
# --------------------------------------------------------------------------
@pytest.mark.parametrize("seed", range(8))
def test_random_cross_check_all_ops(seed):
    rng = random.Random(seed)
    for _ in range(64):
        a = rng.getrandbits(64)
        b = rng.getrandbits(64)
        c = rng.getrandbits(64)

        _, r = run_xmlacc("mlacc.dot8", {1: a, 2: b}, rd=3, rs1=1, rs2=2)
        assert r["rd_val"] == u64(gold.dot8(a, b))

        _, r = run_xmlacc("mlacc.dot8u", {1: a, 2: b}, rd=3, rs1=1, rs2=2)
        assert r["rd_val"] == u64(gold.dot8u(a, b))

        _, r = run_xmlacc("mlacc.dot8su", {1: a, 2: b}, rd=3, rs1=1, rs2=2)
        assert r["rd_val"] == u64(gold.dot8su(a, b))

        _, r = run_xmlacc("mlacc.macc", {1: a, 2: b, 4: c}, rd=3, rs1=1, rs2=2, rs3=4)
        assert r["rd_val"] == u64(gold.macc(a, b, c))

        sh = rng.randint(0, 63)
        h, r = run_xmlacc("mlacc.rqs", {1: a, 2: sh}, rd=3, rs1=1, rs2=2)
        g_rd, g_sat = gold.rqs(a, sh)
        assert r["rd_val"] == u64(g_rd)
        assert h.mlstat_sat == g_sat


def test_x0_destination_no_write():
    a = pack([1, 2, 3, 4, 5, 6, 7, 8])
    b = pack([1] * 8)
    h, rec = run_xmlacc("mlacc.dot8", {1: a, 2: b}, rd=0, rs1=1, rs2=2)
    assert rec["rd"] == 0
    assert rec["rd_val"] == 0
    assert h.x[0] == 0


def test_step_record_shape_xmlacc():
    a = pack([1] * 8)
    _, rec = run_xmlacc("mlacc.dot8", {1: a, 2: a}, rd=5, rs1=1, rs2=1)
    assert set(rec.keys()) == {"pc", "inst", "rd", "rd_val", "mem_write", "halted"}
    assert rec["mem_write"] is None and rec["halted"] is False
