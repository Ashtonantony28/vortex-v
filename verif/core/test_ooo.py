"""Prove the M4 core executes out of order (ADR 0006, verification section).

Correct results alone do not show that anything ran out of order: an in-order
core passes co-sim too. These tests run one program with independent work behind
a long divide and check two things:

1. vortex_ooo_core completes younger ops while the older divide is still in
   flight (its debug counter `dbg_ooo_completions` counts such completions);
2. it needs clearly fewer cycles than the in-order vortex_core on the same program.

The independent work is mostly multiplies. Both cores commit at most one
instruction per cycle and the in-order core already runs ALU ops at one per cycle,
so ALU ops alone cannot show a speed-up: they still have to commit one by one
after the divide. Multiplies stall the in-order core for 3 cycles each, but the
OoO core runs them on its own multiplier while the divider is busy.

A third program, a branchy loop, checks branch speculation (ADR 0008): the
speculative core must predict every branch, recover from mispredicts, and beat
the non-speculative two-wide core.

A second program checks the two-wide configuration (ADR 0007): independent ALU
ops must commit two per cycle (debug counter `dbg_dual_commits`) and finish in
well under the in-order core's cycle count.

A sixth program, an int8 matrix-vector kernel, reports MACs per cycle and checks
that a pipelined Xmlacc unit (ADR 0010) keeps the single-cycle unit's throughput
when the accumulators are independent.

A fifth program checks the load/store queue (ADR 0009): loads behind a store that
is waiting to commit must run early, one must forward from the store, and the run
must beat the in-order memory queue.

Every run is also lockstep co-simulated against the ISS, so the speed-up is not
bought with a wrong answer.

Run (from the repo root):  python3 -m pytest verif/core/test_ooo.py -q
"""
import json
import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
for _p in (str(HERE), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from cores import build_and_test  # noqa: E402
from rvasm import Asm, DATA_BASE, DATA_SIZE, TOHOST_ADDR  # noqa: E402

N_MUL = 22             # independent multiplies behind the divide (+ an addi after
                       # every third one: 31 instructions, fits a 32-entry ROB)
MAX_RATIO = 0.8        # OoO cycles must be at most this fraction of in-order cycles
                       # (measured at M4: 107 vs 149 cycles, 0.72)


def overlap_program():
    """Divide, then independent ALU work, then a use of the quotient, then PASS."""
    a = Asm()
    a.addi(1, 0, 2000)
    a.addi(2, 0, 7)
    a.div(3, 1, 2)                       # ~65 cycles in the iterative divider
    for i in range(N_MUL):               # independent of x3: free to run ahead
        a.mul(4 + (i % 16), 1, 2)
        if i % 3 == 0:
            a.addi(20 + (i % 4), 0, i + 1)
    a.add(25, 3, 4)                      # consumes the quotient
    a.la(31, TOHOST_ADDR)
    a.addi(9, 0, 1)
    a.sw(9, 31, 0)
    return a.assemble()


N_WIDE = 64            # independent ALU ops for the two-wide test
MAX_RATIO_WIDE = 0.7   # two-wide cycles vs in-order cycles on that program


def wide_program():
    """Independent ALU ops in pairs, then PASS: ideal for 2 per cycle."""
    a = Asm()
    for i in range(N_WIDE):
        a.addi(1 + (i % 24), 0, i + 1)
    a.la(31, TOHOST_ADDR)
    a.addi(26, 0, 1)
    a.sw(26, 31, 0)
    return a.assemble()


N_LOOP = 40            # iterations of the branchy loop
MAX_RATIO_SPEC = 0.8   # speculative vs non-speculative two-wide cycles on that loop


def loop_program():
    """A counted loop with a forward branch inside that is taken one time in
    four: both branches are mostly predictable, and each surprise is a mispredict."""
    a = Asm()
    a.addi(1, 0, N_LOOP)
    a.label("top")
    a.addi(2, 2, 1)
    a.addi(3, 3, 3)
    a.andi(4, 1, 3)
    a.beq(4, 0, "skip")              # taken when the count is a multiple of 4
    a.addi(5, 5, 7)
    a.addi(6, 6, 9)
    a.label("skip")
    a.addi(7, 7, -1)
    a.addi(1, 1, -1)
    a.bne(1, 0, "top")
    a.la(31, TOHOST_ADDR)
    a.addi(26, 0, 1)
    a.sw(26, 31, 0)
    return a.assemble()


N_LSQ_LOADS = 12       # independent loads behind a store that waits on a divide
MAX_RATIO_LSQ = 0.95   # LSQ vs in-order-memory-queue cycles on that program
                       # (measured at M7: 94 vs 105 cycles, 0.90)


def lsq_program():
    """A store sits behind a long divide in the ROB, so it cannot commit (and,
    without the LSQ, nothing behind it in the memory queue can run). Its address
    and data are known early. One load reads it back (forwarding); the others read
    other addresses and, with the multiplies that use them, are free to run
    during the divide (ADR 0009). 29 instructions are in flight behind the divide,
    which fits the 32-entry ROB."""
    a = Asm()
    a.la(30, DATA_BASE)
    a.addi(1, 0, 2000)
    a.addi(2, 0, 7)
    a.addi(5, 0, 123)
    a.div(3, 1, 2)                       # ~65 cycles: holds the ROB head
    a.sd(5, 30, 0)                       # address and data ready, waits to commit
    a.ld(6, 30, 0)                       # fully covered: forwards 123
    for i in range(N_LSQ_LOADS):
        a.ld(7 + i, 30, 64 + 8 * i)      # no older store overlaps: runs early
        a.mul(7 + i, 7 + i, 2)           # and so does the work that uses it
    a.add(25, 6, 7)
    a.add(25, 25, 3)                     # consumes the quotient
    a.la(31, TOHOST_ADDR)
    a.addi(26, 0, 1)
    a.sw(26, 31, 0)
    return a.assemble()


XM_K = 16              # activation vectors in the int8 kernel
XM_CH = 4              # output channels (independent accumulators)
XM_MACS = XM_K * XM_CH * 8
MIN_XM_RATIO = 0.85    # pipelined (XM_LATENCY=3) vs single-cycle Xmlacc MACs/cycle


def xm_program():
    """An int8 matrix-vector kernel: 4 output channels, weights held in registers,
    16 activation vectors streamed from memory, one macc per channel per vector.
    The 4 accumulators are independent, so a pipelined Xmlacc unit can start one
    macc per cycle and its latency hides behind the other channels (ADR 0010)."""
    a = Asm()
    a.la(30, DATA_BASE)
    for c in range(XM_CH):
        a.ld(10 + c, 30, 8 * c)          # weights
        a.addi(20 + c, 0, 0)             # accumulators
    for k in range(XM_K):
        a.ld(1 + k % 8, 30, 64 + 8 * k)  # activations
        for c in range(XM_CH):
            a.macc(20 + c, 1 + k % 8, 10 + c, 20 + c)
    for c in range(XM_CH):
        a.sd(20 + c, 30, 512 + 8 * c)
    a.la(31, TOHOST_ADDR)
    a.addi(26, 0, 1)
    a.sw(26, 31, 0)
    return a.assemble()


def xm_data():
    vals = [(0x0123_4567_89AB_CDEF * (i + 3)) & ((1 << 64) - 1) for i in range(8 + XM_K)]
    return [(DATA_BASE + 8 * i, 8, v) for i, v in enumerate(vals[:XM_CH])] + \
           [(DATA_BASE + 64 + 8 * k, 8, v) for k, v in enumerate(vals[8:])]


PROGRAMS = {"overlap": overlap_program, "wide": wide_program, "loop": loop_program,
            "lsq": lsq_program, "xm": xm_program}
PROGRAM_DATA = {"xm": xm_data}

try:
    import cocotb
except ImportError:  # pragma: no cover
    cocotb = None

if cocotb is not None:
    @cocotb.test()
    async def ooo_programs(dut):
        """Run each program under ISS co-sim and record cycles + debug counters."""
        from harness import cosim_lockstep
        results = {}
        for name, prog in PROGRAMS.items():
            data = PROGRAM_DATA[name]() if name in PROGRAM_DATA else None
            _, state, _ = await cosim_lockstep(dut, name, prog(), data=data, max_cycles=2000,
                                               mem_check=(DATA_BASE, DATA_SIZE))
            res = {"cycles": state["cycles"]}
            for counter in ("dbg_ooo_completions", "dbg_dual_commits",
                            "dbg_spec_branches", "dbg_mispredicts",
                            "dbg_lsq_forwards", "dbg_lsq_early", "dbg_xm_macs"):
                if hasattr(dut, counter):
                    res[counter] = int(getattr(dut, counter).value)
            dut._log.info("%s: %s", name, res)
            results[name] = res
        out = os.environ.get("VORTEX_OVERLAP_OUT")
        if out:
            Path(out).write_text(json.dumps(results))


_CACHE = {}


def _measure(config, tmp_path):
    if config not in _CACHE:
        out = tmp_path / f"{config}.json"
        build_and_test(config, "test_ooo", HERE, extra_pythonpath=[ROOT],
                       env={"VORTEX_OVERLAP_OUT": str(out)})
        _CACHE[config] = json.loads(out.read_text())
    return _CACHE[config]


@pytest.mark.parametrize("config", ["vortex_ooo_core", "vortex_ooo_core_w2"])
def test_ooo_core_overlaps_work_behind_a_divide(config, tmp_path):
    inorder = _measure("vortex_core", tmp_path)["overlap"]
    ooo = _measure(config, tmp_path)["overlap"]
    assert ooo["dbg_ooo_completions"] >= N_MUL // 2, \
        f"only {ooo['dbg_ooo_completions']} out-of-order completions"
    assert ooo["cycles"] <= MAX_RATIO * inorder["cycles"], \
        f"OoO {ooo['cycles']} cycles vs in-order {inorder['cycles']} (need <= {MAX_RATIO:.0%})"


def test_two_wide_commits_two_per_cycle(tmp_path):
    inorder = _measure("vortex_core", tmp_path)["wide"]
    narrow = _measure("vortex_ooo_core", tmp_path)["wide"]
    wide = _measure("vortex_ooo_core_w2", tmp_path)["wide"]
    assert narrow["dbg_dual_commits"] == 0, "WIDTH=1 must never commit two per cycle"
    assert wide["dbg_dual_commits"] >= N_WIDE // 4, \
        f"only {wide['dbg_dual_commits']} dual-commit cycles"
    assert wide["cycles"] <= MAX_RATIO_WIDE * inorder["cycles"], \
        f"two-wide {wide['cycles']} cycles vs in-order {inorder['cycles']} " \
        f"(need <= {MAX_RATIO_WIDE:.0%})"


def test_speculation_runs_ahead_of_branches(tmp_path):
    plain = _measure("vortex_ooo_core_w2", tmp_path)["loop"]
    spec = _measure("vortex_ooo_core_spec", tmp_path)["loop"]
    assert plain["dbg_spec_branches"] == 0, "SPEC_BRANCHES=0 must never predict"
    assert spec["dbg_spec_branches"] >= 2 * N_LOOP, \
        f"only {spec['dbg_spec_branches']} predicted branches"
    assert spec["dbg_mispredicts"] > 0, "no mispredict: recovery was never exercised"
    assert spec["cycles"] <= MAX_RATIO_SPEC * plain["cycles"], \
        f"speculative {spec['cycles']} cycles vs {plain['cycles']} without " \
        f"(need <= {MAX_RATIO_SPEC:.0%})"


def test_lsq_runs_loads_past_a_waiting_store(tmp_path):
    plain = _measure("vortex_ooo_core_spec", tmp_path)["lsq"]
    lsq = _measure("vortex_ooo_core_lsq", tmp_path)["lsq"]
    assert plain["dbg_lsq_forwards"] == 0 and plain["dbg_lsq_early"] == 0, \
        "LSQ=0 must never forward or run a load early"
    assert lsq["dbg_lsq_forwards"] >= 1, "the covered load did not forward"
    assert lsq["dbg_lsq_early"] >= N_LSQ_LOADS // 2, \
        f"only {lsq['dbg_lsq_early']} loads ran ahead of the queue head"
    assert lsq["cycles"] <= MAX_RATIO_LSQ * plain["cycles"], \
        f"LSQ {lsq['cycles']} cycles vs {plain['cycles']} without (need <= {MAX_RATIO_LSQ:.0%})"


def test_pipelined_xmlacc_keeps_mac_throughput(tmp_path):
    single = _measure("vortex_ooo_core_lsq", tmp_path)["xm"]
    piped = _measure("vortex_ooo_core_xm", tmp_path)["xm"]
    for res in (single, piped):
        assert res["dbg_xm_macs"] == XM_MACS, \
            f"{res['dbg_xm_macs']} MACs counted, expected {XM_MACS}"
    rate_single = XM_MACS / single["cycles"]
    rate_piped = XM_MACS / piped["cycles"]
    print(f"int8 kernel: {rate_single:.2f} MACs/cycle at XM_LATENCY=1, "
          f"{rate_piped:.2f} at XM_LATENCY=3")
    assert rate_piped >= MIN_XM_RATIO * rate_single, \
        f"pipelined {rate_piped:.2f} MACs/cycle vs {rate_single:.2f} single-cycle " \
        f"(need >= {MIN_XM_RATIO:.0%})"
