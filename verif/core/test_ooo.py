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


PROGRAMS = {"overlap": overlap_program, "wide": wide_program, "loop": loop_program}

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
            _, state, _ = await cosim_lockstep(dut, name, prog(), max_cycles=2000,
                                               mem_check=(DATA_BASE, DATA_SIZE))
            res = {"cycles": state["cycles"]}
            for counter in ("dbg_ooo_completions", "dbg_dual_commits",
                            "dbg_spec_branches", "dbg_mispredicts"):
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
