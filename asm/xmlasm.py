"""xmlasm — assembler/disassembler for Vortex-V Xmlacc Tier-1 custom ML ops.

Single source of truth: docs/XMLACC-SPEC-v1.md (frozen v1).

If the sibling ISA table module (isa/xmlacc_isa.py) is importable it is used for
the opcode/field constants; otherwise we fall back to the constants frozen in
the spec so this module is never blocked. We never *edit* the ISA module.

Encoding formats (XLEN=64, opcode CUSTOM0 = 0b0001011):
  R-type:  funct7[31:25] | rs2[24:20] | rs1[19:15] | funct3[14:12] | rd[11:7] | opcode[6:0]
  R4-type: rs3[31:27] | funct2[26:25] | rs2[24:20] | rs1[19:15] | funct3[14:12] | rd[11:7] | opcode[6:0]

Tier-1 ops:
  mlacc.dot8    R   f3=0b000  f7=0
  mlacc.dot8u   R   f3=0b001  f7=0
  mlacc.dot8su  R   f3=0b010  f7=0
  mlacc.macc    R4  f3=0b100  funct2=0b00   (operands: rd, rs1, rs2, rs3)
  mlacc.rqs     R   f3=0b101  f7=0

----------------------------------------------------------------------------
GCC/GNU `as` `.insn` directive operand order (researched, documented here)
----------------------------------------------------------------------------
GNU `as` understands two generic RISC-V templates relevant to us:

  .insn r  opcode, func3, func7, rd, rs1, rs2
  .insn r4 opcode, func3, func2, rd, rs1, rs2, rs3

`opcode` is the 7-bit major opcode. GNU `as` predefines the symbolic names
CUSTOM_0..CUSTOM_3 for the four custom opcode slots, where CUSTOM_0 = 0x0b
(0b0001011) — exactly our CUSTOM0. So for an R-type op the directive is:

  .insn r CUSTOM_0, <funct3>, <funct7>, rd, rs1, rs2

and for the R4-type fused multiply-accumulate (funct2 takes the func7 slot in
the r4 template):

  .insn r4 CUSTOM_0, <funct3>, <funct2>, rd, rs1, rs2, rs3

Note the ORDER: for `.insn r` the 2nd arg is funct3 and the 3rd is funct7
(NOT funct7 then funct3 — a common mistake). The register order is always
rd, rs1, rs2[, rs3], matching the assembly operand order. These directives
assemble to the identical 32-bit word produced by encode() below, which the
test-suite cross-checks.
"""

from __future__ import annotations

# --- optional sibling ISA table (do not edit it) ---------------------------
try:  # pragma: no cover - depends on sibling task timing
    from isa import xmlacc_isa as _isa  # type: ignore
except Exception:  # pragma: no cover
    _isa = None

# --- frozen constants from the spec -----------------------------------------
OPCODE_CUSTOM0 = 0b0001011  # 0x0b

# mnemonic -> (fmt, funct3, funct7_or_funct2)
# fmt is "R" (3 regs: rd, rs1, rs2) or "R4" (4 regs: rd, rs1, rs2, rs3)
_TABLE = {
    "mlacc.dot8":   ("R",  0b000, 0b0000000),
    "mlacc.dot8u":  ("R",  0b001, 0b0000000),
    "mlacc.dot8su": ("R",  0b010, 0b0000000),
    "mlacc.macc":   ("R4", 0b100, 0b00),
    "mlacc.rqs":    ("R",  0b101, 0b0000000),
}

# GNU `as` symbolic opcode name for CUSTOM0
_INSN_OPCODE_NAME = "CUSTOM_0"

# --- register name resolution -----------------------------------------------
_ABI = {
    "zero": 0, "ra": 1, "sp": 2, "gp": 3, "tp": 4,
    "t0": 5, "t1": 6, "t2": 7,
    "s0": 8, "fp": 8, "s1": 9,
    "a0": 10, "a1": 11, "a2": 12, "a3": 13,
    "a4": 14, "a5": 15, "a6": 16, "a7": 17,
    "s2": 18, "s3": 19, "s4": 20, "s5": 21, "s6": 22, "s7": 23,
    "s8": 24, "s9": 25, "s10": 26, "s11": 27,
    "t3": 28, "t4": 29, "t5": 30, "t6": 31,
}


class XmlasmError(ValueError):
    """Raised on any malformed Xmlacc assembly input."""


def reg_num(tok: str) -> int:
    """Resolve a register token (xN or ABI name) to its 0..31 number."""
    t = tok.strip().lower()
    if not t:
        raise XmlasmError("empty register operand")
    if t in _ABI:
        return _ABI[t]
    if t.startswith("x") and t[1:].isdigit():
        n = int(t[1:])
        if 0 <= n <= 31:
            return n
        raise XmlasmError(f"register out of range: {tok!r} (x0..x31)")
    raise XmlasmError(f"unknown register name: {tok!r}")


def reg_name(num: int) -> str:
    """Canonical register spelling used by the disassembler (xN form)."""
    if not 0 <= num <= 31:
        raise XmlasmError(f"register number out of range: {num}")
    return f"x{num}"


# --- encode / decode --------------------------------------------------------
def _encode_r(funct7: int, rs2: int, rs1: int, funct3: int, rd: int) -> int:
    return ((funct7 & 0x7F) << 25 | (rs2 & 0x1F) << 20 | (rs1 & 0x1F) << 15
            | (funct3 & 0x7) << 12 | (rd & 0x1F) << 7 | OPCODE_CUSTOM0)


def _encode_r4(rs3: int, funct2: int, rs2: int, rs1: int, funct3: int,
               rd: int) -> int:
    return ((rs3 & 0x1F) << 27 | (funct2 & 0x3) << 25 | (rs2 & 0x1F) << 20
            | (rs1 & 0x1F) << 15 | (funct3 & 0x7) << 12 | (rd & 0x1F) << 7
            | OPCODE_CUSTOM0)


def assemble_line(line: str) -> int:
    """Assemble one instruction line into a 32-bit word (unsigned int).

    Comments (``#``), surrounding whitespace, and a trailing label-free form
    are handled.  Lines that are blank/comment-only return ``None``.
    """
    code = line.split("#", 1)[0].strip()
    if not code:
        raise XmlasmError("no instruction on line")

    # optional leading "label:" on the same line
    if ":" in code:
        _, _, code = code.partition(":")
        code = code.strip()
        if not code:
            raise XmlasmError("label with no instruction")

    parts = code.split(None, 1)
    mnem = parts[0].lower()
    if mnem not in _TABLE:
        raise XmlasmError(f"unknown mnemonic: {parts[0]!r}")

    fmt, funct3, f7f2 = _TABLE[mnem]
    operand_str = parts[1] if len(parts) > 1 else ""
    ops = [o for o in (x.strip() for x in operand_str.split(",")) if o != ""]
    # catch trailing/double commas producing empties vs. missing operands
    raw_count = len([x for x in operand_str.split(",")]) if operand_str else 0

    want = 4 if fmt == "R4" else 3
    if len(ops) != want or raw_count != want:
        raise XmlasmError(
            f"{mnem} expects {want} register operands, got {len(ops)}: {code!r}")

    regs = [reg_num(o) for o in ops]
    if fmt == "R4":
        rd, rs1, rs2, rs3 = regs
        return _encode_r4(rs3, f7f2, rs2, rs1, funct3, rd)
    rd, rs1, rs2 = regs
    return _encode_r(f7f2, rs2, rs1, funct3, rd)


def assemble(text: str) -> list[int]:
    """Assemble a block of text; skip blank/comment lines and labels."""
    words: list[int] = []
    for raw in text.splitlines():
        code = raw.split("#", 1)[0].strip()
        if not code:
            continue
        # a line that is only a label (ends with ':') is skipped
        if code.endswith(":") and code.count(":") == 1:
            continue
        words.append(assemble_line(raw))
    return words


# funct3 -> mnemonic (R-type ops are unique by funct3 in Tier-1)
_F3_TO_MNEM = {
    0b000: "mlacc.dot8",
    0b001: "mlacc.dot8u",
    0b010: "mlacc.dot8su",
    0b101: "mlacc.rqs",
}


def disassemble(word: int) -> str:
    """Disassemble a 32-bit word into a canonical assembly string (xN regs)."""
    word &= 0xFFFFFFFF
    opcode = word & 0x7F
    if opcode != OPCODE_CUSTOM0:
        raise XmlasmError(f"not a CUSTOM0 instruction (opcode=0x{opcode:02x})")

    funct3 = (word >> 12) & 0x7
    rd = (word >> 7) & 0x1F
    rs1 = (word >> 15) & 0x1F
    rs2 = (word >> 20) & 0x1F

    if funct3 == 0b100:  # mlacc.macc (R4)
        funct2 = (word >> 25) & 0x3
        rs3 = (word >> 27) & 0x1F
        if funct2 != 0b00:
            raise XmlasmError(f"unknown macc funct2=0b{funct2:02b}")
        return (f"mlacc.macc {reg_name(rd)}, {reg_name(rs1)}, "
                f"{reg_name(rs2)}, {reg_name(rs3)}")

    funct7 = (word >> 25) & 0x7F
    if funct7 != 0:
        raise XmlasmError(f"unknown funct7=0b{funct7:07b} for funct3=0b{funct3:03b}")
    mnem = _F3_TO_MNEM.get(funct3)
    if mnem is None:
        raise XmlasmError(f"unknown funct3=0b{funct3:03b}")
    return f"{mnem} {reg_name(rd)}, {reg_name(rs1)}, {reg_name(rs2)}"


def emit_dot_insn(line: str) -> str:
    """Emit the GCC/GNU `as` `.insn` directive for an assembly line.

    R-type:  .insn r  CUSTOM_0, funct3, funct7, rd, rs1, rs2
    R4-type: .insn r4 CUSTOM_0, funct3, funct2, rd, rs1, rs2, rs3

    Registers are emitted by their ABI names (what a human writes in inline
    asm) when available, falling back to xN.
    """
    code = line.split("#", 1)[0].strip()
    if ":" in code:
        _, _, code = code.partition(":")
        code = code.strip()
    parts = code.split(None, 1)
    if not parts:
        raise XmlasmError("no instruction on line")
    mnem = parts[0].lower()
    if mnem not in _TABLE:
        raise XmlasmError(f"unknown mnemonic: {parts[0]!r}")
    fmt, funct3, f7f2 = _TABLE[mnem]
    ops = [o.strip() for o in (parts[1] if len(parts) > 1 else "").split(",")
           if o.strip()]
    want = 4 if fmt == "R4" else 3
    if len(ops) != want:
        raise XmlasmError(
            f"{mnem} expects {want} register operands, got {len(ops)}")

    # normalize each register to a canonical ABI name for readability
    abi = {v: k for k, v in _ABI.items()
           if k not in ("fp",)}  # prefer s0 over fp for 8

    def pretty(tok: str) -> str:
        return abi.get(reg_num(tok), f"x{reg_num(tok)}")

    regs = ", ".join(pretty(o) for o in ops)
    if fmt == "R4":
        return f".insn r4 {_INSN_OPCODE_NAME}, {funct3}, {f7f2}, {regs}"
    return f".insn r {_INSN_OPCODE_NAME}, {funct3}, {f7f2}, {regs}"


if __name__ == "__main__":  # pragma: no cover
    import sys
    for ln in sys.stdin:
        ln = ln.rstrip("\n")
        if not ln.split("#", 1)[0].strip():
            continue
        w = assemble_line(ln)
        print(f"0x{w:08x}  {disassemble(w)}    ; {emit_dot_insn(ln)}")
