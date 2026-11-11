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

Both runs are also lockstep co-simulated against the ISS, so the speed-up is not
bought with a wrong answer.

Run (from the repo root):  python3 -m pytest verif/core/test_ooo.py -q
"""
import json
import os
import sys
from pathlib import Path

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


try:
    import cocotb
except ImportError:  # pragma: no cover
    cocotb = None

if cocotb is not None:
    @cocotb.test()
    async def ooo_overlap(dut):
        """Run the overlap program under ISS co-sim and record cycles + OoO evidence."""
        from harness import cosim_lockstep
        _, state, _ = await cosim_lockstep(dut, "overlap", overlap_program(), max_cycles=2000,
                                           mem_check=(DATA_BASE, DATA_SIZE))
        ooo = None
        if hasattr(dut, "dbg_ooo_completions"):
            ooo = int(dut.dbg_ooo_completions.value)
            assert ooo > 0, "no instruction completed ahead of an older one"
        dut._log.info("overlap: %d cycles, ooo completions=%s", state["cycles"], ooo)
        out = os.environ.get("VORTEX_OVERLAP_OUT")
        if out:
            Path(out).write_text(json.dumps({"cycles": state["cycles"], "ooo": ooo}))


def _measure(toplevel, tmp_path):
    out = tmp_path / f"{toplevel}.json"
    build_and_test(toplevel, "test_ooo", HERE, extra_pythonpath=[ROOT],
                   env={"VORTEX_OVERLAP_OUT": str(out)})
    return json.loads(out.read_text())


def test_ooo_core_overlaps_work_behind_a_divide(tmp_path):
    inorder = _measure("vortex_core", tmp_path)
    ooo = _measure("vortex_ooo_core", tmp_path)
    assert ooo["ooo"] and ooo["ooo"] >= N_MUL // 2, \
        f"only {ooo['ooo']} out-of-order completions"
    assert ooo["cycles"] <= MAX_RATIO * inorder["cycles"], \
        f"OoO {ooo['cycles']} cycles vs in-order {inorder['cycles']} (need <= {MAX_RATIO:.0%})"
