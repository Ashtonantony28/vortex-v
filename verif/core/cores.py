"""Which cores the verification suites run on, and how to build and run them.

Every core here has the docs/M1-INTERFACE-v1.md ports, so the same cocotb tests,
ISS co-sim and conformance suites apply to all of them (ADR 0006). Adding a core
to CORES runs every suite on it.
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RTL_DIR = ROOT / "rtl" / "core"
FILELIST = RTL_DIR / "files.f"

# In-order reference core and the M4 out-of-order core (ADR 0006).
CORES = ("vortex_core", "vortex_ooo_core")


def rtl_sources():
    """RTL sources from rtl/core/files.f, the list lint also uses."""
    return [str(ROOT / line.strip()) for line in FILELIST.read_text().splitlines()
            if line.strip() and not line.strip().startswith("//")]


def build_and_test(toplevel, test_module, test_dir, extra_pythonpath=(), env=None):
    """Build `toplevel` under the cocotb runner and run the cocotb tests in `test_module`.

    Raises (via the runner) if any cocotb test fails, which fails the calling pytest.
    """
    try:
        from cocotb_tools.runner import get_runner
    except ImportError:  # older cocotb
        from cocotb.runner import get_runner

    test_dir = Path(test_dir)
    build_dir = test_dir / "sim_build" / toplevel
    paths = [str(test_dir), str(Path(__file__).parent), *map(str, extra_pythonpath)]
    os.environ["PYTHONPATH"] = os.pathsep.join(paths + [os.environ.get("PYTHONPATH", "")])
    for k, v in (env or {}).items():
        os.environ[k] = v

    runner = get_runner(os.getenv("SIM", "icarus"))
    runner.build(sources=rtl_sources(), hdl_toplevel=toplevel, build_dir=str(build_dir),
                 always=True, timescale=("1ns", "1ps"))
    runner.test(hdl_toplevel=toplevel, test_module=test_module, test_dir=str(test_dir),
                build_dir=str(build_dir), timescale=("1ns", "1ps"))
