"""pytest suite for xmlasm (Vortex-V Xmlacc Tier-1 assembler/disassembler)."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import xmlasm
from xmlasm import (
    assemble,
    assemble_line,
    disassemble,
    emit_dot_insn,
    reg_num,
    XmlasmError,
)

R_MNEMONICS = ["mlacc.dot8", "mlacc.dot8u", "mlacc.dot8su", "mlacc.rqs"]
R4_MNEMONICS = ["mlacc.macc"]

# a spread of register combos (xN form) to exercise all field widths
R_COMBOS = [
    ("x0", "x0", "x0"),
    ("x10", "x11", "x12"),
    ("x31", "x1", "x30"),
    ("x5", "x6", "x7"),
    ("x1", "x31", "x15"),
]
R4_COMBOS = [
    ("x0", "x0", "x0", "x0"),
    ("x10", "x11", "x12", "x13"),
    ("x31", "x30", "x29", "x28"),
    ("x1", "x2", "x3", "x4"),
]


# --- round-trips ------------------------------------------------------------
@pytest.mark.parametrize("mnem", R_MNEMONICS)
@pytest.mark.parametrize("combo", R_COMBOS)
def test_roundtrip_rtype(mnem, combo):
    line = f"{mnem} {combo[0]}, {combo[1]}, {combo[2]}"
    word = assemble_line(line)
    assert 0 <= word <= 0xFFFFFFFF
    assert word & 0x7F == xmlasm.OPCODE_CUSTOM0
    back = disassemble(word)
    # canonical disassembly re-assembles to the identical word
    assert assemble_line(back) == word
    # and names the same mnemonic + registers
    assert back == f"{mnem} {combo[0]}, {combo[1]}, {combo[2]}"


@pytest.mark.parametrize("mnem", R4_MNEMONICS)
@pytest.mark.parametrize("combo", R4_COMBOS)
def test_roundtrip_r4type(mnem, combo):
    line = f"{mnem} {combo[0]}, {combo[1]}, {combo[2]}, {combo[3]}"
    word = assemble_line(line)
    assert 0 <= word <= 0xFFFFFFFF
    back = disassemble(word)
    assert assemble_line(back) == word
    assert back == f"{mnem} {combo[0]}, {combo[1]}, {combo[2]}, {combo[3]}"


# --- exact hand-encoded words ----------------------------------------------
def test_exact_encoding_dot8():
    # dot8 x10,x11,x12: f7=0, rs2=12, rs1=11, f3=0, rd=10, op=0x0b
    # (12<<20)|(11<<15)|(0<<12)|(10<<7)|0x0b = 0x00C5850B
    assert assemble_line("mlacc.dot8 x10, x11, x12") == 0x00C5850B


def test_exact_encoding_rqs():
    # rqs x5,x6,x7: rs2=7, rs1=6, f3=5, rd=5, op=0x0b = 0x0073528B
    assert assemble_line("mlacc.rqs x5, x6, x7") == 0x0073528B


def test_exact_encoding_macc_r4():
    # macc x10,x11,x12,x13: rs3=13, f2=0, rs2=12, rs1=11, f3=4, rd=10, op=0x0b
    # = 0x68C5C50B
    assert assemble_line("mlacc.macc x10, x11, x12, x13") == 0x68C5C50B


def test_exact_encoding_dot8su():
    # dot8su x1,x2,x3: f3=2, rs2=3, rs1=2, rd=1
    # (3<<20)|(2<<15)|(2<<12)|(1<<7)|0x0b
    expected = (3 << 20) | (2 << 15) | (2 << 12) | (1 << 7) | 0x0B
    assert assemble_line("mlacc.dot8su x1, x2, x3") == expected == 0x0031208B


# --- ABI name vs xN equivalence --------------------------------------------
def test_abi_equiv_simple():
    assert reg_num("a0") == 10
    assert reg_num("zero") == 0
    assert reg_num("sp") == 2
    assert reg_num("ra") == 1
    assert reg_num("t6") == 31
    assert reg_num("s0") == reg_num("fp") == 8


def test_abi_equiv_assembly():
    assert (assemble_line("mlacc.dot8 a0, a1, a2")
            == assemble_line("mlacc.dot8 x10, x11, x12"))
    assert (assemble_line("mlacc.macc a0, a1, a2, a3")
            == assemble_line("mlacc.macc x10, x11, x12, x13"))
    assert (assemble_line("mlacc.rqs t0, t1, t2")
            == assemble_line("mlacc.rqs x5, x6, x7"))


def test_case_insensitive():
    assert (assemble_line("MLACC.DOT8 A0, A1, A2")
            == assemble_line("mlacc.dot8 a0, a1, a2"))


# --- assemble() block handling ---------------------------------------------
def test_assemble_block_with_comments_and_labels():
    text = """
    # a quantized GEMM inner-step
    loop:
        mlacc.dot8  a0, a1, a2    # signed dot product
        mlacc.macc  a0, a1, a2, a0

        mlacc.rqs   a0, a0, a3
    """
    words = assemble(text)
    assert len(words) == 3
    assert words[0] == assemble_line("mlacc.dot8 a0, a1, a2")
    assert words[1] == assemble_line("mlacc.macc a0, a1, a2, a0")
    assert words[2] == assemble_line("mlacc.rqs a0, a0, a3")


def test_inline_label_then_insn():
    assert (assemble_line("start: mlacc.dot8 a0, a1, a2")
            == assemble_line("mlacc.dot8 a0, a1, a2"))


# --- .insn emission ---------------------------------------------------------
def test_emit_dot_insn_rtype():
    assert (emit_dot_insn("mlacc.dot8 a0, a1, a2")
            == ".insn r CUSTOM_0, 0, 0, a0, a1, a2")
    assert (emit_dot_insn("mlacc.dot8u a0, a1, a2")
            == ".insn r CUSTOM_0, 1, 0, a0, a1, a2")
    assert (emit_dot_insn("mlacc.rqs a0, a1, a2")
            == ".insn r CUSTOM_0, 5, 0, a0, a1, a2")


def test_emit_dot_insn_r4type():
    assert (emit_dot_insn("mlacc.macc a0, a1, a2, a3")
            == ".insn r4 CUSTOM_0, 4, 0, a0, a1, a2, a3")


# --- error cases ------------------------------------------------------------
def test_error_unknown_mnemonic():
    with pytest.raises(XmlasmError):
        assemble_line("mlacc.bogus a0, a1, a2")


def test_error_bad_register():
    with pytest.raises(XmlasmError):
        assemble_line("mlacc.dot8 a0, x99, a2")
    with pytest.raises(XmlasmError):
        assemble_line("mlacc.dot8 a0, notareg, a2")


def test_error_wrong_operand_count():
    with pytest.raises(XmlasmError):
        assemble_line("mlacc.dot8 a0, a1")          # too few
    with pytest.raises(XmlasmError):
        assemble_line("mlacc.dot8 a0, a1, a2, a3")  # too many for R
    with pytest.raises(XmlasmError):
        assemble_line("mlacc.macc a0, a1, a2")      # too few for R4


def test_error_empty_line():
    with pytest.raises(XmlasmError):
        assemble_line("   # just a comment")


def test_error_disassemble_wrong_opcode():
    with pytest.raises(XmlasmError):
        disassemble(0x00000013)  # a RISC-V nop (addi x0,x0,0), opcode 0x13
