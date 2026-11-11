"""Integration test: the assembler (asm/xmlasm.py) and the ISA table
(isa/xmlacc_isa.py) were built independently from the same frozen spec
(docs/XMLACC-SPEC-v1.md). This proves they encode/decode identically, so
there is genuinely one source of truth and not two that happen to look alike.

Run: python3 -m pytest verif/ -q   (from the repo root)
"""
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _sub in ("isa", "asm"):
    p = os.path.join(_ROOT, _sub)
    if p not in sys.path:
        sys.path.insert(0, p)

import xmlacc_isa  # noqa: E402  (from isa/)
import xmlasm  # noqa: E402  (from asm/)

# A spread of register numbers incl. x0 and the top of the file.
REGS = [0, 1, 2, 7, 10, 11, 12, 13, 15, 16, 30, 31]


def _operands(instr):
    return xmlacc_isa._OPERANDS[instr["fmt"]]


def _reg_combos(n):
    """A handful of deterministic n-tuples drawn from REGS."""
    combos = []
    for i in range(len(REGS)):
        combos.append(tuple(REGS[(i + k) % len(REGS)] for k in range(n)))
    return combos


def _asm_text(mnemonic, regvals):
    return mnemonic + " " + ", ".join("x%d" % r for r in regvals)


ALL_CASES = []
for _instr in xmlacc_isa.INSTRUCTIONS:
    _ops = _operands(_instr)
    for _combo in _reg_combos(len(_ops)):
        ALL_CASES.append((_instr["mnemonic"], _ops, _combo))


@pytest.mark.parametrize("mnemonic,ops,regvals", ALL_CASES)
def test_assembler_matches_isa_encode(mnemonic, ops, regvals):
    fields = dict(zip(ops, regvals))
    isa_word = xmlacc_isa.encode(mnemonic, **fields) & 0xFFFFFFFF
    asm_word = xmlasm.assemble_line(_asm_text(mnemonic, regvals)) & 0xFFFFFFFF
    assert asm_word == isa_word, (
        "%s %s: asm=0x%08X isa=0x%08X" % (mnemonic, regvals, asm_word, isa_word)
    )


@pytest.mark.parametrize("mnemonic,ops,regvals", ALL_CASES)
def test_isa_decode_matches_assembler_roundtrip(mnemonic, ops, regvals):
    fields = dict(zip(ops, regvals))
    word = xmlacc_isa.encode(mnemonic, **fields) & 0xFFFFFFFF
    dec_mnem, dec_fields = xmlacc_isa.decode(word)
    assert dec_mnem == mnemonic
    for name, val in fields.items():
        assert dec_fields[name] == val
    # The assembler's disassembler must name the same instruction.
    assert xmlasm.disassemble(word).split()[0] == mnemonic


def test_every_mnemonic_is_covered():
    seen = {m for (m, _ops, _rv) in ALL_CASES}
    assert seen == set(xmlacc_isa.BY_MNEMONIC), (
        "coverage gap between ISA table and this test: %s"
        % (seen ^ set(xmlacc_isa.BY_MNEMONIC))
    )
