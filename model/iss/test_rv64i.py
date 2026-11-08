"""Thorough pytest suite for the Vortex-V RV64I ISS (golden model).

Includes a tiny instruction encoder so directed tests and whole programs are
readable.  Expected results are hand-computed.
"""

from __future__ import annotations

import pytest

from rv64i import Memory, Hart, u64, to_signed, sext, RESET_PC, TOHOST_ADDR, MASK64

# --------------------------------------------------------------------------
# Minimal instruction encoder (RV64I subset).  Register args are ints 0..31.
# --------------------------------------------------------------------------

_OP_LUI = 0b0110111
_OP_AUIPC = 0b0010111
_OP_JAL = 0b1101111
_OP_JALR = 0b1100111
_OP_BRANCH = 0b1100011
_OP_LOAD = 0b0000011
_OP_STORE = 0b0100011
_OP_IMM = 0b0010011
_OP_REG = 0b0110011
_OP_IMM32 = 0b0011011
_OP_REG32 = 0b0111011
_OP_FENCE = 0b0001111
_OP_SYSTEM = 0b1110011


def _R(op, rd, f3, rs1, rs2, f7):
    return (f7 << 25) | (rs2 << 20) | (rs1 << 15) | (f3 << 12) | (rd << 7) | op


def _I(op, rd, f3, rs1, imm):
    return ((imm & 0xFFF) << 20) | (rs1 << 15) | (f3 << 12) | (rd << 7) | op


def _S(op, f3, rs1, rs2, imm):
    imm &= 0xFFF
    return ((imm >> 5) << 25) | (rs2 << 20) | (rs1 << 15) | (f3 << 12) \
        | ((imm & 0x1F) << 7) | op


def _B(op, f3, rs1, rs2, imm):
    imm &= 0x1FFF
    b12 = (imm >> 12) & 1
    b11 = (imm >> 11) & 1
    b10_5 = (imm >> 5) & 0x3F
    b4_1 = (imm >> 1) & 0xF
    return (b12 << 31) | (b10_5 << 25) | (rs2 << 20) | (rs1 << 15) \
        | (f3 << 12) | (b4_1 << 8) | (b11 << 7) | op


def _U(op, rd, imm):
    return (imm & 0xFFFFF000) | (rd << 7) | op


def _J(op, rd, imm):
    imm &= 0x1FFFFF
    b20 = (imm >> 20) & 1
    b19_12 = (imm >> 12) & 0xFF
    b11 = (imm >> 11) & 1
    b10_1 = (imm >> 1) & 0x3FF
    return (b20 << 31) | (b10_1 << 21) | (b11 << 20) | (b19_12 << 12) \
        | (rd << 7) | op


# mnemonic -> builder
def lui(rd, imm):   return _U(_OP_LUI, rd, imm)
def auipc(rd, imm): return _U(_OP_AUIPC, rd, imm)
def jal(rd, imm):   return _J(_OP_JAL, rd, imm)
def jalr(rd, rs1, imm): return _I(_OP_JALR, rd, 0, rs1, imm)

def beq(rs1, rs2, imm):  return _B(_OP_BRANCH, 0b000, rs1, rs2, imm)
def bne(rs1, rs2, imm):  return _B(_OP_BRANCH, 0b001, rs1, rs2, imm)
def blt(rs1, rs2, imm):  return _B(_OP_BRANCH, 0b100, rs1, rs2, imm)
def bge(rs1, rs2, imm):  return _B(_OP_BRANCH, 0b101, rs1, rs2, imm)
def bltu(rs1, rs2, imm): return _B(_OP_BRANCH, 0b110, rs1, rs2, imm)
def bgeu(rs1, rs2, imm): return _B(_OP_BRANCH, 0b111, rs1, rs2, imm)

def lb(rd, rs1, imm):  return _I(_OP_LOAD, rd, 0b000, rs1, imm)
def lh(rd, rs1, imm):  return _I(_OP_LOAD, rd, 0b001, rs1, imm)
def lw(rd, rs1, imm):  return _I(_OP_LOAD, rd, 0b010, rs1, imm)
def ld(rd, rs1, imm):  return _I(_OP_LOAD, rd, 0b011, rs1, imm)
def lbu(rd, rs1, imm): return _I(_OP_LOAD, rd, 0b100, rs1, imm)
def lhu(rd, rs1, imm): return _I(_OP_LOAD, rd, 0b101, rs1, imm)
def lwu(rd, rs1, imm): return _I(_OP_LOAD, rd, 0b110, rs1, imm)

def sb(rs2, rs1, imm): return _S(_OP_STORE, 0b000, rs1, rs2, imm)
def sh(rs2, rs1, imm): return _S(_OP_STORE, 0b001, rs1, rs2, imm)
def sw(rs2, rs1, imm): return _S(_OP_STORE, 0b010, rs1, rs2, imm)
def sd(rs2, rs1, imm): return _S(_OP_STORE, 0b011, rs1, rs2, imm)

def addi(rd, rs1, imm):  return _I(_OP_IMM, rd, 0b000, rs1, imm)
def slti(rd, rs1, imm):  return _I(_OP_IMM, rd, 0b010, rs1, imm)
def sltiu(rd, rs1, imm): return _I(_OP_IMM, rd, 0b011, rs1, imm)
def xori(rd, rs1, imm):  return _I(_OP_IMM, rd, 0b100, rs1, imm)
def ori(rd, rs1, imm):   return _I(_OP_IMM, rd, 0b110, rs1, imm)
def andi(rd, rs1, imm):  return _I(_OP_IMM, rd, 0b111, rs1, imm)
def slli(rd, rs1, sh):   return (0b000000 << 26) | ((sh & 0x3F) << 20) | (rs1 << 15) | (0b001 << 12) | (rd << 7) | _OP_IMM
def srli(rd, rs1, sh):   return (0b000000 << 26) | ((sh & 0x3F) << 20) | (rs1 << 15) | (0b101 << 12) | (rd << 7) | _OP_IMM
def srai(rd, rs1, sh):   return (0b010000 << 26) | ((sh & 0x3F) << 20) | (rs1 << 15) | (0b101 << 12) | (rd << 7) | _OP_IMM

def add(rd, rs1, rs2):  return _R(_OP_REG, rd, 0b000, rs1, rs2, 0b0000000)
def sub(rd, rs1, rs2):  return _R(_OP_REG, rd, 0b000, rs1, rs2, 0b0100000)
def sll(rd, rs1, rs2):  return _R(_OP_REG, rd, 0b001, rs1, rs2, 0b0000000)
def slt(rd, rs1, rs2):  return _R(_OP_REG, rd, 0b010, rs1, rs2, 0b0000000)
def sltu(rd, rs1, rs2): return _R(_OP_REG, rd, 0b011, rs1, rs2, 0b0000000)
def xor(rd, rs1, rs2):  return _R(_OP_REG, rd, 0b100, rs1, rs2, 0b0000000)
def srl(rd, rs1, rs2):  return _R(_OP_REG, rd, 0b101, rs1, rs2, 0b0000000)
def sra(rd, rs1, rs2):  return _R(_OP_REG, rd, 0b101, rs1, rs2, 0b0100000)
def or_(rd, rs1, rs2):  return _R(_OP_REG, rd, 0b110, rs1, rs2, 0b0000000)
def and_(rd, rs1, rs2): return _R(_OP_REG, rd, 0b111, rs1, rs2, 0b0000000)

def addiw(rd, rs1, imm): return _I(_OP_IMM32, rd, 0b000, rs1, imm)
def slliw(rd, rs1, sh):  return (0b0000000 << 25) | ((sh & 0x1F) << 20) | (rs1 << 15) | (0b001 << 12) | (rd << 7) | _OP_IMM32
def srliw(rd, rs1, sh):  return (0b0000000 << 25) | ((sh & 0x1F) << 20) | (rs1 << 15) | (0b101 << 12) | (rd << 7) | _OP_IMM32
def sraiw(rd, rs1, sh):  return (0b0100000 << 25) | ((sh & 0x1F) << 20) | (rs1 << 15) | (0b101 << 12) | (rd << 7) | _OP_IMM32

def addw(rd, rs1, rs2): return _R(_OP_REG32, rd, 0b000, rs1, rs2, 0b0000000)
def subw(rd, rs1, rs2): return _R(_OP_REG32, rd, 0b000, rs1, rs2, 0b0100000)
def sllw(rd, rs1, rs2): return _R(_OP_REG32, rd, 0b001, rs1, rs2, 0b0000000)
def srlw(rd, rs1, rs2): return _R(_OP_REG32, rd, 0b101, rs1, rs2, 0b0000000)
def sraw(rd, rs1, rs2): return _R(_OP_REG32, rd, 0b101, rs1, rs2, 0b0100000)

def fence():    return _I(_OP_FENCE, 0, 0b000, 0, 0)
def fence_i():  return _I(_OP_FENCE, 0, 0b001, 0, 0)
def ecall():    return _OP_SYSTEM  # imm=0, funct3=0

# --- RV64M (funct7 = 0000001) ---
_F7_M = 0b0000001

def mul(rd, rs1, rs2):    return _R(_OP_REG, rd, 0b000, rs1, rs2, _F7_M)
def mulh(rd, rs1, rs2):   return _R(_OP_REG, rd, 0b001, rs1, rs2, _F7_M)
def mulhsu(rd, rs1, rs2): return _R(_OP_REG, rd, 0b010, rs1, rs2, _F7_M)
def mulhu(rd, rs1, rs2):  return _R(_OP_REG, rd, 0b011, rs1, rs2, _F7_M)
def div(rd, rs1, rs2):    return _R(_OP_REG, rd, 0b100, rs1, rs2, _F7_M)
def divu(rd, rs1, rs2):   return _R(_OP_REG, rd, 0b101, rs1, rs2, _F7_M)
def rem(rd, rs1, rs2):    return _R(_OP_REG, rd, 0b110, rs1, rs2, _F7_M)
def remu(rd, rs1, rs2):   return _R(_OP_REG, rd, 0b111, rs1, rs2, _F7_M)

def mulw(rd, rs1, rs2):   return _R(_OP_REG32, rd, 0b000, rs1, rs2, _F7_M)
def divw(rd, rs1, rs2):   return _R(_OP_REG32, rd, 0b100, rs1, rs2, _F7_M)
def divuw(rd, rs1, rs2):  return _R(_OP_REG32, rd, 0b101, rs1, rs2, _F7_M)
def remw(rd, rs1, rs2):   return _R(_OP_REG32, rd, 0b110, rs1, rs2, _F7_M)
def remuw(rd, rs1, rs2):  return _R(_OP_REG32, rd, 0b111, rs1, rs2, _F7_M)


# --------------------------------------------------------------------------
# Test fixtures / helpers
# --------------------------------------------------------------------------

def make_hart(words, base=RESET_PC, **kw):
    """Build a hart with ``words`` loaded as the program at ``base``."""
    mem = Memory(base=base)
    mem.load_words(words, base)
    return Hart(mem=mem, reset_pc=base, **kw)


def run_one(word, setup=None):
    """Run a single instruction (optionally with register setup) and return
    (hart, record)."""
    h = make_hart([word])
    if setup:
        for reg, val in setup.items():
            h.x[reg] = u64(val)
    rec = h.step()
    return h, rec


# --------------------------------------------------------------------------
# Helper-function unit tests
# --------------------------------------------------------------------------

def test_to_signed():
    assert to_signed(0xFFFFFFFFFFFFFFFF) == -1
    assert to_signed(0x8000000000000000) == -(1 << 63)
    assert to_signed(0x7FFFFFFFFFFFFFFF) == (1 << 63) - 1
    assert to_signed(0xFFFFFFFF, 32) == -1
    assert to_signed(0x80, 8) == -128


def test_sext():
    assert sext(0xFF, 8) == MASK64
    assert sext(0x7F, 8) == 0x7F
    assert sext(0xFFFFFFFF, 32) == MASK64
    assert sext(0x80000000, 32) == 0xFFFFFFFF80000000


# --------------------------------------------------------------------------
# Memory tests
# --------------------------------------------------------------------------

def test_memory_little_endian_roundtrip():
    m = Memory(base=0x8000_0000)
    m.store(0x8000_0000, 8, 0x1122334455667788)
    assert m.load(0x8000_0000, 1) == 0x88
    assert m.load(0x8000_0001, 1) == 0x77
    assert m.load(0x8000_0000, 2) == 0x7788
    assert m.load(0x8000_0000, 4) == 0x55667788
    assert m.load(0x8000_0000, 8) == 0x1122334455667788


def test_memory_image_dict():
    m = Memory(base=0x8000_0000)
    m.load_image({0x8000_0000: 0xDE, 0x8000_0001: 0xAD})
    assert m.load(0x8000_0000, 2) == 0xADDE


def test_memory_load_words():
    m = Memory(base=0x8000_0000)
    m.load_words([0x01020304, 0xAABBCCDD])
    assert m.fetch(0x8000_0000) == 0x01020304
    assert m.fetch(0x8000_0004) == 0xAABBCCDD


# --------------------------------------------------------------------------
# Upper-immediate / jumps
# --------------------------------------------------------------------------

def test_lui():
    _, rec = run_one(lui(5, 0x12345000))
    assert rec["rd"] == 5
    assert rec["rd_val"] == 0x0000000012345000


def test_lui_sign_extended():
    # bit 31 set -> sign-extend to 64 bits
    _, rec = run_one(lui(5, 0xFFFFF000))
    assert rec["rd_val"] == 0xFFFFFFFFFFFFF000


def test_auipc():
    h, rec = run_one(auipc(6, 0x1000))
    assert rec["rd_val"] == u64(RESET_PC + 0x1000)


def test_jal_link_and_target():
    h, rec = run_one(jal(1, 0x100))
    assert rec["rd"] == 1
    assert rec["rd_val"] == u64(RESET_PC + 4)   # link = pc+4
    assert h.pc == u64(RESET_PC + 0x100)        # target = pc + imm


def test_jal_negative_offset():
    base = RESET_PC
    # place jal at pc = base+8 jumping back by 8
    words = [addi(0, 0, 0), addi(0, 0, 0), jal(1, -8)]
    h = make_hart(words)
    h.step(); h.step()
    rec = h.step()
    assert rec["rd_val"] == u64(base + 8 + 4)
    assert h.pc == u64(base + 8 - 8)


def test_jalr_link_target_lsb_clear():
    # rs1 = 0x8000_0203, imm = 4 -> 0x8000_0207, lsb cleared -> 0x8000_0206
    h, rec = run_one(jalr(1, 2, 4), setup={2: 0x8000_0203})
    assert rec["rd_val"] == u64(RESET_PC + 4)
    assert h.pc == 0x8000_0206


# --------------------------------------------------------------------------
# Branches
# --------------------------------------------------------------------------

@pytest.mark.parametrize("builder,a,b,taken", [
    (beq, 5, 5, True), (beq, 5, 6, False),
    (bne, 5, 6, True), (bne, 5, 5, False),
    (blt, to_signed(-1), 0, True), (blt, 1, -1, False),
    (bge, 0, to_signed(-1), True), (bge, -1, 0, False),
    (bltu, 1, u64(-1), True), (bltu, u64(-1), 1, False),
    (bgeu, u64(-1), 1, True), (bgeu, 1, u64(-1), False),
])
def test_branches(builder, a, b, taken):
    h = make_hart([builder(1, 2, 0x40)])
    h.x[1] = u64(a)
    h.x[2] = u64(b)
    h.step()
    assert h.pc == (u64(RESET_PC + 0x40) if taken else u64(RESET_PC + 4))


def test_blt_signed_vs_bltu_unsigned():
    # -1 vs 1: signed -1 < 1 (taken), unsigned 0xFFFF.. >= 1 (BLTU not taken)
    h1 = make_hart([blt(1, 2, 0x20)]); h1.x[1] = u64(-1); h1.x[2] = 1; h1.step()
    assert h1.pc == u64(RESET_PC + 0x20)
    h2 = make_hart([bltu(1, 2, 0x20)]); h2.x[1] = u64(-1); h2.x[2] = 1; h2.step()
    assert h2.pc == u64(RESET_PC + 4)


# --------------------------------------------------------------------------
# OP-IMM
# --------------------------------------------------------------------------

def test_addi():
    _, rec = run_one(addi(1, 2, 100), setup={2: 50})
    assert rec["rd_val"] == 150


def test_addi_negative():
    _, rec = run_one(addi(1, 2, -1), setup={2: 0})
    assert rec["rd_val"] == MASK64


def test_slti_signed():
    _, rec = run_one(slti(1, 2, 0), setup={2: u64(-5)})
    assert rec["rd_val"] == 1
    _, rec = run_one(slti(1, 2, 0), setup={2: 5})
    assert rec["rd_val"] == 0


def test_sltiu_unsigned():
    # imm -1 -> 0xFFF sign-extended to 0xFFFF..F ; compare unsigned
    _, rec = run_one(sltiu(1, 2, -1), setup={2: 0})
    assert rec["rd_val"] == 1
    # SLTIU rd, rs1, 1 is the "seqz" idiom: 1 iff rs1 == 0
    _, rec = run_one(sltiu(1, 2, 1), setup={2: 0})
    assert rec["rd_val"] == 1
    _, rec = run_one(sltiu(1, 2, 1), setup={2: 7})
    assert rec["rd_val"] == 0


def test_logical_imm():
    assert run_one(xori(1, 2, -1), setup={2: 0x0F})[1]["rd_val"] == u64(~0x0F)
    assert run_one(ori(1, 2, 0x0F), setup={2: 0xF0})[1]["rd_val"] == 0xFF
    assert run_one(andi(1, 2, 0x0F), setup={2: 0xFF})[1]["rd_val"] == 0x0F


def test_shift_imm_rv64_6bit():
    # SLLI by 40 (needs 6-bit shamt)
    _, rec = run_one(slli(1, 2, 40), setup={2: 1})
    assert rec["rd_val"] == (1 << 40)
    # SRLI logical
    _, rec = run_one(srli(1, 2, 4), setup={2: 0xFF00000000000000})
    assert rec["rd_val"] == 0x0FF0000000000000
    # SRAI arithmetic (sign bit set)
    _, rec = run_one(srai(1, 2, 4), setup={2: 0x8000000000000000})
    assert rec["rd_val"] == 0xF800000000000000


def test_srai_vs_srli():
    val = 0xFFFFFFFFFFFFFFF0
    assert run_one(srli(1, 2, 4), setup={2: val})[1]["rd_val"] == 0x0FFFFFFFFFFFFFFF
    assert run_one(srai(1, 2, 4), setup={2: val})[1]["rd_val"] == 0xFFFFFFFFFFFFFFFF


# --------------------------------------------------------------------------
# OP (register-register)
# --------------------------------------------------------------------------

def test_add_sub():
    assert run_one(add(1, 2, 3), setup={2: 10, 3: 32})[1]["rd_val"] == 42
    assert run_one(sub(1, 2, 3), setup={2: 10, 3: 32})[1]["rd_val"] == u64(-22)


def test_add_wraps_64bit():
    _, rec = run_one(add(1, 2, 3), setup={2: MASK64, 3: 1})
    assert rec["rd_val"] == 0   # wraps around


def test_sub_below_zero():
    _, rec = run_one(sub(1, 2, 3), setup={2: 0, 3: 1})
    assert rec["rd_val"] == MASK64


def test_slt_sltu():
    assert run_one(slt(1, 2, 3), setup={2: u64(-1), 3: 0})[1]["rd_val"] == 1
    assert run_one(sltu(1, 2, 3), setup={2: u64(-1), 3: 0})[1]["rd_val"] == 0


def test_logical_reg():
    assert run_one(xor(1, 2, 3), setup={2: 0xFF, 3: 0x0F})[1]["rd_val"] == 0xF0
    assert run_one(or_(1, 2, 3), setup={2: 0xF0, 3: 0x0F})[1]["rd_val"] == 0xFF
    assert run_one(and_(1, 2, 3), setup={2: 0xFF, 3: 0x0F})[1]["rd_val"] == 0x0F


def test_shift_reg():
    assert run_one(sll(1, 2, 3), setup={2: 1, 3: 63})[1]["rd_val"] == (1 << 63)
    # shift amount uses only low 6 bits: 64 & 0x3F == 0 -> no shift
    assert run_one(sll(1, 2, 3), setup={2: 0xABCD, 3: 64})[1]["rd_val"] == 0xABCD
    assert run_one(srl(1, 2, 3), setup={2: 0x8000000000000000, 3: 63})[1]["rd_val"] == 1
    assert run_one(sra(1, 2, 3), setup={2: 0x8000000000000000, 3: 63})[1]["rd_val"] == MASK64


# --------------------------------------------------------------------------
# W ops (32-bit, sign-extended to 64)
# --------------------------------------------------------------------------

def test_addiw_truncate_and_sext():
    # 0x7FFFFFFF + 1 -> 0x80000000 in 32 bits -> sign-extend
    _, rec = run_one(addiw(1, 2, 1), setup={2: 0x7FFFFFFF})
    assert rec["rd_val"] == 0xFFFFFFFF80000000
    # upper bits of source ignored
    _, rec = run_one(addiw(1, 2, 0), setup={2: 0xDEADBEEF_00000005})
    assert rec["rd_val"] == 5


def test_addw_subw():
    _, rec = run_one(addw(1, 2, 3), setup={2: 0xFFFFFFFF, 3: 1})
    assert rec["rd_val"] == 0          # 32-bit sum is 0
    _, rec = run_one(subw(1, 2, 3), setup={2: 0, 3: 1})
    assert rec["rd_val"] == 0xFFFFFFFFFFFFFFFF  # -1 sign-extended


def test_slliw_srliw_sraiw():
    _, rec = run_one(slliw(1, 2, 31), setup={2: 1})
    assert rec["rd_val"] == 0xFFFFFFFF80000000
    # SRLIW operates on low 32 bits, zero fill, then sign-extend result
    _, rec = run_one(srliw(1, 2, 4), setup={2: 0xF0000000})
    assert rec["rd_val"] == 0x0F000000
    # SRAIW arithmetic within 32 bits
    _, rec = run_one(sraiw(1, 2, 4), setup={2: 0x80000000})
    assert rec["rd_val"] == 0xFFFFFFFFF8000000


def test_sllw_srlw_sraw():
    _, rec = run_one(sllw(1, 2, 3), setup={2: 1, 3: 31})
    assert rec["rd_val"] == 0xFFFFFFFF80000000
    # shift amount for W ops uses low 5 bits
    _, rec = run_one(sllw(1, 2, 3), setup={2: 1, 3: 32})
    assert rec["rd_val"] == 1
    _, rec = run_one(srlw(1, 2, 3), setup={2: 0x80000000, 3: 4})
    assert rec["rd_val"] == 0x08000000
    _, rec = run_one(sraw(1, 2, 3), setup={2: 0x80000000, 3: 4})
    assert rec["rd_val"] == 0xFFFFFFFFF8000000


# --------------------------------------------------------------------------
# Loads / stores of each width
# --------------------------------------------------------------------------

def test_store_load_each_width():
    base = RESET_PC
    data = base + 0x800
    # store x2 (a pattern) at each width into memory then load back
    h = make_hart([sd(2, 1, 0)], base=base)
    h.x[1] = data
    h.x[2] = 0x1122334455667788
    rec = h.step()
    assert rec["mem_write"] == (data, 8, 0x1122334455667788)
    assert h.mem.load(data, 8) == 0x1122334455667788


def test_load_sign_extension():
    base = RESET_PC
    data = base + 0x800
    m = Memory(base=base)
    m.load_words([lb(1, 3, 0)], base)
    m.store(data, 1, 0x80)   # negative byte
    h = Hart(mem=m, reset_pc=base)
    h.x[3] = data
    rec = h.step()
    assert rec["rd_val"] == 0xFFFFFFFFFFFFFF80


def test_load_zero_extension():
    base = RESET_PC
    data = base + 0x800
    m = Memory(base=base)
    m.load_words([lbu(1, 3, 0)], base)
    m.store(data, 1, 0x80)
    h = Hart(mem=m, reset_pc=base)
    h.x[3] = data
    rec = h.step()
    assert rec["rd_val"] == 0x80


def test_lw_vs_lwu():
    base = RESET_PC
    data = base + 0x800
    for builder, expect in [(lw, 0xFFFFFFFF80000001), (lwu, 0x0000000080000001)]:
        m = Memory(base=base)
        m.load_words([builder(1, 3, 0)], base)
        m.store(data, 4, 0x80000001)
        h = Hart(mem=m, reset_pc=base)
        h.x[3] = data
        rec = h.step()
        assert rec["rd_val"] == expect, builder.__name__


def test_lh_lhu():
    base = RESET_PC
    data = base + 0x800
    for builder, expect in [(lh, 0xFFFFFFFFFFFF8001), (lhu, 0x0000000000008001)]:
        m = Memory(base=base)
        m.load_words([builder(1, 3, 0)], base)
        m.store(data, 2, 0x8001)
        h = Hart(mem=m, reset_pc=base)
        h.x[3] = data
        rec = h.step()
        assert rec["rd_val"] == expect, builder.__name__


def test_store_width_truncation():
    base = RESET_PC
    data = base + 0x800
    h = make_hart([sb(2, 1, 0)], base=base)
    h.x[1] = data
    h.x[2] = 0x1122334455667788
    rec = h.step()
    assert rec["mem_write"] == (data, 1, 0x88)
    assert h.mem.load(data, 1) == 0x88
    assert h.mem.load(data + 1, 1) == 0x00


# --------------------------------------------------------------------------
# x0 semantics
# --------------------------------------------------------------------------

def test_x0_stays_zero():
    # addi x0, x0, 123 must not change x0, and record rd_val 0
    h, rec = run_one(addi(0, 0, 123))
    assert rec["rd"] == 0
    assert rec["rd_val"] == 0
    assert h.x[0] == 0


def test_x0_as_source():
    _, rec = run_one(add(1, 0, 0))
    assert rec["rd_val"] == 0


# --------------------------------------------------------------------------
# FENCE / ECALL
# --------------------------------------------------------------------------

def test_fence_noop():
    h, rec = run_one(fence())
    assert rec["rd"] is None
    assert not rec["halted"]
    assert h.pc == u64(RESET_PC + 4)


def test_ecall_halts():
    h = make_hart([ecall()])
    rec = h.step()
    assert rec["halted"]
    assert h.halted
    assert h.halt_code == (True, 0)


# --------------------------------------------------------------------------
# tohost watcher
# --------------------------------------------------------------------------

def test_tohost_pass():
    base = RESET_PC
    # x1 preset to TOHOST_ADDR (address synthesis is covered by test_lui_*)
    words = [
        addi(2, 0, 1),           # x2 = 1
        sw(2, 1, 0),             # store 1 -> tohost
    ]
    h = make_hart(words, base=base)
    h.x[1] = TOHOST_ADDR
    summary = h.run(max_steps=10)
    assert summary["halted"]
    assert h.halt_code == (True, 0)


def test_tohost_pass_synthesized_addr():
    """Build 0x0000000080001000 with the shift idiom (no sign-extension)."""
    base = RESET_PC
    words = [
        addi(1, 0, 1),           # x1 = 1
        slli(1, 1, 31),          # x1 = 0x0000000080000000
        addi(1, 1, 0x400),       # +0x400
        addi(1, 1, 0x400),       # +0x400
        addi(1, 1, 0x400),       # +0x400
        addi(1, 1, 0x400),       # +0x400 -> 0x80001000
        addi(2, 0, 1),           # x2 = 1
        sw(2, 1, 0),             # store 1 -> tohost
    ]
    h = make_hart(words, base=base)
    summary = h.run(max_steps=20)
    assert summary["halted"]
    assert h.halt_code == (True, 0)


def test_tohost_fail_code():
    base = RESET_PC
    words = [
        addi(2, 0, 7),          # odd value 7 -> FAIL code 3
        sw(2, 1, 0),
    ]
    h = make_hart(words, base=base)
    h.x[1] = TOHOST_ADDR
    h.run(max_steps=10)
    assert h.halted
    assert h.halt_code == (False, 3)


# --------------------------------------------------------------------------
# RV64M — multiply (64-bit)
# --------------------------------------------------------------------------

def test_mul_basic():
    assert run_one(mul(1, 2, 3), setup={2: 6, 3: 7})[1]["rd_val"] == 42


def test_mul_low64_wraps():
    # low 64 bits of the product; signedness irrelevant for MUL
    _, rec = run_one(mul(1, 2, 3), setup={2: MASK64, 3: MASK64})
    # (2^64-1)^2 mod 2^64 == 1
    assert rec["rd_val"] == 1


def test_mul_negative_operands_low():
    # (-3) * 5 = -15 -> low 64 bits = -15
    _, rec = run_one(mul(1, 2, 3), setup={2: u64(-3), 3: 5})
    assert rec["rd_val"] == u64(-15)


def test_mulh_signed_signed():
    # (-1) * (-1) = 1 ; high 64 bits of 128-bit product = 0
    assert run_one(mulh(1, 2, 3), setup={2: u64(-1), 3: u64(-1)})[1]["rd_val"] == 0
    # (-1) * 2 = -2 ; 128-bit two's complement high word = all ones
    assert run_one(mulh(1, 2, 3), setup={2: u64(-1), 3: 2})[1]["rd_val"] == MASK64
    # 2^62 * 2^62 = 2^124 ; high 64 bits = 2^60
    assert run_one(mulh(1, 2, 3), setup={2: 1 << 62, 3: 1 << 62})[1]["rd_val"] == (1 << 60)
    # most-negative * most-negative = 2^126 ; high 64 = 2^62
    mn = 1 << 63
    assert run_one(mulh(1, 2, 3), setup={2: mn, 3: mn})[1]["rd_val"] == (1 << 62)


def test_mulhu_unsigned_unsigned():
    # (2^64-1)^2 = 2^128 - 2^65 + 1 ; high 64 = 2^64 - 2 = 0xFFFF...FE
    assert run_one(mulhu(1, 2, 3), setup={2: MASK64, 3: MASK64})[1]["rd_val"] == (MASK64 - 1)
    # 2^63 * 2 = 2^64 ; high 64 = 1
    assert run_one(mulhu(1, 2, 3), setup={2: 1 << 63, 3: 2})[1]["rd_val"] == 1
    assert run_one(mulhu(1, 2, 3), setup={2: 3, 3: 5})[1]["rd_val"] == 0


def test_mulhsu_signed_unsigned():
    # signed(-1) * unsigned(2^64-1) = -(2^64-1) ; 128-bit two's comp high word
    val = ((to_signed(MASK64) * MASK64) & ((1 << 128) - 1)) >> 64
    assert run_one(mulhsu(1, 2, 3), setup={2: u64(-1), 3: MASK64})[1]["rd_val"] == val
    assert val == MASK64  # -(2^64-1) high word is all ones
    # signed(-1) * unsigned(1) = -1 ; high word all ones
    assert run_one(mulhsu(1, 2, 3), setup={2: u64(-1), 3: 1})[1]["rd_val"] == MASK64
    # positive signed * unsigned matches MULHU for that case
    assert run_one(mulhsu(1, 2, 3), setup={2: 1 << 62, 3: 4})[1]["rd_val"] == 1


def test_mulh_family_distinct():
    # rs1 = -1 (0xFFFF..FF), rs2 = 2 : the three high-mul ops disagree
    s = {2: u64(-1), 3: 2}
    assert run_one(mulh(1, 2, 3), setup=s)[1]["rd_val"] == MASK64         # signed*signed: -2 -> high all ones
    assert run_one(mulhu(1, 2, 3), setup=s)[1]["rd_val"] == 1             # unsigned: (2^64-1)*2 high = 1
    assert run_one(mulhsu(1, 2, 3), setup=s)[1]["rd_val"] == MASK64       # signed*unsigned: -2 -> high all ones


# --------------------------------------------------------------------------
# RV64M — divide / remainder (64-bit)
# --------------------------------------------------------------------------

def test_div_basic_signed():
    assert run_one(div(1, 2, 3), setup={2: 20, 3: 6})[1]["rd_val"] == 3
    assert run_one(rem(1, 2, 3), setup={2: 20, 3: 6})[1]["rd_val"] == 2


def test_div_rounds_toward_zero_negative():
    # -7 / 2 = -3 (toward zero), remainder -1 (sign of dividend)
    assert run_one(div(1, 2, 3), setup={2: u64(-7), 3: 2})[1]["rd_val"] == u64(-3)
    assert run_one(rem(1, 2, 3), setup={2: u64(-7), 3: 2})[1]["rd_val"] == u64(-1)
    # 7 / -2 = -3, remainder 1
    assert run_one(div(1, 2, 3), setup={2: 7, 3: u64(-2)})[1]["rd_val"] == u64(-3)
    assert run_one(rem(1, 2, 3), setup={2: 7, 3: u64(-2)})[1]["rd_val"] == 1
    # -7 / -2 = 3, remainder -1
    assert run_one(div(1, 2, 3), setup={2: u64(-7), 3: u64(-2)})[1]["rd_val"] == 3
    assert run_one(rem(1, 2, 3), setup={2: u64(-7), 3: u64(-2)})[1]["rd_val"] == u64(-1)


def test_divu_remu_basic():
    assert run_one(divu(1, 2, 3), setup={2: 20, 3: 6})[1]["rd_val"] == 3
    assert run_one(remu(1, 2, 3), setup={2: 20, 3: 6})[1]["rd_val"] == 2
    # large unsigned dividend that is negative when viewed signed
    assert run_one(divu(1, 2, 3), setup={2: MASK64, 3: 2})[1]["rd_val"] == (MASK64 >> 1)
    assert run_one(remu(1, 2, 3), setup={2: MASK64, 3: 2})[1]["rd_val"] == 1


def test_div_by_zero_all_forms():
    # DIV  by zero -> -1 (all ones) ; REM  by zero -> dividend
    assert run_one(div(1, 2, 3), setup={2: 123, 3: 0})[1]["rd_val"] == MASK64
    assert run_one(rem(1, 2, 3), setup={2: 123, 3: 0})[1]["rd_val"] == 123
    assert run_one(rem(1, 2, 3), setup={2: u64(-123), 3: 0})[1]["rd_val"] == u64(-123)
    # DIVU by zero -> all ones (2^64-1) ; REMU by zero -> dividend
    assert run_one(divu(1, 2, 3), setup={2: 123, 3: 0})[1]["rd_val"] == MASK64
    assert run_one(remu(1, 2, 3), setup={2: 123, 3: 0})[1]["rd_val"] == 123


def test_div_signed_overflow():
    # most-negative / -1 : DIV -> dividend (most-negative), REM -> 0
    mn = 1 << 63
    assert run_one(div(1, 2, 3), setup={2: mn, 3: u64(-1)})[1]["rd_val"] == mn
    assert run_one(rem(1, 2, 3), setup={2: mn, 3: u64(-1)})[1]["rd_val"] == 0
    # DIVU on the same bit patterns is a plain unsigned divide (no overflow rule)
    # 2^63 / (2^64-1) = 0 ; remainder = 2^63
    assert run_one(divu(1, 2, 3), setup={2: mn, 3: u64(-1)})[1]["rd_val"] == 0
    assert run_one(remu(1, 2, 3), setup={2: mn, 3: u64(-1)})[1]["rd_val"] == mn


# --------------------------------------------------------------------------
# RV64M — *W variants (32-bit, result sign-extended to 64)
# --------------------------------------------------------------------------

def test_mulw_truncate_and_sext():
    # 0x10000 * 0x10000 = 0x1_0000_0000 ; low 32 = 0 -> 0
    assert run_one(mulw(1, 2, 3), setup={2: 0x10000, 3: 0x10000})[1]["rd_val"] == 0
    # 0x7FFFFFFF * 2 = 0xFFFFFFFE (low 32) -> sign-extend
    assert run_one(mulw(1, 2, 3), setup={2: 0x7FFFFFFF, 3: 2})[1]["rd_val"] == 0xFFFFFFFFFFFFFFFE
    # high bits of operands ignored; (-3)*5 in 32 bits = -15 -> sext
    assert run_one(mulw(1, 2, 3), setup={2: 0xDEADBEEF_FFFFFFFD, 3: 5})[1]["rd_val"] == u64(-15)


def test_divw_remw_signed():
    # -7 / 2 (32-bit) = -3, rem -1 ; both sign-extended
    assert run_one(divw(1, 2, 3), setup={2: u64(-7), 3: 2})[1]["rd_val"] == u64(-3)
    assert run_one(remw(1, 2, 3), setup={2: u64(-7), 3: 2})[1]["rd_val"] == u64(-1)
    # operands' high 32 bits ignored: low32 = -7 and 2 regardless of garbage above
    assert run_one(divw(1, 2, 3), setup={2: 0xAAAAAAAA_FFFFFFF9, 3: 0x55555555_00000002})[1]["rd_val"] == u64(-3)


def test_divw_sext_positive_result_with_high_bit():
    # 0xFFFFFFFE (= -2 in 32 bits) / 1 = -2 -> sext 0xFFFF...FE
    assert run_one(divw(1, 2, 3), setup={2: 0xFFFFFFFE, 3: 1})[1]["rd_val"] == 0xFFFFFFFFFFFFFFFE


def test_divuw_remuw_unsigned():
    # 0xFFFFFFFF / 2 = 0x7FFFFFFF (unsigned 32-bit) -> sext (bit31=0) = 0x7FFFFFFF
    assert run_one(divuw(1, 2, 3), setup={2: 0xFFFFFFFF, 3: 2})[1]["rd_val"] == 0x7FFFFFFF
    assert run_one(remuw(1, 2, 3), setup={2: 0xFFFFFFFF, 3: 2})[1]["rd_val"] == 1
    # result with bit31 set must sign-extend: 0xFFFFFFFE / 1 = 0xFFFFFFFE -> sext
    assert run_one(divuw(1, 2, 3), setup={2: 0xFFFFFFFE, 3: 1})[1]["rd_val"] == 0xFFFFFFFFFFFFFFFE


def test_w_div_by_zero():
    # DIVW/DIVUW by zero -> -1 (all ones, sign-extended) ; REMW/REMUW -> dividend (sext low32)
    assert run_one(divw(1, 2, 3), setup={2: 5, 3: 0})[1]["rd_val"] == MASK64
    assert run_one(divuw(1, 2, 3), setup={2: 5, 3: 0})[1]["rd_val"] == MASK64
    # REMW dividend 0x80000005 (bit31 set) -> sext
    assert run_one(remw(1, 2, 3), setup={2: 0x80000005, 3: 0})[1]["rd_val"] == 0xFFFFFFFF80000005
    assert run_one(remuw(1, 2, 3), setup={2: 0x00000005, 3: 0})[1]["rd_val"] == 5


def test_w_div_signed_overflow():
    # 0x80000000 (= -2^31 in 32 bits) / -1 : DIVW -> dividend (sext), REMW -> 0
    assert run_one(divw(1, 2, 3), setup={2: 0x80000000, 3: u64(-1)})[1]["rd_val"] == 0xFFFFFFFF80000000
    assert run_one(remw(1, 2, 3), setup={2: 0x80000000, 3: u64(-1)})[1]["rd_val"] == 0


def test_m_writes_x0_discarded():
    h, rec = run_one(mul(0, 2, 3), setup={2: 6, 3: 7})
    assert rec["rd"] == 0
    assert rec["rd_val"] == 0
    assert h.x[0] == 0


# --------------------------------------------------------------------------
# Whole programs
# --------------------------------------------------------------------------

def test_program_sum_1_to_10():
    """sum = 0; for i in 1..10: sum += i;  store sum then 1 to tohost."""
    base = RESET_PC
    # x1=sum, x2=i, x3=limit(10), x10=tohost (preset)
    words = [
        addi(1, 0, 0),           # 0x00 sum = 0
        addi(2, 0, 1),           # 0x04 i = 1
        addi(3, 0, 10),          # 0x08 limit = 10
        # loop: (pc 0x0C)
        add(1, 1, 2),            # 0x0C sum += i
        addi(2, 2, 1),           # 0x10 i += 1
        bge(3, 2, -8),           # 0x14 if limit >= i goto loop (back 8 -> 0x0C)
        sd(1, 10, 0x100),        # 0x18 store sum at tohost+0x100 (scratch)
        addi(4, 0, 1),           # 0x1C x4 = 1
        sw(4, 10, 0),            # 0x20 store 1 -> tohost (halt, pass)
    ]
    h = make_hart(words, base=base)
    h.x[10] = TOHOST_ADDR
    summary = h.run(max_steps=200)
    assert summary["halted"]
    assert h.halt_code == (True, 0)
    assert h.x[1] == 55
    assert h.mem.load(TOHOST_ADDR + 0x100, 8) == 55


def test_program_memcpy():
    """Copy 4 words from src to dst, then signal pass."""
    base = RESET_PC
    src = base + 0x400
    dst = base + 0x600
    # preload source data
    m = Memory(base=base)
    srcdata = [0x11111111, 0x22222222, 0x33333333, 0x44444444]
    # x1=src, x2=dst, x3=count, x4=tmp, x10=tohost (all preset)
    words = [
        addi(3, 0, 4),    # 0x00 x3 = count
        # loop (pc 0x04)
        lw(4, 1, 0),      # 0x04 tmp = *src
        sw(4, 2, 0),      # 0x08 *dst = tmp
        addi(1, 1, 4),    # 0x0C src += 4
        addi(2, 2, 4),    # 0x10 dst += 4
        addi(3, 3, -1),   # 0x14 count -= 1
        bne(3, 0, -20),   # 0x18 if count != 0 goto 0x04 (back 20)
        addi(5, 0, 1),    # 0x1C x5 = 1
        sw(5, 10, 0),     # 0x20 store 1 -> tohost
    ]
    m.load_words(words, base)
    for i, w in enumerate(srcdata):
        m.store(src + 4 * i, 4, w)
    h = Hart(mem=m, reset_pc=base)
    h.x[1] = src
    h.x[2] = dst
    h.x[10] = TOHOST_ADDR
    summary = h.run(max_steps=500)
    assert summary["halted"]
    assert h.halt_code == (True, 0)
    for i, w in enumerate(srcdata):
        assert h.mem.load(dst + 4 * i, 4) == w


def test_program_muldiv():
    """Exercise MUL/DIV/REM in a small program; mirrors the RTL co-sim program.

    x2=20, x3=6: q=x2/x3, r=x2%x3, p=q*x3+r should reconstruct x2=20.
    Store the reconstruction and halt PASS.
    """
    base = RESET_PC
    words = [
        addi(2, 0, 20),          # x2 = 20
        addi(3, 0, 6),           # x3 = 6
        div(4, 2, 3),            # x4 = 3
        rem(5, 2, 3),            # x5 = 2
        mul(6, 4, 3),            # x6 = 18
        add(7, 6, 5),            # x7 = 20 (== x2)
        sd(7, 10, 0x100),        # store 20 at tohost+0x100
        addi(8, 0, 1),           # x8 = 1
        sw(8, 10, 0),            # tohost = 1 -> halt
    ]
    h = make_hart(words, base=base)
    h.x[10] = TOHOST_ADDR
    summary = h.run(max_steps=100)
    assert summary["halted"]
    assert h.halt_code == (True, 0)
    assert h.x[4] == 3 and h.x[5] == 2 and h.x[6] == 18 and h.x[7] == 20
    assert h.mem.load(TOHOST_ADDR + 0x100, 8) == 20


def test_run_max_steps_guard():
    # infinite loop: jal x0, 0  (jump to self)
    h = make_hart([jal(0, 0)])
    summary = h.run(max_steps=50)
    assert not summary["halted"]
    assert summary["steps"] == 50


# --------------------------------------------------------------------------
# step() record shape
# --------------------------------------------------------------------------

def test_step_record_shape():
    _, rec = run_one(addi(5, 0, 1))
    assert set(rec.keys()) == {"pc", "inst", "rd", "rd_val", "mem_write", "halted"}
    assert rec["pc"] == RESET_PC
    assert rec["rd"] == 5
    assert rec["rd_val"] == 1
    assert rec["mem_write"] is None
    assert rec["halted"] is False
