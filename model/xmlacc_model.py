"""Xmlacc v1 numpy integer golden model — Tier-1 ML ops.

Implements EXACTLY the semantics frozen in docs/XMLACC-SPEC-v1.md. Register
values are 64-bit; they are accepted as Python ints or numpy integer scalars and
returned as signed Python ints (the two's-complement value the register holds).

A GP register packs 8 int8 lanes, lane i = bits [8i+7:8i] (byte 0 = bits [7:0]).

Ops:
  dot8   : rd = SX64( sum_{i} sext8(rs1[i]) * sext8(rs2[i]) )
  dot8u  : rd = ZX64( sum_{i} zext8(rs1[i]) * zext8(rs2[i]) )
  dot8su : rd = SX64( sum_{i} sext8(rs1[i]) * zext8(rs2[i]) )
  macc   : rd = rs3 + sum_{i} sext8(rs1[i]) * sext8(rs2[i])   (64-bit two's complement)
  rqs    : sh=rs2[5:0]; t=(rs1 + (sh? 1<<(sh-1):0)) >>s sh; rd = SX64(clamp(t,-128,127));
           SAT sticky set if the clamp fired.

numpy is used for the lane arithmetic (int32 lane products, matching the HW
datapath width) while the public results are exact Python ints.
"""

import numpy as np

MASK64 = (1 << 64) - 1
NLANES = 8


# --------------------------------------------------------------------------- #
# mlstat — the custom-CSR status register (bit0 = SAT sticky, W1C).
# --------------------------------------------------------------------------- #
class MlStat:
    """Models CSR 0x801 (mlstat). bit0 = SAT sticky, set by rqs clamp, W1C."""

    SAT_BIT = 0

    def __init__(self, value=0):
        self.value = int(value) & 0xFFFFFFFFFFFFFFFF

    @property
    def sat(self):
        return bool((self.value >> self.SAT_BIT) & 1)

    def set_sat(self):
        self.value |= (1 << self.SAT_BIT)

    def w1c(self, mask):
        """Write-1-to-clear: bits set in `mask` are cleared."""
        self.value &= ~(int(mask) & 0xFFFFFFFFFFFFFFFF)

    def __repr__(self):
        return f"MlStat(value=0x{self.value:016x}, SAT={self.sat})"


# --------------------------------------------------------------------------- #
# Bit-level helpers
# --------------------------------------------------------------------------- #
def _u64(x):
    """Normalise a register value to its unsigned 64-bit bit pattern."""
    return int(x) & MASK64


def _to_s64(x):
    """Interpret a register value as a signed 64-bit two's-complement integer."""
    u = _u64(x)
    return u - (1 << 64) if u >= (1 << 63) else u


def _lanes_signed(reg):
    """Return the 8 lanes of `reg` as signed int8 values (list of ints)."""
    u = _u64(reg)
    out = []
    for i in range(NLANES):
        b = (u >> (8 * i)) & 0xFF
        out.append(b - 256 if b >= 128 else b)
    return out


def _lanes_unsigned(reg):
    """Return the 8 lanes of `reg` as unsigned uint8 values (list of ints)."""
    u = _u64(reg)
    return [(u >> (8 * i)) & 0xFF for i in range(NLANES)]


def _sx64(v):
    """Sign-extend/represent a signed value as the signed 64-bit register value."""
    return _to_s64(v)


def _zx64(v):
    """Zero-extend a non-negative value into the (non-negative) register value."""
    return _u64(v)


# --------------------------------------------------------------------------- #
# Tier-1 ops
# --------------------------------------------------------------------------- #
def dot8(rs1, rs2):
    """mlacc.dot8 : signed*signed int8 dot product, sign-extended to 64."""
    a = np.array(_lanes_signed(rs1), dtype=np.int32)
    b = np.array(_lanes_signed(rs2), dtype=np.int32)
    acc = int(np.sum(a * b, dtype=np.int64))  # cannot overflow int32, but be safe
    return _sx64(acc)


def dot8u(rs1, rs2):
    """mlacc.dot8u : unsigned*unsigned int8 dot product, zero-extended to 64."""
    a = np.array(_lanes_unsigned(rs1), dtype=np.int32)
    b = np.array(_lanes_unsigned(rs2), dtype=np.int32)
    acc = int(np.sum(a * b, dtype=np.int64))
    return _zx64(acc)


def dot8su(rs1, rs2):
    """mlacc.dot8su : signed(rs1)*unsigned(rs2) int8 dot product, sign-extended."""
    a = np.array(_lanes_signed(rs1), dtype=np.int32)
    b = np.array(_lanes_unsigned(rs2), dtype=np.int32)
    acc = int(np.sum(a * b, dtype=np.int64))
    return _sx64(acc)


def macc(rs1, rs2, rs3):
    """mlacc.macc : rd = rs3 + signed int8 dot product (64-bit two's complement)."""
    a = np.array(_lanes_signed(rs1), dtype=np.int32)
    b = np.array(_lanes_signed(rs2), dtype=np.int32)
    acc = int(np.sum(a * b, dtype=np.int64))
    total = _to_s64(rs3) + acc
    return _to_s64(total)  # wrap into signed 64-bit accumulator


def rqs(rs1, rs2, stat=None):
    """mlacc.rqs : requantize int32->int8 with round-half-up and saturation.

    sh = rs2[5:0]; t = (rs1 + (1<<(sh-1) if sh>0 else 0)) >>s sh;
    rd = SX64(clamp(t, -128, 127)). The SAT sticky bit is set if the clamp fired.

    Returns (rd, sat_fired). If `stat` (an MlStat) is given, its SAT bit is
    sticky-set when the clamp fires.
    """
    sh = _u64(rs2) & 0b111111  # rs2[5:0], range 0..63
    x = _to_s64(rs1)
    rnd = (1 << (sh - 1)) if sh > 0 else 0
    # Python >> on signed ints is arithmetic (floors toward -inf) == ASR.
    t = (x + rnd) >> sh

    if t < -128:
        clamped = -128
        sat = True
    elif t > 127:
        clamped = 127
        sat = True
    else:
        clamped = t
        sat = False

    if sat and stat is not None:
        stat.set_sat()

    return _sx64(clamped), sat
