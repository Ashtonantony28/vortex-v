"""Seeded constrained-random RV64IM + Xmlacc program generator (ADR 0006).

Stands in for riscv-dv until it is installed. Every program:

- terminates by construction: branches and jumps only go forward, and loops are
  bounded by a counter register (x29) that random code never writes;
- only touches memory inside the data region, with naturally aligned accesses;
- ends with a PASS store to tohost.

Register roles: x1..x27 random sources/destinations (plus occasional x0
destinations), x28 scratch for JALR targets, x29 loop counter, x30 data base,
x31 tohost pointer.

The same seed always yields the same program, so a failing seed reproduces.
"""
import random
from collections import Counter

from rvasm import Asm, CODE_LIMIT, DATA_BASE, DATA_SIZE, MASK64, RESET_PC, TOHOST_ADDR

POOL = list(range(1, 28))
R_SCRATCH, R_LOOP, R_BASE, R_TOHOST = 28, 29, 30, 31
MEM_SPAN = 2048          # load/store offsets stay within a 12-bit signed immediate
assert MEM_SPAN <= DATA_SIZE
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

# Relative weights of each kind of item in the program body.
DEFAULT_MIX = {
    "alu_rr": 18, "alu_ri": 12, "shift": 8, "upper": 3, "muldiv": 10, "xmlacc": 10,
    "load": 12, "store": 9, "branch": 7, "jal": 2, "jalr": 2, "loop": 3,
}


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

    def __init__(self, seed, n_items=180, mix=None):
        self.seed = seed
        self.rng = random.Random(seed)
        self.n_items = n_items
        self.mix = dict(mix or DEFAULT_MIX)
        self.stats = Counter()
        self._label_n = 0

    # -- helpers ---------------------------------------------------------
    def _label(self):
        self._label_n += 1
        return f"L{self._label_n}"

    def _src(self):
        # Mostly pool registers; sometimes x0, which reads as zero.
        return 0 if self.rng.random() < 0.05 else self.rng.choice(POOL)

    def _dst(self):
        return 0 if self.rng.random() < 0.03 else self.rng.choice(POOL)

    def _imm12(self):
        r = self.rng.random()
        if r < 0.2:
            return self.rng.choice([0, 1, -1, 2047, -2048])
        return self.rng.randint(-2048, 2047)

    # -- emitters for one "simple" instruction (no control flow) ----------
    def _emit_simple(self, a, allow_mem=True):
        kinds = ["alu_rr", "alu_ri", "shift", "upper", "muldiv", "xmlacc"]
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
        elif kind == "load":
            op, size = rng.choice(list(LOADS.items()))
            off = rng.randrange(0, MEM_SPAN - 8 + 1, size)
            getattr(a, op)(self._dst(), R_BASE, off)
        else:  # store
            op, size = rng.choice(list(STORES.items()))
            off = rng.randrange(0, MEM_SPAN - 8 + 1, size)
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

    # -- whole program -----------------------------------------------------
    def build(self):
        a = Asm()
        data_vals = [_interesting64(self.rng) for _ in range(DATA_SIZE // 8)]
        data = [(DATA_BASE + 8 * i, 8, v) for i, v in enumerate(data_vals)]
        a.la(R_BASE, DATA_BASE)
        for i, r in enumerate(POOL):
            a.ld(r, R_BASE, 8 * i)
        for _ in range(self.n_items):
            self._emit_item(a)
        a.la(R_TOHOST, TOHOST_ADDR)
        a.addi(R_SCRATCH, 0, 1)
        a.sw(R_SCRATCH, R_TOHOST, 0)
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
