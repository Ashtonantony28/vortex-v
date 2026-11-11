"""RV64IM + Xmlacc assembler for Vortex-V testbenches (pure Python, no cocotb).

Shared by verif/core (directed tests, co-sim), verif/random (constrained-random
generator) and anything else that needs to build programs in Python.
"""

# Parameters (must match the RTL defaults, docs/M1-INTERFACE-v1.md)
XLEN        = 64
RESET_PC    = 0x8000_0000
TOHOST_ADDR = 0x8000_1000
DATA_BASE   = 0x8000_2000
DATA_SIZE   = 0x1000        # data region [DATA_BASE, DATA_BASE+DATA_SIZE)
CODE_LIMIT  = TOHOST_ADDR   # programs at RESET_PC must end below tohost
MASK64      = (1 << 64) - 1


# ===========================================================================
# Tiny RV64I assembler (label-resolving, two-pass)
# ===========================================================================
def _u(x, bits):
    return x & ((1 << bits) - 1)


def _simm(x, bits):
    # return two's complement representation of signed x in `bits`
    return x & ((1 << bits) - 1)


def _enc_r(funct7, rs2, rs1, funct3, rd, opcode):
    return (_u(funct7, 7) << 25) | (_u(rs2, 5) << 20) | (_u(rs1, 5) << 15) \
        | (_u(funct3, 3) << 12) | (_u(rd, 5) << 7) | _u(opcode, 7)


def _enc_i(imm, rs1, funct3, rd, opcode):
    return (_simm(imm, 12) << 20) | (_u(rs1, 5) << 15) | (_u(funct3, 3) << 12) \
        | (_u(rd, 5) << 7) | _u(opcode, 7)


def _enc_i_shift(funct7_6, shamt, rs1, funct3, rd, opcode):
    # 6-bit shamt (RV64) with top 6 bits = funct7_6
    return (_u(funct7_6, 6) << 26) | (_u(shamt, 6) << 20) | (_u(rs1, 5) << 15) \
        | (_u(funct3, 3) << 12) | (_u(rd, 5) << 7) | _u(opcode, 7)


def _enc_i_shiftw(funct7, shamt, rs1, funct3, rd, opcode):
    # 5-bit shamt (*IW ops), funct7 fixed
    return (_u(funct7, 7) << 25) | (_u(shamt, 5) << 20) | (_u(rs1, 5) << 15) \
        | (_u(funct3, 3) << 12) | (_u(rd, 5) << 7) | _u(opcode, 7)


def _enc_s(imm, rs2, rs1, funct3, opcode):
    imm = _simm(imm, 12)
    hi = (imm >> 5) & 0x7F
    lo = imm & 0x1F
    return (hi << 25) | (_u(rs2, 5) << 20) | (_u(rs1, 5) << 15) \
        | (_u(funct3, 3) << 12) | (lo << 7) | _u(opcode, 7)


def _enc_b(imm, rs2, rs1, funct3, opcode):
    imm = _simm(imm, 13)
    b12 = (imm >> 12) & 1
    b11 = (imm >> 11) & 1
    b10_5 = (imm >> 5) & 0x3F
    b4_1 = (imm >> 1) & 0xF
    return (b12 << 31) | (b10_5 << 25) | (_u(rs2, 5) << 20) | (_u(rs1, 5) << 15) \
        | (_u(funct3, 3) << 12) | (b4_1 << 8) | (b11 << 7) | _u(opcode, 7)


def _enc_u(imm20, rd, opcode):
    return ((imm20 & 0xFFFFF) << 12) | (_u(rd, 5) << 7) | _u(opcode, 7)


def _enc_j(imm, rd, opcode):
    imm = _simm(imm, 21)
    b20 = (imm >> 20) & 1
    b19_12 = (imm >> 12) & 0xFF
    b11 = (imm >> 11) & 1
    b10_1 = (imm >> 1) & 0x3FF
    return (b20 << 31) | (b10_1 << 21) | (b11 << 20) | (b19_12 << 12) \
        | (_u(rd, 5) << 7) | _u(opcode, 7)


def _hilo(delta):
    """Split a PC-relative delta into (auipc hi20, addi lo12) with standard rounding."""
    hi = (delta + 0x800) >> 12
    lo = delta - (hi << 12)        # lo in [-2048, 2047]
    return hi & 0xFFFFF, lo


# opcode / funct3 / funct7 constants
OPC_LOAD, OPC_OPIMM, OPC_AUIPC, OPC_OPIMMW = 0x03, 0x13, 0x17, 0x1B
OPC_STORE, OPC_OP, OPC_LUI, OPC_OPW = 0x23, 0x33, 0x37, 0x3B
OPC_BRANCH, OPC_JALR, OPC_JAL = 0x63, 0x67, 0x6F
OPC_CUSTOM0 = 0x0B   # Xmlacc Tier-1 ML ops


def _enc_r4(rs3, funct2, rs2, rs1, funct3, rd, opcode):
    return (_u(rs3, 5) << 27) | (_u(funct2, 2) << 25) | (_u(rs2, 5) << 20) \
        | (_u(rs1, 5) << 15) | (_u(funct3, 3) << 12) | (_u(rd, 5) << 7) | _u(opcode, 7)


class Asm:
    """Two-pass label-resolving assembler for the RV64I subset we need."""

    def __init__(self, base=RESET_PC):
        self.base = base
        self.items = []    # list of ("op", args) or ("label", name)
        self.labels = {}

    # recording -----------------------------------------------------------
    def label(self, name):
        self.items.append(("label", name))
        return self

    def _emit(self, fn):
        self.items.append(("op", fn))
        return self

    # pass 1: assign addresses -------------------------------------------
    def _resolve_labels(self):
        addr = self.base
        for kind, payload in self.items:
            if kind == "label":
                self.labels[payload] = addr
            else:
                addr += 4

    def assemble(self):
        self.labels = {}
        self._resolve_labels()
        words = []
        pc = self.base
        for kind, payload in self.items:
            if kind == "label":
                continue
            words.append(payload(pc, self.labels) & 0xFFFF_FFFF)
            pc += 4
        return words

    # --- instruction helpers (each stores a closure resolved at assemble) ---
    # Register-immediate
    def addi(self, rd, rs1, imm):  return self._emit(lambda pc, L: _enc_i(imm, rs1, 0x0, rd, OPC_OPIMM))
    def slti(self, rd, rs1, imm):  return self._emit(lambda pc, L: _enc_i(imm, rs1, 0x2, rd, OPC_OPIMM))
    def sltiu(self, rd, rs1, imm): return self._emit(lambda pc, L: _enc_i(imm, rs1, 0x3, rd, OPC_OPIMM))
    def xori(self, rd, rs1, imm):  return self._emit(lambda pc, L: _enc_i(imm, rs1, 0x4, rd, OPC_OPIMM))
    def ori(self, rd, rs1, imm):   return self._emit(lambda pc, L: _enc_i(imm, rs1, 0x6, rd, OPC_OPIMM))
    def andi(self, rd, rs1, imm):  return self._emit(lambda pc, L: _enc_i(imm, rs1, 0x7, rd, OPC_OPIMM))
    def slli(self, rd, rs1, sh):   return self._emit(lambda pc, L: _enc_i_shift(0x00, sh, rs1, 0x1, rd, OPC_OPIMM))
    def srli(self, rd, rs1, sh):   return self._emit(lambda pc, L: _enc_i_shift(0x00, sh, rs1, 0x5, rd, OPC_OPIMM))
    def srai(self, rd, rs1, sh):   return self._emit(lambda pc, L: _enc_i_shift(0x10, sh, rs1, 0x5, rd, OPC_OPIMM))
    # Register-register
    def add(self, rd, rs1, rs2):   return self._emit(lambda pc, L: _enc_r(0x00, rs2, rs1, 0x0, rd, OPC_OP))
    def sub(self, rd, rs1, rs2):   return self._emit(lambda pc, L: _enc_r(0x20, rs2, rs1, 0x0, rd, OPC_OP))
    def sll(self, rd, rs1, rs2):   return self._emit(lambda pc, L: _enc_r(0x00, rs2, rs1, 0x1, rd, OPC_OP))
    def slt(self, rd, rs1, rs2):   return self._emit(lambda pc, L: _enc_r(0x00, rs2, rs1, 0x2, rd, OPC_OP))
    def sltu(self, rd, rs1, rs2):  return self._emit(lambda pc, L: _enc_r(0x00, rs2, rs1, 0x3, rd, OPC_OP))
    def xor_(self, rd, rs1, rs2):  return self._emit(lambda pc, L: _enc_r(0x00, rs2, rs1, 0x4, rd, OPC_OP))
    def srl(self, rd, rs1, rs2):   return self._emit(lambda pc, L: _enc_r(0x00, rs2, rs1, 0x5, rd, OPC_OP))
    def sra(self, rd, rs1, rs2):   return self._emit(lambda pc, L: _enc_r(0x20, rs2, rs1, 0x5, rd, OPC_OP))
    def or_(self, rd, rs1, rs2):   return self._emit(lambda pc, L: _enc_r(0x00, rs2, rs1, 0x6, rd, OPC_OP))
    def and_(self, rd, rs1, rs2):  return self._emit(lambda pc, L: _enc_r(0x00, rs2, rs1, 0x7, rd, OPC_OP))
    # *W immediate
    def addiw(self, rd, rs1, imm): return self._emit(lambda pc, L: _enc_i(imm, rs1, 0x0, rd, OPC_OPIMMW))
    def slliw(self, rd, rs1, sh):  return self._emit(lambda pc, L: _enc_i_shiftw(0x00, sh, rs1, 0x1, rd, OPC_OPIMMW))
    def srliw(self, rd, rs1, sh):  return self._emit(lambda pc, L: _enc_i_shiftw(0x00, sh, rs1, 0x5, rd, OPC_OPIMMW))
    def sraiw(self, rd, rs1, sh):  return self._emit(lambda pc, L: _enc_i_shiftw(0x20, sh, rs1, 0x5, rd, OPC_OPIMMW))
    # *W register
    def addw(self, rd, rs1, rs2):  return self._emit(lambda pc, L: _enc_r(0x00, rs2, rs1, 0x0, rd, OPC_OPW))
    def subw(self, rd, rs1, rs2):  return self._emit(lambda pc, L: _enc_r(0x20, rs2, rs1, 0x0, rd, OPC_OPW))
    def sllw(self, rd, rs1, rs2):  return self._emit(lambda pc, L: _enc_r(0x00, rs2, rs1, 0x1, rd, OPC_OPW))
    def srlw(self, rd, rs1, rs2):  return self._emit(lambda pc, L: _enc_r(0x00, rs2, rs1, 0x5, rd, OPC_OPW))
    def sraw(self, rd, rs1, rs2):  return self._emit(lambda pc, L: _enc_r(0x20, rs2, rs1, 0x5, rd, OPC_OPW))
    # RV64M (funct7 = 0x01)
    def mul(self, rd, rs1, rs2):    return self._emit(lambda pc, L: _enc_r(0x01, rs2, rs1, 0x0, rd, OPC_OP))
    def mulh(self, rd, rs1, rs2):   return self._emit(lambda pc, L: _enc_r(0x01, rs2, rs1, 0x1, rd, OPC_OP))
    def mulhsu(self, rd, rs1, rs2): return self._emit(lambda pc, L: _enc_r(0x01, rs2, rs1, 0x2, rd, OPC_OP))
    def mulhu(self, rd, rs1, rs2):  return self._emit(lambda pc, L: _enc_r(0x01, rs2, rs1, 0x3, rd, OPC_OP))
    def div(self, rd, rs1, rs2):    return self._emit(lambda pc, L: _enc_r(0x01, rs2, rs1, 0x4, rd, OPC_OP))
    def divu(self, rd, rs1, rs2):   return self._emit(lambda pc, L: _enc_r(0x01, rs2, rs1, 0x5, rd, OPC_OP))
    def rem(self, rd, rs1, rs2):    return self._emit(lambda pc, L: _enc_r(0x01, rs2, rs1, 0x6, rd, OPC_OP))
    def remu(self, rd, rs1, rs2):   return self._emit(lambda pc, L: _enc_r(0x01, rs2, rs1, 0x7, rd, OPC_OP))
    def mulw(self, rd, rs1, rs2):   return self._emit(lambda pc, L: _enc_r(0x01, rs2, rs1, 0x0, rd, OPC_OPW))
    def divw(self, rd, rs1, rs2):   return self._emit(lambda pc, L: _enc_r(0x01, rs2, rs1, 0x4, rd, OPC_OPW))
    def divuw(self, rd, rs1, rs2):  return self._emit(lambda pc, L: _enc_r(0x01, rs2, rs1, 0x5, rd, OPC_OPW))
    def remw(self, rd, rs1, rs2):   return self._emit(lambda pc, L: _enc_r(0x01, rs2, rs1, 0x6, rd, OPC_OPW))
    def remuw(self, rd, rs1, rs2):  return self._emit(lambda pc, L: _enc_r(0x01, rs2, rs1, 0x7, rd, OPC_OPW))
    # Upper immediates
    def lui(self, rd, imm20):      return self._emit(lambda pc, L: _enc_u(imm20, rd, OPC_LUI))
    def auipc(self, rd, imm20):    return self._emit(lambda pc, L: _enc_u(imm20, rd, OPC_AUIPC))
    # Loads
    def lb(self, rd, rs1, imm):    return self._emit(lambda pc, L: _enc_i(imm, rs1, 0x0, rd, OPC_LOAD))
    def lh(self, rd, rs1, imm):    return self._emit(lambda pc, L: _enc_i(imm, rs1, 0x1, rd, OPC_LOAD))
    def lw(self, rd, rs1, imm):    return self._emit(lambda pc, L: _enc_i(imm, rs1, 0x2, rd, OPC_LOAD))
    def ld(self, rd, rs1, imm):    return self._emit(lambda pc, L: _enc_i(imm, rs1, 0x3, rd, OPC_LOAD))
    def lbu(self, rd, rs1, imm):   return self._emit(lambda pc, L: _enc_i(imm, rs1, 0x4, rd, OPC_LOAD))
    def lhu(self, rd, rs1, imm):   return self._emit(lambda pc, L: _enc_i(imm, rs1, 0x5, rd, OPC_LOAD))
    def lwu(self, rd, rs1, imm):   return self._emit(lambda pc, L: _enc_i(imm, rs1, 0x6, rd, OPC_LOAD))
    # Stores
    def sb(self, rs2, rs1, imm):   return self._emit(lambda pc, L: _enc_s(imm, rs2, rs1, 0x0, OPC_STORE))
    def sh(self, rs2, rs1, imm):   return self._emit(lambda pc, L: _enc_s(imm, rs2, rs1, 0x1, OPC_STORE))
    def sw(self, rs2, rs1, imm):   return self._emit(lambda pc, L: _enc_s(imm, rs2, rs1, 0x2, OPC_STORE))
    def sd(self, rs2, rs1, imm):   return self._emit(lambda pc, L: _enc_s(imm, rs2, rs1, 0x3, OPC_STORE))
    # Branches (target = label)
    def beq(self, rs1, rs2, tgt):  return self._emit(lambda pc, L: _enc_b(L[tgt] - pc, rs2, rs1, 0x0, OPC_BRANCH))
    def bne(self, rs1, rs2, tgt):  return self._emit(lambda pc, L: _enc_b(L[tgt] - pc, rs2, rs1, 0x1, OPC_BRANCH))
    def blt(self, rs1, rs2, tgt):  return self._emit(lambda pc, L: _enc_b(L[tgt] - pc, rs2, rs1, 0x4, OPC_BRANCH))
    def bge(self, rs1, rs2, tgt):  return self._emit(lambda pc, L: _enc_b(L[tgt] - pc, rs2, rs1, 0x5, OPC_BRANCH))
    def bltu(self, rs1, rs2, tgt): return self._emit(lambda pc, L: _enc_b(L[tgt] - pc, rs2, rs1, 0x6, OPC_BRANCH))
    def bgeu(self, rs1, rs2, tgt): return self._emit(lambda pc, L: _enc_b(L[tgt] - pc, rs2, rs1, 0x7, OPC_BRANCH))
    # Jumps
    def jal(self, rd, tgt):        return self._emit(lambda pc, L: _enc_j(L[tgt] - pc, rd, OPC_JAL))
    def jalr(self, rd, rs1, imm):  return self._emit(lambda pc, L: _enc_i(imm, rs1, 0x0, rd, OPC_JALR))
    def nop(self):                 return self.addi(0, 0, 0)
    # Xmlacc Tier-1 (opcode CUSTOM0 = 0x0B)
    def dot8(self, rd, rs1, rs2):   return self._emit(lambda pc, L: _enc_r(0x00, rs2, rs1, 0x0, rd, OPC_CUSTOM0))
    def dot8u(self, rd, rs1, rs2):  return self._emit(lambda pc, L: _enc_r(0x00, rs2, rs1, 0x1, rd, OPC_CUSTOM0))
    def dot8su(self, rd, rs1, rs2): return self._emit(lambda pc, L: _enc_r(0x00, rs2, rs1, 0x2, rd, OPC_CUSTOM0))
    def macc(self, rd, rs1, rs2, rs3): return self._emit(lambda pc, L: _enc_r4(rs3, 0x0, rs2, rs1, 0x4, rd, OPC_CUSTOM0))
    def rqs(self, rd, rs1, rs2):    return self._emit(lambda pc, L: _enc_r(0x00, rs2, rs1, 0x5, rd, OPC_CUSTOM0))

    def la(self, rd, target):
        """Load a 64-bit absolute address (PC-relative via AUIPC+ADDI).

        Unlike LUI, this keeps the upper 32 bits zero when PC and target are in
        the low 2 GB window (as riscv-tests do), so addresses match the plain
        64-bit TOHOST_ADDR / data-region constants the TB uses.
        """
        self._emit(lambda pc, L: _enc_u(_hilo(target - pc)[0], rd, OPC_AUIPC))
        # second op's pc is auipc_pc + 4, so (target - (pc-4)) == original delta
        self._emit(lambda pc, L: _enc_i(_hilo(target - (pc - 4))[1], rd, 0x0, rd, OPC_OPIMM))
        return self

    def la_label(self, rd, label):
        """Like la(), but the target is a label resolved at assemble time."""
        self._emit(lambda pc, L: _enc_u(_hilo(L[label] - pc)[0], rd, OPC_AUIPC))
        self._emit(lambda pc, L: _enc_i(_hilo(L[label] - (pc - 4))[1], rd, 0x0, rd, OPC_OPIMM))
        return self
