"""Xmlacc v1 ISA opcode table — single source of truth.

This module is dependency-free (pure Python). It defines the machine-readable
instruction table for the Vortex-V custom ML extension (Xmlacc, opcode CUSTOM0)
and derives the ``encode``/``decode`` functions from that table.

Derived per the frozen canonical spec at docs/XMLACC-SPEC-v1.md:

  XLEN = 64. A GP register holds 8 packed int8 lanes (byte 0 = bits [7:0]).

  R-type:  funct7[31:25] rs2[24:20] rs1[19:15] funct3[14:12] rd[11:7] opcode[6:0]
  R4-type: rs3[31:27] funct2[26:25] rs2[24:20] rs1[19:15] funct3[14:12] rd[11:7] opcode[6:0]

  opcode CUSTOM0 = 0b0001011 (0x0B).
"""

# --------------------------------------------------------------------------- #
# Opcodes
# --------------------------------------------------------------------------- #
OPCODE_CUSTOM0 = 0b0001011  # 0x0B

# --------------------------------------------------------------------------- #
# Field layout (bit position of the LSB, and width) for each named field.
# These are the ONLY places the bit geometry is written down.
# --------------------------------------------------------------------------- #
FIELDS = {
    "opcode": (0, 7),
    "rd":     (7, 5),
    "funct3": (12, 3),
    "rs1":    (15, 5),
    "rs2":    (20, 5),
    # R-type:
    "funct7": (25, 7),
    # R4-type (overlap the funct7 span; mutually exclusive by format):
    "funct2": (25, 2),
    "rs3":    (27, 5),
}

# Register operands supplied by the caller at encode time, per format.
_OPERANDS = {
    "R":  ("rd", "rs1", "rs2"),
    "R4": ("rd", "rs1", "rs2", "rs3"),
}

# --------------------------------------------------------------------------- #
# The instruction table — the single source of truth.
# Each entry fully pins the fixed (opcode/funct) bits; the variable register
# fields are listed in _OPERANDS[fmt].
# --------------------------------------------------------------------------- #
INSTRUCTIONS = [
    {
        "mnemonic": "mlacc.dot8",
        "fmt": "R",
        "opcode": OPCODE_CUSTOM0,
        "funct3": 0b000,
        "funct7": 0b0000000,
        "doc": "rd = SX64( sum_{i=0..7} sext8(rs1[i]) * sext8(rs2[i]) )",
    },
    {
        "mnemonic": "mlacc.dot8u",
        "fmt": "R",
        "opcode": OPCODE_CUSTOM0,
        "funct3": 0b001,
        "funct7": 0b0000000,
        "doc": "rd = ZX64( sum zext8(rs1[i]) * zext8(rs2[i]) )",
    },
    {
        "mnemonic": "mlacc.dot8su",
        "fmt": "R",
        "opcode": OPCODE_CUSTOM0,
        "funct3": 0b010,
        "funct7": 0b0000000,
        "doc": "rd = SX64( sum sext8(rs1[i]) * zext8(rs2[i]) )",
    },
    {
        "mnemonic": "mlacc.macc",
        "fmt": "R4",
        "opcode": OPCODE_CUSTOM0,
        "funct3": 0b100,
        "funct2": 0b00,
        "doc": "rd = rs3 + sum sext8(rs1[i]) * sext8(rs2[i])",
    },
    {
        "mnemonic": "mlacc.rqs",
        "fmt": "R",
        "opcode": OPCODE_CUSTOM0,
        "funct3": 0b101,
        "funct7": 0b0000000,
        "doc": "sh=rs2[5:0]; t=(rs1 + (sh? 1<<(sh-1):0)) >>s sh; rd = SX64(clamp(t,-128,127))",
    },
]

# Fast lookups derived from the table.
BY_MNEMONIC = {d["mnemonic"]: d for d in INSTRUCTIONS}


def _mask(width):
    return (1 << width) - 1


def _check_reg(name, val):
    if not isinstance(val, int) or isinstance(val, bool):
        raise TypeError(f"{name} must be an int register number, got {val!r}")
    if not (0 <= val <= 31):
        raise ValueError(f"{name}={val} out of range [0,31]")


def _place(word, field, value):
    pos, width = FIELDS[field]
    if not (0 <= value <= _mask(width)):
        raise ValueError(f"field {field}={value} does not fit in {width} bits")
    return word | (value << pos)


def _extract(word, field):
    pos, width = FIELDS[field]
    return (word >> pos) & _mask(width)


def encode(mnemonic, **fields):
    """Encode an Xmlacc instruction to its 32-bit word.

    Register operands (rd, rs1, rs2, and rs3 for R4-type) are passed as keyword
    arguments holding register numbers in [0, 31]. The opcode and all funct
    fields are taken from the instruction table.
    """
    try:
        spec = BY_MNEMONIC[mnemonic]
    except KeyError:
        raise KeyError(f"unknown mnemonic {mnemonic!r}") from None

    operands = _OPERANDS[spec["fmt"]]
    missing = [o for o in operands if o not in fields]
    if missing:
        raise TypeError(f"{mnemonic} missing operand(s): {', '.join(missing)}")
    extra = [k for k in fields if k not in operands]
    if extra:
        raise TypeError(f"{mnemonic} got unexpected field(s): {', '.join(extra)}")

    word = 0
    word = _place(word, "opcode", spec["opcode"])
    word = _place(word, "funct3", spec["funct3"])
    for name in operands:
        _check_reg(name, fields[name])
        word = _place(word, name, fields[name])

    if spec["fmt"] == "R":
        word = _place(word, "funct7", spec["funct7"])
    else:  # R4
        word = _place(word, "funct2", spec["funct2"])

    return word & 0xFFFFFFFF


def decode(word):
    """Decode a 32-bit word to ``(mnemonic, fields)``.

    ``fields`` is a dict of the register operands for the matched instruction.
    Raises ValueError if the word does not match any Xmlacc instruction.
    """
    word &= 0xFFFFFFFF
    opcode = _extract(word, "opcode")
    funct3 = _extract(word, "funct3")

    for spec in INSTRUCTIONS:
        if spec["opcode"] != opcode or spec["funct3"] != funct3:
            continue
        if spec["fmt"] == "R":
            if _extract(word, "funct7") != spec["funct7"]:
                continue
        else:  # R4
            if _extract(word, "funct2") != spec["funct2"]:
                continue
        fields = {name: _extract(word, name) for name in _OPERANDS[spec["fmt"]]}
        return spec["mnemonic"], fields

    raise ValueError(
        f"word 0x{word:08x} (opcode=0x{opcode:02x} funct3=0b{funct3:03b}) "
        "matches no Xmlacc instruction"
    )
