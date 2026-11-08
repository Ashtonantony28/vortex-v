"""pytest tests for the Xmlacc Tier-1 numpy golden model."""

import random

import numpy as np
import pytest

import xmlacc_model as m


# --------------------------------------------------------------------------- #
# Packing helpers (build a 64-bit register value from 8 lanes)
# --------------------------------------------------------------------------- #
def pack(lanes):
    """Pack 8 lane values (signed or unsigned bytes) into a 64-bit reg value."""
    assert len(lanes) == 8
    u = 0
    for i, v in enumerate(lanes):
        u |= (int(v) & 0xFF) << (8 * i)
    return u


def ref_dot(a, b):
    """Slow reference: sum of products over an int32 loop (returns Python int)."""
    acc = np.int64(0)
    for x, y in zip(a, b):
        acc += np.int32(x) * np.int32(y)
    return int(acc)


# --------------------------------------------------------------------------- #
# Lane extraction sanity
# --------------------------------------------------------------------------- #
def test_lane_extraction_signed():
    reg = pack([-128, -1, 0, 1, 127, 50, -50, 100])
    assert m._lanes_signed(reg) == [-128, -1, 0, 1, 127, 50, -50, 100]


def test_lane_extraction_unsigned():
    reg = pack([0, 1, 127, 128, 255, 200, 100, 50])
    assert m._lanes_unsigned(reg) == [0, 1, 127, 128, 255, 200, 100, 50]


def test_byte0_is_low_bits():
    # lane 0 occupies bits [7:0]
    assert m._lanes_unsigned(0x00000000000000AB)[0] == 0xAB
    assert m._lanes_unsigned(0xAB00000000000000)[7] == 0xAB


# --------------------------------------------------------------------------- #
# dot8 — signed * signed
# --------------------------------------------------------------------------- #
def test_dot8_zeros():
    assert m.dot8(0, 0) == 0


def test_dot8_ones():
    assert m.dot8(pack([1] * 8), pack([1] * 8)) == 8


def test_dot8_max_positive():
    # 8 * 127 * 127
    assert m.dot8(pack([127] * 8), pack([127] * 8)) == 8 * 127 * 127 == 129032


def test_dot8_min_times_min_positive():
    # 8 * (-128)*(-128) = +131072
    assert m.dot8(pack([-128] * 8), pack([-128] * 8)) == 8 * 128 * 128 == 131072


def test_dot8_mixed_sign_negative():
    # 8 * (-128 * 127) = -130048
    res = m.dot8(pack([-128] * 8), pack([127] * 8))
    assert res == 8 * (-128 * 127) == -130048
    assert res < 0


def test_dot8_handcomputed_mixed():
    a = [1, -2, 3, -4, 5, -6, 7, -8]
    b = [8, 7, 6, 5, 4, 3, 2, 1]
    expected = sum(x * y for x, y in zip(a, b))
    assert m.dot8(pack(a), pack(b)) == expected


def test_dot8_fits_int32_no_overflow():
    # Extreme magnitude still well inside int32.
    r = m.dot8(pack([-128] * 8), pack([127] * 8))
    assert -(2 ** 31) <= r < 2 ** 31


# --------------------------------------------------------------------------- #
# dot8u — unsigned * unsigned
# --------------------------------------------------------------------------- #
def test_dot8u_zeros():
    assert m.dot8u(0, 0) == 0


def test_dot8u_max():
    # 8 * 255 * 255 = 520200
    assert m.dot8u(pack([0xFF] * 8), pack([0xFF] * 8)) == 8 * 255 * 255 == 520200


def test_dot8u_treats_high_byte_as_unsigned():
    # byte 0xFF is 255 unsigned, not -1
    assert m.dot8u(pack([0xFF, 0, 0, 0, 0, 0, 0, 0]),
                   pack([0x02, 0, 0, 0, 0, 0, 0, 0])) == 255 * 2


def test_dot8u_always_nonnegative():
    for _ in range(50):
        a = [random.randint(0, 255) for _ in range(8)]
        b = [random.randint(0, 255) for _ in range(8)]
        assert m.dot8u(pack(a), pack(b)) >= 0


# --------------------------------------------------------------------------- #
# dot8su — signed(rs1) * unsigned(rs2)
# --------------------------------------------------------------------------- #
def test_dot8su_zeros():
    assert m.dot8su(0, 0) == 0


def test_dot8su_sign_mix():
    # rs1 = -128 (signed), rs2 = 255 (unsigned)
    assert m.dot8su(pack([-128] * 8), pack([0xFF] * 8)) == 8 * (-128 * 255) == -261120


def test_dot8su_asymmetry():
    # The ops differ because rs2 is read unsigned here.
    reg_a = pack([1, 0, 0, 0, 0, 0, 0, 0])      # rs1 lane0 = 1 (signed)
    reg_b = pack([0xFF, 0, 0, 0, 0, 0, 0, 0])   # rs2 lane0 = 0xFF
    assert m.dot8su(reg_a, reg_b) == 1 * 255    # unsigned -> +255
    assert m.dot8(reg_a, reg_b) == 1 * (-1)     # signed   -> -1


# --------------------------------------------------------------------------- #
# macc — fused accumulate
# --------------------------------------------------------------------------- #
def test_macc_zero_acc_equals_dot8():
    a = pack([3, -4, 5, -6, 1, 2, -1, 7])
    b = pack([-1, 2, -3, 4, 5, -6, 7, -8])
    assert m.macc(a, b, 0) == m.dot8(a, b)


def test_macc_adds_accumulator():
    a = pack([1] * 8)
    b = pack([1] * 8)
    assert m.macc(a, b, 1000) == 1000 + 8


def test_macc_negative_accumulator():
    a = pack([1] * 8)
    b = pack([1] * 8)
    assert m.macc(a, b, -1000) == -1000 + 8


def test_macc_large_accumulator_chain():
    # Simulate a GEMM inner loop accumulating many tiles.
    acc = 0
    a = pack([127] * 8)
    b = pack([127] * 8)
    for _ in range(1000):
        acc = m.macc(a, b, acc)
    assert acc == 1000 * 8 * 127 * 127


def test_macc_64bit_wraparound():
    # rs3 at the top of the signed 64-bit range wraps as two's complement.
    top = (1 << 63) - 1  # max signed 64-bit
    a = pack([1] * 8)
    b = pack([1] * 8)     # dot = 8
    res = m.macc(a, b, top)
    assert res == m._to_s64(top + 8)
    assert res < 0  # wrapped to negative


# --------------------------------------------------------------------------- #
# rqs — requantize (round-half-up, arithmetic shift, saturate)
# --------------------------------------------------------------------------- #
def test_rqs_shift_zero_passthrough():
    rd, sat = m.rqs(100, 0)
    assert rd == 100 and sat is False


def test_rqs_shift_zero_no_rounding_term():
    # sh==0 => rnd term is 0, value passes through (then clamps).
    rd, sat = m.rqs(-128, 0)
    assert rd == -128 and sat is False


def test_rqs_round_half_up_positive():
    # 1 >> 1 with +0.5 bias: (1+1)>>1 = 1
    assert m.rqs(1, 1) == (1, False)
    # 3/2 = 1.5 -> 2 : (3+1)>>1 = 2
    assert m.rqs(3, 1) == (2, False)
    # 5/4 = 1.25 -> round-half-up at .5 boundary only; (5 + 2) >> 2 = 1
    assert m.rqs(5, 2) == (1, False)
    # 6/4 = 1.5 -> 2 : (6 + 2) >> 2 = 2
    assert m.rqs(6, 2) == (2, False)


def test_rqs_round_half_up_negative():
    # -0.5 rounds up to 0 : (-1 + 1) >> 1 = 0
    assert m.rqs(-1, 1) == (0, False)
    # -1.5 rounds up to -1 : (-3 + 1) >> 1 = -1
    assert m.rqs(-3, 1) == (-1, False)
    # -2.5 rounds up to -2 : (-5 + 1) >> 1 = -2
    assert m.rqs(-5, 1) == (-2, False)


def test_rqs_arithmetic_shift_floor_for_negatives():
    # Without the rounding bias at sh large, negative values floor toward -inf.
    # -8 >> 2 with bias (1<<1)=2 : (-8+2)>>2 = (-6)>>2 = -2  (floor(-1.5))
    assert m.rqs(-8, 2)[0] == -2


def test_rqs_saturation_high():
    rd, sat = m.rqs(1000, 0)
    assert rd == 127 and sat is True


def test_rqs_saturation_low():
    rd, sat = m.rqs(-1000, 0)
    assert rd == -128 and sat is True


def test_rqs_saturation_boundaries():
    # exactly 127 -> no clamp
    assert m.rqs(127, 0) == (127, False)
    # exactly 128 -> clamp to 127, sat
    assert m.rqs(128, 0) == (127, True)
    # exactly -128 -> no clamp
    assert m.rqs(-128, 0) == (-128, False)
    # -129 -> clamp to -128, sat
    assert m.rqs(-129, 0) == (-128, True)


def test_rqs_rounding_into_saturation():
    # t = (255 + 1) >> 1 = 128 -> clamp to 127, sat set by the rounded value.
    rd, sat = m.rqs(255, 1)
    assert rd == 127 and sat is True


def test_rqs_rs2_only_low_6_bits_used():
    # sh is rs2[5:0]; high bits of rs2 are ignored.
    assert m.rqs(16, 0b11000000 | 2) == m.rqs(16, 2)


def test_rqs_sat_sticky_in_mlstat():
    stat = m.MlStat()
    assert stat.sat is False
    m.rqs(10, 0, stat)        # no clamp
    assert stat.sat is False
    m.rqs(5000, 0, stat)      # clamp -> sticky set
    assert stat.sat is True
    m.rqs(10, 0, stat)        # no clamp, but stays sticky
    assert stat.sat is True
    stat.w1c(1)               # write-1-to-clear bit 0
    assert stat.sat is False


# --------------------------------------------------------------------------- #
# Randomized cross-checks against the slow reference
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("seed", range(20))
def test_dot8_random_crosscheck(seed):
    rng = random.Random(seed)
    a = [rng.randint(-128, 127) for _ in range(8)]
    b = [rng.randint(-128, 127) for _ in range(8)]
    assert m.dot8(pack(a), pack(b)) == ref_dot(a, b)


@pytest.mark.parametrize("seed", range(20))
def test_dot8u_random_crosscheck(seed):
    rng = random.Random(1000 + seed)
    a = [rng.randint(0, 255) for _ in range(8)]
    b = [rng.randint(0, 255) for _ in range(8)]
    assert m.dot8u(pack(a), pack(b)) == ref_dot(a, b)


@pytest.mark.parametrize("seed", range(20))
def test_dot8su_random_crosscheck(seed):
    rng = random.Random(2000 + seed)
    a = [rng.randint(-128, 127) for _ in range(8)]   # signed lanes
    b = [rng.randint(0, 255) for _ in range(8)]       # unsigned lanes
    assert m.dot8su(pack(a), pack(b)) == ref_dot(a, b)


@pytest.mark.parametrize("seed", range(20))
def test_macc_random_crosscheck(seed):
    rng = random.Random(3000 + seed)
    a = [rng.randint(-128, 127) for _ in range(8)]
    b = [rng.randint(-128, 127) for _ in range(8)]
    rs3 = rng.randint(-(10 ** 9), 10 ** 9)
    assert m.macc(pack(a), pack(b), rs3) == rs3 + ref_dot(a, b)


@pytest.mark.parametrize("seed", range(40))
def test_rqs_random_crosscheck(seed):
    rng = random.Random(4000 + seed)
    x = rng.randint(-(1 << 31), (1 << 31) - 1)
    sh = rng.randint(0, 63)
    rd, sat = m.rqs(x, sh)
    # Independent reference using Python floor-division arithmetic shift.
    rnd = (1 << (sh - 1)) if sh > 0 else 0
    t = (x + rnd) >> sh
    exp = max(-128, min(127, t))
    exp_sat = t < -128 or t > 127
    assert rd == exp
    assert sat == exp_sat
