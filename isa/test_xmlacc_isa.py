"""pytest tests for the Xmlacc ISA opcode table (encode/decode)."""

import itertools

import pytest

import xmlacc_isa as isa


R_MNEMONICS = ["mlacc.dot8", "mlacc.dot8u", "mlacc.dot8su", "mlacc.rqs"]
R4_MNEMONICS = ["mlacc.macc"]
ALL_MNEMONICS = [d["mnemonic"] for d in isa.INSTRUCTIONS]

# A spread of register numbers including the boundaries 0 and 31.
REGS = [0, 1, 2, 7, 15, 16, 30, 31]


def bits(word, hi, lo):
    """Extract word[hi:lo] inclusive."""
    return (word >> lo) & ((1 << (hi - lo + 1)) - 1)


# --------------------------------------------------------------------------- #
# Table integrity
# --------------------------------------------------------------------------- #
def test_all_opcodes_are_custom0():
    for d in isa.INSTRUCTIONS:
        assert d["opcode"] == isa.OPCODE_CUSTOM0 == 0b0001011


def test_mnemonics_unique():
    names = [d["mnemonic"] for d in isa.INSTRUCTIONS]
    assert len(names) == len(set(names))


def test_r_funct3_unique_enough_to_decode():
    # Each (opcode, funct3, format-discriminant) must be unambiguous.
    seen = set()
    for d in isa.INSTRUCTIONS:
        disc = d.get("funct7") if d["fmt"] == "R" else d.get("funct2")
        key = (d["opcode"], d["funct3"], d["fmt"], disc)
        assert key not in seen
        seen.add(key)


# --------------------------------------------------------------------------- #
# Round-trip encode -> decode over a range of register numbers
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("mnem", R_MNEMONICS)
def test_roundtrip_r_type(mnem):
    for rd, rs1, rs2 in itertools.product(REGS, REGS, REGS):
        word = isa.encode(mnem, rd=rd, rs1=rs1, rs2=rs2)
        name, fields = isa.decode(word)
        assert name == mnem
        assert fields == {"rd": rd, "rs1": rs1, "rs2": rs2}


@pytest.mark.parametrize("mnem", R4_MNEMONICS)
def test_roundtrip_r4_type(mnem):
    for rd, rs1, rs2, rs3 in itertools.product(REGS, REGS, REGS, REGS):
        word = isa.encode(mnem, rd=rd, rs1=rs1, rs2=rs2, rs3=rs3)
        name, fields = isa.decode(word)
        assert name == mnem
        assert fields == {"rd": rd, "rs1": rs1, "rs2": rs2, "rs3": rs3}


# --------------------------------------------------------------------------- #
# Exact bit-field assertions
# --------------------------------------------------------------------------- #
def test_r_type_bitfields_exact():
    # mlacc.dot8su : funct3=0b010, funct7=0, opcode=0x0B
    word = isa.encode("mlacc.dot8su", rd=5, rs1=10, rs2=20)
    assert bits(word, 6, 0) == 0b0001011      # opcode
    assert bits(word, 11, 7) == 5             # rd
    assert bits(word, 14, 12) == 0b010        # funct3
    assert bits(word, 19, 15) == 10           # rs1
    assert bits(word, 24, 20) == 20           # rs2
    assert bits(word, 31, 25) == 0b0000000    # funct7


def test_r_type_funct3_values():
    assert bits(isa.encode("mlacc.dot8", rd=0, rs1=0, rs2=0), 14, 12) == 0b000
    assert bits(isa.encode("mlacc.dot8u", rd=0, rs1=0, rs2=0), 14, 12) == 0b001
    assert bits(isa.encode("mlacc.dot8su", rd=0, rs1=0, rs2=0), 14, 12) == 0b010
    assert bits(isa.encode("mlacc.rqs", rd=0, rs1=0, rs2=0), 14, 12) == 0b101


def test_r4_type_bitfields_exact():
    # mlacc.macc : funct3=0b100, funct2=0b00, rs3 in [31:27]
    word = isa.encode("mlacc.macc", rd=3, rs1=9, rs2=17, rs3=25)
    assert bits(word, 6, 0) == 0b0001011      # opcode
    assert bits(word, 11, 7) == 3             # rd
    assert bits(word, 14, 12) == 0b100        # funct3
    assert bits(word, 19, 15) == 9            # rs1
    assert bits(word, 24, 20) == 17           # rs2
    assert bits(word, 26, 25) == 0b00         # funct2
    assert bits(word, 31, 27) == 25           # rs3


def test_encoded_word_is_32_bits():
    for mnem in ALL_MNEMONICS:
        spec = isa.BY_MNEMONIC[mnem]
        kw = {"rd": 31, "rs1": 31, "rs2": 31}
        if spec["fmt"] == "R4":
            kw["rs3"] = 31
        word = isa.encode(mnem, **kw)
        assert 0 <= word <= 0xFFFFFFFF


def test_max_regs_dont_bleed_into_funct_fields():
    # All register fields = 31 (0b11111) must not corrupt the funct3 field.
    word = isa.encode("mlacc.dot8", rd=31, rs1=31, rs2=31)
    assert bits(word, 14, 12) == 0b000
    assert bits(word, 31, 25) == 0b0000000


# --------------------------------------------------------------------------- #
# Error handling
# --------------------------------------------------------------------------- #
def test_encode_unknown_mnemonic():
    with pytest.raises(KeyError):
        isa.encode("mlacc.nope", rd=0, rs1=0, rs2=0)


def test_encode_missing_operand():
    with pytest.raises(TypeError):
        isa.encode("mlacc.macc", rd=0, rs1=0, rs2=0)  # missing rs3


def test_encode_extra_operand():
    with pytest.raises(TypeError):
        isa.encode("mlacc.dot8", rd=0, rs1=0, rs2=0, rs3=0)  # rs3 not allowed


def test_encode_reg_out_of_range():
    with pytest.raises(ValueError):
        isa.encode("mlacc.dot8", rd=32, rs1=0, rs2=0)
    with pytest.raises(ValueError):
        isa.encode("mlacc.dot8", rd=-1, rs1=0, rs2=0)


def test_decode_unknown_word():
    with pytest.raises(ValueError):
        isa.decode(0xFFFFFFFF)   # opcode won't be CUSTOM0
    with pytest.raises(ValueError):
        isa.decode(0x00000000)   # opcode 0


def test_decode_bad_funct7_rejected():
    # Valid dot8 word but with a nonzero funct7 must not decode as dot8.
    word = isa.encode("mlacc.dot8", rd=1, rs1=2, rs2=3)
    word |= (0b1 << 25)   # corrupt funct7
    with pytest.raises(ValueError):
        isa.decode(word)
