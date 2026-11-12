"""Formal proof of the LSQ store-to-load forwarding unit (ADR 0009).

SymbiYosys is not installed in this environment, so the proof runs yosys's own
SAT solver directly: `sat -prove-asserts -set-assumes` proves every assertion in
verif/formal/lsq_fwd_props.sv for all inputs (the unit is combinational, so no
induction depth is needed). The proof runs at XLEN=32 with 3 older stores, which
takes about half a minute: byte-level forwarding does not depend on the width,
and the RTL is the same module the core instantiates at XLEN=64.

Run (from the repo root):  python3 -m pytest verif/formal -q
Skipped when yosys is missing, unless CI is set (then it fails).
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
RTL = ROOT / "rtl" / "core" / "vortex_lsq_fwd.sv"
PROPS = HERE / "lsq_fwd_props.sv"


def _yosys():
    exe = shutil.which("yosys")
    if exe is None:
        if os.environ.get("CI"):
            pytest.fail("yosys is required in CI for the formal proof")
        pytest.skip("yosys not installed")
    return exe


def _prove(script):
    return subprocess.run([_yosys(), "-q", "-p", script], cwd=HERE,
                          capture_output=True, text=True, timeout=900)


def test_lsq_forwarding_is_proven():
    script = (f"read_verilog -formal -sv {RTL} {PROPS}; prep -top lsq_fwd_props; "
              "flatten; opt; sat -prove-asserts -set-assumes -verify")
    r = _prove(script)
    assert r.returncode == 0, f"proof failed:\n{r.stdout[-4000:]}\n{r.stderr[-4000:]}"


def test_proof_is_not_vacuous():
    """A broken forwarding unit must fail the same proof: bypass the offset shift."""
    src = RTL.read_text()
    needle = "ov_data >> {ov_off, 3'b000}"
    assert needle in src, "mutation anchor moved; update this test"
    mutant = HERE / "_mutant_lsq_fwd.sv"
    mutant.write_text(src.replace(needle, "ov_data"))
    try:
        script = (f"read_verilog -formal -sv {mutant} {PROPS}; prep -top lsq_fwd_props; "
                  "flatten; opt; sat -prove-asserts -set-assumes -verify")
        r = _prove(script)
    finally:
        mutant.unlink()
    assert r.returncode != 0, "the proof passed on a unit that ignores the byte offset"
