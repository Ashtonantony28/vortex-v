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

# Each configuration is a name -> (HDL toplevel, parameters). The in-order reference
# core, the M4 out-of-order core (ADR 0006), the same core two-wide (ADR 0007), and
# two-wide with branch prediction and speculation (ADR 0008), and that plus the
# load/store queue with store-to-load forwarding (ADR 0009).
CORE_CONFIGS = {
    "vortex_core": ("vortex_core", {}),
    "vortex_ooo_core": ("vortex_ooo_core", {}),
    "vortex_ooo_core_w2": ("vortex_ooo_core", {"WIDTH": 2}),
    "vortex_ooo_core_spec": ("vortex_ooo_core", {"WIDTH": 2, "SPEC_BRANCHES": 1}),
    "vortex_ooo_core_lsq": ("vortex_ooo_core", {"WIDTH": 2, "SPEC_BRANCHES": 1, "LSQ": 1}),
}
CORES = tuple(CORE_CONFIGS)


def rtl_sources():
    """RTL sources from rtl/core/files.f, the list lint also uses."""
    return [str(ROOT / line.strip()) for line in FILELIST.read_text().splitlines()
            if line.strip() and not line.strip().startswith("//")]


def build_and_test(config, test_module, test_dir, extra_pythonpath=(), env=None):
    """Build core configuration `config` (a CORE_CONFIGS name) under the cocotb runner
    and run the cocotb tests in `test_module` on it.

    Raises (via the runner) if any cocotb test fails, which fails the calling pytest.
    """
    try:
        from cocotb_tools.runner import get_runner
    except ImportError:  # older cocotb
        from cocotb.runner import get_runner

    toplevel, parameters = CORE_CONFIGS[config]
    test_dir = Path(test_dir)
    build_dir = test_dir / "sim_build" / config
    paths = [str(test_dir), str(Path(__file__).parent), *map(str, extra_pythonpath)]
    os.environ["PYTHONPATH"] = os.pathsep.join(paths + [os.environ.get("PYTHONPATH", "")])
    for k, v in (env or {}).items():
        os.environ[k] = v

    runner = get_runner(os.getenv("SIM", "icarus"))
    runner.build(sources=rtl_sources(), hdl_toplevel=toplevel, build_dir=str(build_dir),
                 parameters=parameters, always=True, timescale=("1ns", "1ps"))
    runner.test(hdl_toplevel=toplevel, test_module=test_module, test_dir=str(test_dir),
                build_dir=str(build_dir), timescale=("1ns", "1ps"))
