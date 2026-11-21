"""Seeded constrained-random RV64IM + Xmlacc program generator (ADR 0006).

Stands in for riscv-dv until it is installed. Every program:

- terminates by construction: branches and jumps only go forward, and loops are
  bounded by a counter register (x29) that random code never writes;
- only touches memory inside the data region, with naturally aligned accesses;
- ends with a PASS store to tohost.

Register roles: x1..x27 random sources/destinations (plus occasional x0
destinations), x28 scratch for JALR targets, x29 loop counter, x30 data base,
x31 tohost pointer.

With irq=True (ADR 0015) the program also takes timer interrupts: a prologue
enables them and arms the core harness's MTIMECMP, and a handler after the
tohost store re-arms it a pseudo-random number of cycles ahead (from mcycle)
and counts interrupts. x27 is then the handler's own register, so random code
never uses it; the handler saves and restores everything else it touches.

The same seed always yields the same program, so a failing seed reproduces.
"""
import random
from collections import Counter

from rvasm import (Asm, CODE_LIMIT, DATA_BASE, DATA_SIZE, MASK64, MTIMECMP_ADDR, RESET_PC,
                   TOHOST_ADDR)

POOL = list(range(1, 28))
R_SCRATCH, R_LOOP, R_BASE, R_TOHOST = 28, 29, 30, 31
MEM_SPAN = 2048          # load/store offsets stay within a 12-bit signed immediate
assert MEM_SPAN <= DATA_SIZE
ALIAS_P = 0.4            # chance a load/store reuses a recent store's doubleword
RECENT_STORES = 4
MAX_WORDS = (CODE_LIMIT - RESET_PC) // 4

ALU_RR = ["add", "sub", "sll", "slt", "sltu", "xor_", "srl", "sra", "or_", "and_",
          "addw", "subw", "sllw", "srlw", "sraw"]
ALU_RI = ["addi", "slti", "sltiu", "xori", "ori", "andi", "addiw"]
SHIFT_I = ["slli", "srli", "srai"]
SHIFT_IW = ["slliw", "srliw", "sraiw"]
MULDIV = ["mul", "mulh", "mulhsu", "mulhu", "div", "divu", "rem", "remu",
          "mulw", "divw", "divuw", "remw", "remuw"]
XMLACC_RR = ["dot8", "dot8u", "dot8su", "rqs"]
LOADS = {"lb": 1, "lh": 2, "lw": 4, "ld": 8, "lbu": 1, "lhu": 2, "lwu": 4}
STORES = {"sb": 1, "sh": 2, "sw": 4, "sd": 8}
BRANCHES = ["beq", "bne", "blt", "bge", "bltu", "bgeu"]

CSR_MSCRATCH, CSR_MLSTAT = 0x340, 0x801
CSR_MSTATUS, CSR_MIE, CSR_MTVEC, CSR_MCYCLE = 0x300, 0x304, 0x305, 0xB00
MSTATUS_MIE, MIE_MTIE = 1 << 3, 1 << 7
# Interrupt mode: the handler's register, its save area (outside the checked data
# region) and how far ahead it re-arms the timer: IRQ_GAP_MIN + (mcycle & IRQ_GAP_MASK).
R_IRQ = 27
IRQ_SAVE = TOHOST_ADDR + 0x40          # +0 x28, +8 x26, +16 interrupt count
IRQ_FIRST, IRQ_GAP_MIN, IRQ_GAP_MASK = 40, 48, 63   # the gap outlasts the handler
# CSRs the random programs read (ADR 0014): mscratch, minstret, instret, mlstat,
# mcycle, cycle, misa, mhartid.
CSR_READS = [0x340, 0xB02, 0xC02, 0x801, 0xB00, 0xC00, 0x301, 0xF14]

# Relative weights of each kind of item in the program body.
DEFAULT_MIX = {
    "alu_rr": 18, "alu_ri": 12, "shift": 8, "upper": 3, "muldiv": 10, "xmlacc": 10,
    "load": 12, "store": 9, "branch": 7, "jal": 2, "jalr": 2, "loop": 3, "csr": 2,
}

# Xmlacc-heavy mix: dense maccs around mispredicting branches, so the Xmlacc
# pipelines (ADR 0010) and the second unit (ADR 0013) see squashes mid-flight.
XM_MIX = {
    "alu_rr": 10, "alu_ri": 6, "shift": 2, "upper": 1, "muldiv": 2, "xmlacc": 30,
    "load": 6, "store": 4, "branch": 14, "jal": 1, "jalr": 1, "loop": 3, "csr": 2,
}
MIXES = {"default": DEFAULT_MIX, "xm": XM_MIX}


def _interesting64(rng):
    """Register seed values biased toward the edges where bugs live."""
    k = rng.random()
    if k < 0.25:
        return rng.getrandbits(64)
    if k < 0.45:
        return rng.choice([0, 1, 2, MASK64, 1 << 63, (1 << 63) - 1,
                           0xFFFF_FFFF, 0x8000_0000, 0x7FFF_FFFF, 0xFFFF_FFFF_8000_0000])
    if k < 0.70:
        return (rng.randint(-300, 300)) & MASK64
    return ((rng.getrandbits(32) ^ (rng.getrandbits(1) << 31)) - (1 << 31)) & MASK64


class RandomProgram:
    """One random program. build() returns (Asm, data, stats)."""

    def __init__(self, seed, n_items=180, mix=None, irq=False):
        self.seed = seed
        self.rng = random.Random(seed)
        self.n_items = n_items
        self.mix = dict(mix or DEFAULT_MIX)
        self.irq = irq
        self.pool = [r for r in POOL if not (irq and r == R_IRQ)]
        self.stats = Counter()
        self._label_n = 0
        self._recent_st = []      # offsets of the last few stores, for aliasing

    # -- helpers ---------------------------------------------------------
    def _label(self):
        self._label_n += 1
        return f"L{self._label_n}"

    def _src(self):
        # Mostly pool registers; sometimes x0, which reads as zero.
        return 0 if self.rng.random() < 0.05 else self.rng.choice(self.pool)

    def _dst(self):
        return 0 if self.rng.random() < 0.03 else self.rng.choice(self.pool)

    def _imm12(self):
        r = self.rng.random()
        if r < 0.2:
            return self.rng.choice([0, 1, -1, 2047, -2048])
        return self.rng.randint(-2048, 2047)

    def _mem_off(self, size, is_store):
        """Offset for a load/store. Often lands in the same doubleword as a
        recent store, so loads forward (fully or partially) and stores overlap."""
        rng = self.rng
        if self._recent_st and rng.random() < ALIAS_P:
            off = (rng.choice(self._recent_st) & ~7) + rng.randrange(0, 8, size)
            self.stats["mem_alias"] += 1
        else:
            off = rng.randrange(0, MEM_SPAN - 8 + 1, size)
        if is_store:
            self._recent_st = (self._recent_st + [off])[-RECENT_STORES:]
        return off

    # -- emitters for one "simple" instruction (no control flow) ----------
    def _emit_simple(self, a, allow_mem=True):
        kinds = ["alu_rr", "alu_ri", "shift", "upper", "muldiv", "xmlacc", "csr"]
        if allow_mem:
            kinds += ["load", "store"]
        weights = [self.mix[k] for k in kinds]
        kind = self.rng.choices(kinds, weights)[0]
        rng = self.rng
        self.stats[kind] += 1
        if kind == "alu_rr":
            getattr(a, rng.choice(ALU_RR))(self._dst(), self._src(), self._src())
        elif kind == "alu_ri":
            getattr(a, rng.choice(ALU_RI))(self._dst(), self._src(), self._imm12())
        elif kind == "shift":
            if rng.random() < 0.5:
                getattr(a, rng.choice(SHIFT_I))(self._dst(), self._src(), rng.randint(0, 63))
            else:
                getattr(a, rng.choice(SHIFT_IW))(self._dst(), self._src(), rng.randint(0, 31))
        elif kind == "upper":
            if rng.random() < 0.5:
                a.lui(self._dst(), rng.getrandbits(20))
            else:
                a.auipc(self._dst(), rng.getrandbits(20))
        elif kind == "muldiv":
            op = rng.choice(MULDIV)
            self.stats[op] += 1
            getattr(a, op)(self._dst(), self._src(), self._src())
        elif kind == "xmlacc":
            if rng.random() < 0.25:
                self.stats["macc"] += 1
                a.macc(self._dst(), self._src(), self._src(), self._src())
            else:
                op = rng.choice(XMLACC_RR)
                self.stats[op] += 1
                getattr(a, op)(self._dst(), self._src(), self._src())
        elif kind == "csr":
            # ADR 0014. Serializing on the out-of-order core. mcycle/cycle are
            # only read with rs1 = x0: the harness hands the ISS the RTL's value.
            r = rng.random()
            if r < 0.35:
                op = "csr_read"
                a.csrr(self._dst(), rng.choice(CSR_READS))
            elif r < 0.65:
                op = rng.choice(["csrrw", "csrrs", "csrrc"])
                getattr(a, op)(self._dst(), CSR_MSCRATCH, self._src())
            elif r < 0.8:
                op = rng.choice(["csrrwi", "csrrsi", "csrrci"])
                getattr(a, op)(self._dst(), CSR_MSCRATCH, rng.randint(0, 31))
            elif r < 0.9:
                op = "mlstat_clear"                 # W1C
                a.csrrw(self._dst(), CSR_MLSTAT, self._src())
            else:
                op = "fence"
                a.fence()
            self.stats[op] += 1
        elif kind == "load":
            op, size = rng.choice(list(LOADS.items()))
            off = self._mem_off(size, is_store=False)
            getattr(a, op)(self._dst(), R_BASE, off)
        else:  # store
            op, size = rng.choice(list(STORES.items()))
            off = self._mem_off(size, is_store=True)
            getattr(a, op)(self._src(), R_BASE, off)

    # -- control-flow items -----------------------------------------------
    def _emit_item(self, a):
        kinds = list(self.mix)
        kind = self.rng.choices(kinds, [self.mix[k] for k in kinds])[0]
        rng = self.rng
        if kind == "branch":
            self.stats["branch"] += 1
            lbl = self._label()
            getattr(a, rng.choice(BRANCHES))(self._src(), self._src(), lbl)
            for _ in range(rng.randint(1, 5)):
                self._emit_simple(a)
            a.label(lbl)
        elif kind == "jal":
            self.stats["jal"] += 1
            lbl = self._label()
            a.jal(self._dst(), lbl)
            for _ in range(rng.randint(1, 3)):
                self._emit_simple(a)
            a.label(lbl)
        elif kind == "jalr":
            self.stats["jalr"] += 1
            lbl = self._label()
            a.la_label(R_SCRATCH, lbl)
            a.jalr(self._dst(), R_SCRATCH, 0)
            for _ in range(rng.randint(1, 3)):
                self._emit_simple(a)
            a.label(lbl)
        elif kind == "loop":
            self.stats["loop"] += 1
            lbl = self._label()
            a.addi(R_LOOP, 0, rng.randint(1, 4))
            a.label(lbl)
            for _ in range(rng.randint(1, 4)):
                self._emit_simple(a)
            a.addi(R_LOOP, R_LOOP, -1)
            a.bne(R_LOOP, 0, lbl)
        else:
            self._emit_simple(a)

    # -- timer interrupts (ADR 0015) -----------------------------------------
    def _irq_prologue(self, a):
        """mtvec = handler, mie.MTIE, first interrupt IRQ_FIRST cycles from now, MIE."""
        a.la_label(R_IRQ, "irq_handler")
        a.csrw(CSR_MTVEC, R_IRQ)
        a.addi(R_IRQ, 0, MIE_MTIE)
        a.csrw(CSR_MIE, R_IRQ)
        a.csrr(R_IRQ, CSR_MCYCLE)
        a.addi(R_IRQ, R_IRQ, IRQ_FIRST)
        a.la(R_SCRATCH, MTIMECMP_ADDR)
        a.sd(R_IRQ, R_SCRATCH, 0)
        a.csrrsi(0, CSR_MSTATUS, MSTATUS_MIE)

    def _irq_handler(self, a):
        """Count the interrupt and re-arm the timer; every register but x27 survives."""
        a.label("irq_handler")
        a.la(R_IRQ, IRQ_SAVE)
        a.sd(R_SCRATCH, R_IRQ, 0)
        a.sd(26, R_IRQ, 8)
        a.ld(26, R_IRQ, 16)
        a.addi(26, 26, 1)
        a.sd(26, R_IRQ, 16)
        a.csrr(R_SCRATCH, CSR_MCYCLE)
        a.andi(26, R_SCRATCH, IRQ_GAP_MASK)
        a.add(R_SCRATCH, R_SCRATCH, 26)
        a.addi(R_SCRATCH, R_SCRATCH, IRQ_GAP_MIN)
        a.la(26, MTIMECMP_ADDR)
        a.sd(R_SCRATCH, 26, 0)
        a.ld(R_SCRATCH, R_IRQ, 0)
        a.ld(26, R_IRQ, 8)
        a.mret()

    # -- whole program -----------------------------------------------------
    def build(self):
        a = Asm()
        data_vals = [_interesting64(self.rng) for _ in range(DATA_SIZE // 8)]
        data = [(DATA_BASE + 8 * i, 8, v) for i, v in enumerate(data_vals)]
        a.la(R_BASE, DATA_BASE)
        for i, r in enumerate(self.pool):
            a.ld(r, R_BASE, 8 * i)
        if self.irq:
            self._irq_prologue(a)
        for _ in range(self.n_items):
            self._emit_item(a)
        a.la(R_TOHOST, TOHOST_ADDR)
        a.addi(R_SCRATCH, 0, 1)
        a.sw(R_SCRATCH, R_TOHOST, 0)
        if self.irq:
            self._irq_handler(a)
        words = a.assemble()
        assert len(words) <= MAX_WORDS, \
            f"seed {self.seed}: {len(words)} words overflows the code region ({MAX_WORDS})"
        return a, data, self.stats


def parse_seeds(spec):
    """'0-24' -> [0..24]; '3,7,9' -> [3,7,9]; '5' -> [5]."""
    out = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            lo, hi = part.split("-")
            out.extend(range(int(lo), int(hi) + 1))
        elif part:
            out.append(int(part))
    return out
