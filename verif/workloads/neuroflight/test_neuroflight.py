"""NeuroFlight's int8 hover policy, bit-exact on Vortex-V (cross-project workload).

The fixtures come from neuroflight-fc's own golden model (gen_fixture.py records the
commit). These checks show Vortex-V computes the flight policy exactly as the
NeuroFlight firmware does, using only existing Xmlacc instructions:

1. the plain-int reference in nf_kernel.py matches the fixture (contract understood);
2. the kernel on the golden ISS matches every fixture vector (hidden layers + output);
3. the same kernel without macc (plain RV64IM) gives the same answers with several
   times the instructions, and a mutant kernel with a floor shift instead of rqs fails, so the fixtures catch a
   wrong rounding rule;
4. mlacc.rqs in the Xmlacc golden model equals NeuroFlight's rshift_round + clamp on
   its 4000 edge and random cases (ties, int32*int32 extremes, shifts 1..62);
5. on the RTL, the kernel runs in lockstep with the ISS on every core configuration
   and its outputs in memory match the fixture.

Run (from the repo root):  python3 -m pytest verif/workloads/neuroflight -q
"""
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
CORE_DIR = ROOT / "verif" / "core"
for _p in (str(HERE), str(CORE_DIR), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import nf_kernel as nf  # noqa: E402
from cores import CORES, build_and_test  # noqa: E402

RTL_VECTORS = (66, 42)         # an in-distribution input and the input with the most rqs clamps
MIN_SCALAR_RATIO = 3.0         # measured 2026-11-12: see README
MAX_RTL_CYCLES = 60_000        # ~7.8k instructions per vector, mul is multi-cycle in-order


def _expected(v):
    return {"hidden": v["hidden"], "last_acc": v["last_acc"]}


@pytest.fixture(scope="module")
def fix():
    return nf.load_fixture()


def test_fixture_provenance(fix):
    src = fix["source"]
    assert src["repo"] == "Ashtonantony28/neuroflight-fc" and len(src["commit"]) == 40
    assert [(len(L["W_q"]), len(L["W_q"][0])) for L in fix["layers"]] == [(64, 19), (64, 64), (4, 64)]
    assert len(fix["vectors"]) == 114
    assert {v["tag"] for v in fix["vectors"]} == {"zero_mean", "tie", "grid", "saturating", "nan",
                                                   "in_dist", "wide", "beyond"}


def test_reference_matches_fixture(fix):
    for i, v in enumerate(fix["vectors"]):
        assert nf.reference(fix, v["x_q"]) == _expected(v), f"vector {i} ({v['tag']})"


def test_kernel_on_iss_matches_fixture(fix):
    out, hart = nf.run_on_iss(fix, fix["vectors"])
    for i, (o, v) in enumerate(zip(out, fix["vectors"])):
        assert o == _expected(v), f"vector {i} ({v['tag']})"
    assert hart.mlstat_sat, "saturating inputs must set mlstat.SAT"


def test_int32_saturation_path(fix):
    """Output biases at the int32 limits force sat_int32 both ways (no fixture input does)."""
    import copy
    f2 = copy.deepcopy(fix)
    last = f2["layers"][-1]
    last["b_q"] = [(1 << 31) - 1, -(1 << 31), (1 << 31) - 1, -(1 << 31)]
    vecs = f2["vectors"][:8]
    out, _ = nf.run_on_iss(f2, vecs)
    want = [nf.reference(f2, v["x_q"]) for v in vecs]
    assert out == want
    flat = [a for w in want for a in w["last_acc"]]
    assert (1 << 31) - 1 in flat and -(1 << 31) in flat, "both clamps must fire"


def test_floor_shift_mutant_is_caught(fix):
    out, _ = nf.run_on_iss(fix, fix["vectors"], mutant="floor")
    wrong = sum(o != _expected(v) for o, v in zip(out, fix["vectors"]))
    assert wrong == len(fix["vectors"])


def test_xmlacc_beats_scalar_baseline(fix):
    """Same results without macc, at a much higher instruction count."""
    vecs = fix["vectors"][:8]
    out_x, h_x = nf.run_on_iss(fix, vecs)
    out_s, h_s = nf.run_on_iss(fix, vecs, mutant="scalar")
    assert out_x == out_s == [_expected(v) for v in vecs]
    ratio = h_s.inst_count / h_x.inst_count
    print(f"instructions per inference: Xmlacc {h_x.inst_count // len(vecs)}, "
          f"scalar RV64IM {h_s.inst_count // len(vecs)} ({ratio:.2f}x)")
    assert ratio > MIN_SCALAR_RATIO


def test_rqs_equals_neuroflight_rshift_round():
    from model.xmlacc_model import rqs
    cases = json.loads((HERE / "fixtures" / "rshift_cases.json").read_text())["cases"]
    assert len(cases) == 4000
    for v, s, want in cases:
        got, _ = rqs(v & nf.MASK64, s)      # returns the signed int8 result
        assert got == want, f"rqs({v}, {s}) = {got}, NeuroFlight expects {want}"


# ---------------------------------------------------------------------------
# RTL co-simulation (cocotb)
# ---------------------------------------------------------------------------
try:
    import cocotb
except ImportError:  # pragma: no cover
    cocotb = None

if cocotb is not None:
    @cocotb.test()
    async def neuroflight_cosim(dut):
        """Policy kernel in lockstep with the ISS; outputs in RTL memory match the fixture."""
        from harness import cosim_lockstep
        fx = nf.load_fixture()
        vecs = [fx["vectors"][i] for i in RTL_VECTORS]
        words, data, lay = nf.build(fx, vecs)
        retires, state, iss = await cosim_lockstep(
            dut, "neuroflight", words, data=data, max_cycles=MAX_RTL_CYCLES,
            mem_check=(nf.NF_BASE, lay.end - nf.NF_BASE))
        # mem_check proved RTL memory == ISS memory over the whole image, so the ISS
        # copy stands for the RTL's outputs here.
        for k, (o, v) in enumerate(zip(nf.decode(iss.mem.load, lay), vecs)):
            assert o == _expected(v), f"RTL vector {RTL_VECTORS[k]} ({v['tag']})"
        assert state["mlstat_sat"], "the saturating vector must set mlstat.SAT on the RTL"
        dut._log.info("neuroflight policy: %d vectors, %d instructions, %d cycles (%.0f cycles per inference)",
                      len(vecs), len(retires), state["cycles"], state["cycles"] / len(vecs))


@pytest.mark.parametrize("toplevel", CORES)
def test_neuroflight_rtl(toplevel):
    build_and_test(toplevel, "test_neuroflight", HERE, extra_pythonpath=[CORE_DIR, ROOT])
