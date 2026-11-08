"""Vortex-V RV64IM instruction-set simulator (ISS).

Pure-Python golden model / co-simulation oracle for the Vortex-V out-of-order
RV64 core.  Implements the RV64I base integer ISA plus the RV64M extension:

  * single hart, machine mode, little-endian
  * 32 x-registers (x0 hardwired to 0), 64-bit pc
  * all RV64I instructions including the RV64 *W ops
  * RV64M integer multiply/divide (M1.1 part A):
      MUL MULH MULHSU MULHU DIV DIVU REM REMU (64-bit, OP, funct7=0000001)
      MULW DIVW DIVUW REMW REMUW (32-bit, OP-32, funct7=0000001)
    Division-by-zero and signed-overflow follow the RISC-V spec (no traps).
  * FENCE / FENCE.I treated as no-ops
  * ECALL sets a halted flag
  * a store to TOHOST_ADDR (0x8000_1000) halts the run (value 1 == PASS)

No C (compressed) extension, no CSRs beyond the trivial ecall-halt, no
traps/interrupts.  Dependency-light: numpy is NOT required.

Frozen contract: docs/M1-INTERFACE-v1.md, section "Golden model contract".
RV64M is the M1.1 follow-up named as out-of-scope by the frozen M1 contract.

``step()`` returns a record dict:
    {
        "pc":        <pc of the instruction that was executed>,
        "inst":      <raw 32-bit instruction word>,
        "rd":        <destination register index, or None>,
        "rd_val":    <64-bit value written to rd (0 / unchanged for x0)>,
        "mem_write": (addr, size, data) | None,
        "halted":    <bool, hart halted after this instruction>,
    }
"""

from __future__ import annotations

# --- 64-bit two's-complement helpers -------------------------------------

MASK64 = (1 << 64) - 1
MASK32 = (1 << 32) - 1
MASK128 = (1 << 128) - 1

RESET_PC = 0x8000_0000
TOHOST_ADDR = 0x8000_1000


def u64(x: int) -> int:
    """Mask an arbitrary Python int to an unsigned 64-bit value."""
    return x & MASK64


def to_signed(x: int, bits: int = 64) -> int:
    """Interpret the low `bits` of x as a two's-complement signed integer."""
    x &= (1 << bits) - 1
    sign = 1 << (bits - 1)
    return (x ^ sign) - sign


def sext(x: int, bits: int) -> int:
    """Sign-extend the low `bits` of x to a full (unsigned-masked) 64-bit value."""
    return u64(to_signed(x, bits))


# --- Xmlacc (custom ML extension, opcode CUSTOM0) helpers ------------------
# Mirrors model/xmlacc_model.py exactly (pure Python; keeps the ISS numpy-free).
# A GP register packs 8 int8 lanes, lane i = bits [8i+7:8i] (byte 0 = [7:0]).

XMLACC_NLANES = 8


def _xm_lanes_signed(reg: int) -> list[int]:
    u = u64(reg)
    out = []
    for i in range(XMLACC_NLANES):
        b = (u >> (8 * i)) & 0xFF
        out.append(b - 256 if b >= 128 else b)
    return out


def _xm_lanes_unsigned(reg: int) -> list[int]:
    u = u64(reg)
    return [(u >> (8 * i)) & 0xFF for i in range(XMLACC_NLANES)]


def _xm_dot(a_lanes: list[int], b_lanes: list[int]) -> int:
    """Exact int dot product of two 8-element lane lists (Python int)."""
    return sum(x * y for x, y in zip(a_lanes, b_lanes))


# --- Memory ----------------------------------------------------------------

class Memory:
    """Byte-addressable little-endian memory.

    Backed by a flat ``bytearray`` that grows on demand.  Addresses are the
    raw 64-bit addresses used by the program (e.g. 0x8000_0000); an internal
    base offsets them into the bytearray so we do not allocate the low 2 GiB.
    """

    def __init__(self, base: int = RESET_PC, size: int = 1 << 20):
        self.base = base
        self._data = bytearray(size)

    # -- internal growth ---------------------------------------------------
    def _ensure(self, off: int, n: int) -> None:
        need = off + n
        if need > len(self._data):
            # grow generously so repeated stores do not thrash
            newlen = max(need, len(self._data) * 2)
            self._data.extend(b"\x00" * (newlen - len(self._data)))

    def _off(self, addr: int) -> int:
        off = (addr - self.base) & MASK64
        if off < 0 or off >= (1 << 40):
            raise ValueError(f"address 0x{addr:016x} out of modelled range")
        return off

    # -- image loading -----------------------------------------------------
    def load_image(self, image, base: int | None = None) -> None:
        """Load a flat image at ``base`` (default self.base).

        ``image`` may be bytes/bytearray, or a dict mapping absolute
        addresses to byte values.
        """
        if base is None:
            base = self.base
        if isinstance(image, dict):
            for addr, byte in image.items():
                self.store(addr, 1, byte & 0xFF)
        else:
            off = self._off(base)
            self._ensure(off, len(image))
            self._data[off:off + len(image)] = bytes(image)

    def load_words(self, words, base: int | None = None) -> None:
        """Load a list of 32-bit words as a little-endian image at ``base``."""
        if base is None:
            base = self.base
        for i, w in enumerate(words):
            self.store(base + 4 * i, 4, w & MASK32)

    # -- access ------------------------------------------------------------
    def load(self, addr: int, size: int) -> int:
        """Load ``size`` (1/2/4/8) bytes, little-endian, returned unsigned."""
        off = self._off(addr)
        self._ensure(off, size)
        val = 0
        for i in range(size):
            val |= self._data[off + i] << (8 * i)
        return val

    def store(self, addr: int, size: int, data: int) -> None:
        """Store the low ``size`` bytes of ``data``, little-endian."""
        off = self._off(addr)
        self._ensure(off, size)
        data &= (1 << (8 * size)) - 1
        for i in range(size):
            self._data[off + i] = (data >> (8 * i)) & 0xFF

    def fetch(self, addr: int) -> int:
        """Fetch a 32-bit instruction word (little-endian)."""
        return self.load(addr, 4)


# --- Hart ------------------------------------------------------------------

# load funct3 -> (size_bytes, signed)
_LOADS = {
    0b000: (1, True),   # LB
    0b001: (2, True),   # LH
    0b010: (4, True),   # LW
    0b011: (8, False),  # LD  (full width, sign irrelevant)
    0b100: (1, False),  # LBU
    0b101: (2, False),  # LHU
    0b110: (4, False),  # LWU
}

# store funct3 -> size_bytes
_STORES = {0b000: 1, 0b001: 2, 0b010: 4, 0b011: 8}


class Hart:
    """A single RV64I hart (machine mode)."""

    def __init__(self, mem: Memory | None = None, reset_pc: int = RESET_PC,
                 tohost_addr: int = TOHOST_ADDR, ecall_halts: bool = True):
        self.mem = mem if mem is not None else Memory(base=reset_pc)
        self.reset_pc = reset_pc
        self.tohost_addr = tohost_addr
        self.ecall_halts = ecall_halts
        self.reset()

    def reset(self) -> None:
        self.x = [0] * 32
        self.pc = self.reset_pc & MASK64
        self.halted = False
        self.halt_code = None  # None, or (pass:bool, code:int)
        self.inst_count = 0
        # Xmlacc custom CSRs: mlcfg (0x800, RAZ/WI) and mlstat (0x801).
        # mlstat bit0 = SAT sticky, set by mlacc.rqs clamp, write-1-to-clear.
        self.mlstat = 0

    @property
    def mlstat_sat(self) -> bool:
        return bool(self.mlstat & 1)

    # -- register file -----------------------------------------------------
    def rd_reg(self, i: int) -> int:
        return 0 if i == 0 else self.x[i]

    def wr_reg(self, i: int, val: int) -> None:
        if i != 0:
            self.x[i] = u64(val)

    # -- decode helpers ----------------------------------------------------
    @staticmethod
    def _imm_i(inst: int) -> int:
        return to_signed(inst >> 20, 12)

    @staticmethod
    def _imm_s(inst: int) -> int:
        imm = ((inst >> 25) << 5) | ((inst >> 7) & 0x1F)
        return to_signed(imm, 12)

    @staticmethod
    def _imm_b(inst: int) -> int:
        imm = (((inst >> 31) & 1) << 12) | (((inst >> 7) & 1) << 11) \
            | (((inst >> 25) & 0x3F) << 5) | (((inst >> 8) & 0xF) << 1)
        return to_signed(imm, 13)

    @staticmethod
    def _imm_u(inst: int) -> int:
        return to_signed(inst & 0xFFFF_F000, 32)

    @staticmethod
    def _imm_j(inst: int) -> int:
        imm = (((inst >> 31) & 1) << 20) | (((inst >> 12) & 0xFF) << 12) \
            | (((inst >> 20) & 1) << 11) | (((inst >> 21) & 0x3FF) << 1)
        return to_signed(imm, 21)

    # -- RV64M multiply / divide -------------------------------------------
    @staticmethod
    def _sdiv(a: int, b: int, bits: int) -> int:
        """Signed division, round toward zero. a, b are signed Python ints.

        RISC-V spec: divide-by-zero -> -1 (all ones); signed overflow
        (most-negative / -1) -> the dividend. Returns an unsigned bit pattern.
        """
        if b == 0:
            return MASK64  # all ones; W callers truncate+sign-extend low 32
        if a == -(1 << (bits - 1)) and b == -1:
            return u64(a)  # overflow: quotient = dividend
        q = abs(a) // abs(b)
        if (a < 0) ^ (b < 0):
            q = -q
        return u64(q)

    @staticmethod
    def _srem(a: int, b: int, bits: int) -> int:
        """Signed remainder, sign of dividend.

        divide-by-zero -> dividend; signed overflow -> 0.
        """
        if b == 0:
            return u64(a)  # dividend
        if a == -(1 << (bits - 1)) and b == -1:
            return 0
        r = abs(a) % abs(b)
        if a < 0:
            r = -r
        return u64(r)

    @staticmethod
    def _udiv(a: int, b: int) -> int:
        """Unsigned division. divide-by-zero -> all ones."""
        if b == 0:
            return MASK64
        return u64(a // b)

    @staticmethod
    def _urem(a: int, b: int) -> int:
        """Unsigned remainder. divide-by-zero -> dividend."""
        if b == 0:
            return u64(a)
        return u64(a % b)

    def _muldiv(self, funct3: int, a: int, b: int, word: bool) -> int:
        """Evaluate an RV64M op. a, b are unsigned 64-bit register values."""
        if word:
            # *W variants: operate on the low 32 bits, sign-extend to 64.
            if funct3 == 0b000:   # MULW
                res = u64((a & MASK32) * (b & MASK32)) & MASK32
                return sext(res, 32)
            sa = to_signed(a & MASK32, 32)
            sb = to_signed(b & MASK32, 32)
            ua = a & MASK32
            ub = b & MASK32
            if funct3 == 0b100:    # DIVW
                res = self._sdiv(sa, sb, 32)
            elif funct3 == 0b101:  # DIVUW
                res = self._udiv(ua, ub)
            elif funct3 == 0b110:  # REMW
                res = self._srem(sa, sb, 32)
            elif funct3 == 0b111:  # REMUW
                res = self._urem(ua, ub)
            else:
                raise ValueError(f"illegal OP-32 M funct3: {funct3:#05b}")
            return sext(res & MASK32, 32)

        # 64-bit variants
        if funct3 == 0b000:    # MUL  (low 64 bits)
            return u64(a * b)
        elif funct3 == 0b001:  # MULH  (signed x signed, high 64)
            return ((to_signed(a) * to_signed(b)) & MASK128) >> 64
        elif funct3 == 0b010:  # MULHSU (signed rs1 x unsigned rs2, high 64)
            return ((to_signed(a) * b) & MASK128) >> 64
        elif funct3 == 0b011:  # MULHU (unsigned x unsigned, high 64)
            return ((a * b) & MASK128) >> 64
        elif funct3 == 0b100:  # DIV
            return self._sdiv(to_signed(a), to_signed(b), 64)
        elif funct3 == 0b101:  # DIVU
            return self._udiv(a, b)
        elif funct3 == 0b110:  # REM
            return self._srem(to_signed(a), to_signed(b), 64)
        else:                  # funct3 == 0b111, REMU
            return self._urem(a, b)

    # -- one step ----------------------------------------------------------
    def step(self) -> dict:
        if self.halted:
            return {"pc": self.pc, "inst": 0, "rd": None, "rd_val": 0,
                    "mem_write": None, "halted": True}

        pc = self.pc & MASK64
        inst = self.mem.fetch(pc)

        rec = {"pc": pc, "inst": inst, "rd": None, "rd_val": 0,
               "mem_write": None, "halted": False}

        opcode = inst & 0x7F
        rd = (inst >> 7) & 0x1F
        funct3 = (inst >> 12) & 0x7
        rs1 = (inst >> 15) & 0x1F
        rs2 = (inst >> 20) & 0x1F
        funct7 = (inst >> 25) & 0x7F

        next_pc = u64(pc + 4)
        wrote_rd = False
        rd_val = 0

        if opcode == 0b0110111:  # LUI
            rd_val = u64(self._imm_u(inst))
            wrote_rd = True

        elif opcode == 0b0010111:  # AUIPC
            rd_val = u64(pc + self._imm_u(inst))
            wrote_rd = True

        elif opcode == 0b1101111:  # JAL
            rd_val = next_pc
            wrote_rd = True
            next_pc = u64(pc + self._imm_j(inst))

        elif opcode == 0b1100111:  # JALR
            if funct3 != 0:
                raise ValueError(f"illegal JALR funct3 at 0x{pc:016x}: 0x{inst:08x}")
            target = u64(self.rd_reg(rs1) + self._imm_i(inst)) & ~1  # clear lsb
            rd_val = next_pc
            wrote_rd = True
            next_pc = target

        elif opcode == 0b1100011:  # branches
            a = self.rd_reg(rs1)
            b = self.rd_reg(rs2)
            taken = False
            if funct3 == 0b000:   # BEQ
                taken = a == b
            elif funct3 == 0b001:  # BNE
                taken = a != b
            elif funct3 == 0b100:  # BLT
                taken = to_signed(a) < to_signed(b)
            elif funct3 == 0b101:  # BGE
                taken = to_signed(a) >= to_signed(b)
            elif funct3 == 0b110:  # BLTU
                taken = a < b
            elif funct3 == 0b111:  # BGEU
                taken = a >= b
            else:
                raise ValueError(f"illegal branch funct3 at 0x{pc:016x}: 0x{inst:08x}")
            if taken:
                next_pc = u64(pc + self._imm_b(inst))

        elif opcode == 0b0000011:  # loads
            if funct3 not in _LOADS:
                raise ValueError(f"illegal load funct3 at 0x{pc:016x}: 0x{inst:08x}")
            size, signed = _LOADS[funct3]
            addr = u64(self.rd_reg(rs1) + self._imm_i(inst))
            raw = self.mem.load(addr, size)
            rd_val = sext(raw, size * 8) if signed else u64(raw)
            wrote_rd = True

        elif opcode == 0b0100011:  # stores
            if funct3 not in _STORES:
                raise ValueError(f"illegal store funct3 at 0x{pc:016x}: 0x{inst:08x}")
            size = _STORES[funct3]
            addr = u64(self.rd_reg(rs1) + self._imm_s(inst))
            data = self.rd_reg(rs2) & ((1 << (8 * size)) - 1)
            self.mem.store(addr, size, data)
            rec["mem_write"] = (addr, size, data)
            # tohost watcher
            if addr == self.tohost_addr:
                self.halted = True
                val = data
                self.halt_code = (val == 1, val >> 1)
                rec["halted"] = True

        elif opcode == 0b0010011:  # OP-IMM (64-bit)
            a = self.rd_reg(rs1)
            imm = self._imm_i(inst)
            if funct3 == 0b000:    # ADDI
                rd_val = u64(a + imm)
            elif funct3 == 0b010:  # SLTI
                rd_val = 1 if to_signed(a) < imm else 0
            elif funct3 == 0b011:  # SLTIU
                rd_val = 1 if a < u64(imm) else 0
            elif funct3 == 0b100:  # XORI
                rd_val = u64(a ^ u64(imm))
            elif funct3 == 0b110:  # ORI
                rd_val = u64(a | u64(imm))
            elif funct3 == 0b111:  # ANDI
                rd_val = u64(a & u64(imm))
            elif funct3 == 0b001:  # SLLI (RV64: 6-bit shamt)
                shamt = (inst >> 20) & 0x3F
                if (inst >> 26) != 0b000000:
                    raise ValueError(f"illegal SLLI at 0x{pc:016x}: 0x{inst:08x}")
                rd_val = u64(a << shamt)
            elif funct3 == 0b101:  # SRLI / SRAI (RV64: 6-bit shamt)
                shamt = (inst >> 20) & 0x3F
                top = inst >> 26
                if top == 0b000000:    # SRLI
                    rd_val = a >> shamt
                elif top == 0b010000:  # SRAI
                    rd_val = u64(to_signed(a) >> shamt)
                else:
                    raise ValueError(f"illegal SRLI/SRAI at 0x{pc:016x}: 0x{inst:08x}")
            wrote_rd = True

        elif opcode == 0b0110011:  # OP (64-bit register-register)
            a = self.rd_reg(rs1)
            b = self.rd_reg(rs2)
            if funct7 == 0b0000001:  # RV64M mul/div
                rd_val = self._muldiv(funct3, a, b, word=False)
            elif funct3 == 0b000:
                rd_val = u64(a - b) if funct7 == 0b0100000 else u64(a + b)  # SUB/ADD
            elif funct3 == 0b001:  # SLL
                rd_val = u64(a << (b & 0x3F))
            elif funct3 == 0b010:  # SLT
                rd_val = 1 if to_signed(a) < to_signed(b) else 0
            elif funct3 == 0b011:  # SLTU
                rd_val = 1 if a < b else 0
            elif funct3 == 0b100:  # XOR
                rd_val = u64(a ^ b)
            elif funct3 == 0b101:  # SRL / SRA
                if funct7 == 0b0100000:   # SRA
                    rd_val = u64(to_signed(a) >> (b & 0x3F))
                else:                     # SRL
                    rd_val = a >> (b & 0x3F)
            elif funct3 == 0b110:  # OR
                rd_val = u64(a | b)
            elif funct3 == 0b111:  # AND
                rd_val = u64(a & b)
            wrote_rd = True

        elif opcode == 0b0011011:  # OP-IMM-32 (W ops)
            a = self.rd_reg(rs1)
            imm = self._imm_i(inst)
            if funct3 == 0b000:    # ADDIW
                rd_val = sext(u64(a + imm) & MASK32, 32)
            elif funct3 == 0b001:  # SLLIW
                shamt = (inst >> 20) & 0x1F
                if (inst >> 25) != 0b0000000:
                    raise ValueError(f"illegal SLLIW at 0x{pc:016x}: 0x{inst:08x}")
                rd_val = sext((a << shamt) & MASK32, 32)
            elif funct3 == 0b101:  # SRLIW / SRAIW (5-bit shamt, op on 32 bits)
                shamt = (inst >> 20) & 0x1F
                top = inst >> 25
                a32 = a & MASK32
                if top == 0b0000000:    # SRLIW
                    rd_val = sext(a32 >> shamt, 32)
                elif top == 0b0100000:  # SRAIW
                    rd_val = sext(u64(to_signed(a32, 32) >> shamt) & MASK32, 32)
                else:
                    raise ValueError(f"illegal SRLIW/SRAIW at 0x{pc:016x}: 0x{inst:08x}")
            else:
                raise ValueError(f"illegal OP-IMM-32 funct3 at 0x{pc:016x}: 0x{inst:08x}")
            wrote_rd = True

        elif opcode == 0b0111011:  # OP-32 (W ops, register-register)
            a = self.rd_reg(rs1)
            b = self.rd_reg(rs2)
            a32 = a & MASK32
            b32 = b & MASK32
            if funct7 == 0b0000001:  # RV64M *W mul/div
                if funct3 not in (0b000, 0b100, 0b101, 0b110, 0b111):
                    raise ValueError(f"illegal OP-32 M funct3 at 0x{pc:016x}: 0x{inst:08x}")
                rd_val = self._muldiv(funct3, a, b, word=True)
            elif funct3 == 0b000:    # ADDW / SUBW
                res = (a32 - b32) if funct7 == 0b0100000 else (a32 + b32)
                rd_val = sext(res & MASK32, 32)
            elif funct3 == 0b001:  # SLLW
                rd_val = sext((a32 << (b & 0x1F)) & MASK32, 32)
            elif funct3 == 0b101:  # SRLW / SRAW
                if funct7 == 0b0100000:   # SRAW
                    rd_val = sext(u64(to_signed(a32, 32) >> (b & 0x1F)) & MASK32, 32)
                else:                     # SRLW
                    rd_val = sext(a32 >> (b & 0x1F), 32)
            else:
                raise ValueError(f"illegal OP-32 funct3 at 0x{pc:016x}: 0x{inst:08x}")
            wrote_rd = True

        elif opcode == 0b0001011:  # CUSTOM0 — Xmlacc Tier-1 ML ops
            a = self.rd_reg(rs1)
            b = self.rd_reg(rs2)
            funct2 = (inst >> 25) & 0x3
            if funct3 == 0b000 and funct7 == 0b0000000:      # mlacc.dot8
                acc = _xm_dot(_xm_lanes_signed(a), _xm_lanes_signed(b))
                rd_val = u64(acc)
            elif funct3 == 0b001 and funct7 == 0b0000000:    # mlacc.dot8u
                acc = _xm_dot(_xm_lanes_unsigned(a), _xm_lanes_unsigned(b))
                rd_val = u64(acc)
            elif funct3 == 0b010 and funct7 == 0b0000000:    # mlacc.dot8su
                acc = _xm_dot(_xm_lanes_signed(a), _xm_lanes_unsigned(b))
                rd_val = u64(acc)
            elif funct3 == 0b100 and funct2 == 0b00:         # mlacc.macc (R4)
                rs3 = (inst >> 27) & 0x1F
                c = self.rd_reg(rs3)
                acc = _xm_dot(_xm_lanes_signed(a), _xm_lanes_signed(b))
                rd_val = u64(to_signed(c) + acc)
            elif funct3 == 0b101 and funct7 == 0b0000000:    # mlacc.rqs
                sh = b & 0x3F
                x = to_signed(a)
                rnd = (1 << (sh - 1)) if sh > 0 else 0
                t = (x + rnd) >> sh  # Python >> on signed int is arithmetic (ASR)
                if t < -128:
                    clamped, sat = -128, True
                elif t > 127:
                    clamped, sat = 127, True
                else:
                    clamped, sat = t, False
                if sat:
                    self.mlstat |= 1  # SAT sticky
                rd_val = u64(clamped)
            else:
                raise ValueError(
                    f"illegal CUSTOM0/Xmlacc inst at 0x{pc:016x}: 0x{inst:08x}")
            wrote_rd = True

        elif opcode == 0b0001111:  # FENCE / FENCE.I -> no-op
            pass

        elif opcode == 0b1110011:  # SYSTEM (ECALL only, in M1)
            imm12 = inst >> 20
            if imm12 == 0 and funct3 == 0:  # ECALL
                if self.ecall_halts:
                    self.halted = True
                    self.halt_code = (True, 0)
                    rec["halted"] = True
            elif imm12 == 1 and funct3 == 0:  # EBREAK -> halt too
                if self.ecall_halts:
                    self.halted = True
                    self.halt_code = (True, 0)
                    rec["halted"] = True
            elif funct3 in (0b001, 0b010, 0b011, 0b101, 0b110, 0b111):
                # Zicsr — minimal support for the Xmlacc custom CSRs only:
                #   0x800 mlcfg  : RAZ/WI (reads 0, writes ignored)
                #   0x801 mlstat : bit0 = SAT sticky, write-1-to-clear (W1C)
                csr = imm12 & 0xFFF
                if csr not in (0x800, 0x801):
                    raise ValueError(
                        f"unsupported CSR 0x{csr:03x} at 0x{pc:016x}: 0x{inst:08x}")
                # Source operand: register (funct3 0b0xx) or 5-bit zimm (0b1xx).
                if funct3 & 0b100:
                    src = rs1  # zimm = inst[19:15]
                else:
                    src = self.rd_reg(rs1)
                # Read old value.
                old = self.mlstat if csr == 0x801 else 0
                # A write happens for CSRRW always; for CSRRS/CSRRC only when the
                # source operand register/zimm is non-zero.
                op = funct3 & 0b011
                does_write = (op == 0b001) or (src != 0)
                if does_write and csr == 0x801:
                    if op == 0b001:    # CSRRW / CSRRWI : write-1-to-clear
                        self.mlstat &= ~(u64(src)) & MASK64
                    elif op == 0b010:  # CSRRS / CSRRSI : set-bits are W1C here
                        self.mlstat &= ~(u64(src)) & MASK64
                    elif op == 0b011:  # CSRRC / CSRRCI : clear-bits -> no-op on W1C
                        pass
                # mlcfg (0x800) ignores all writes (WI).
                rd_val = u64(old)
                wrote_rd = True
            else:
                raise ValueError(f"unsupported SYSTEM inst at 0x{pc:016x}: 0x{inst:08x}")

        else:
            raise ValueError(f"illegal opcode at 0x{pc:016x}: 0x{inst:08x}")

        if wrote_rd:
            rec["rd"] = rd
            if rd == 0:
                rec["rd_val"] = 0  # x0: no architectural change
            else:
                self.wr_reg(rd, rd_val)
                rec["rd_val"] = u64(rd_val)

        self.pc = next_pc & MASK64
        self.inst_count += 1
        return rec

    # -- run helper --------------------------------------------------------
    def run(self, max_steps: int = 1_000_000) -> dict:
        """Run until halted or max_steps reached.

        Returns a summary dict: {steps, halted, halt_code, pc}.
        """
        steps = 0
        while not self.halted and steps < max_steps:
            self.step()
            steps += 1
        return {"steps": steps, "halted": self.halted,
                "halt_code": self.halt_code, "pc": self.pc}
