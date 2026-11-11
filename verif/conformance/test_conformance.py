"""riscv-tests conformance for Vortex-V (RV64I + RV64M), on the ISS and on the RTL.

The upstream riscv-tests (vendored in third_party/riscv-tests, see VENDORED.md)
are self-checking: each test hardcodes its expected results and reports through
a store to `tohost` (1 = pass, (n<<1)|1 = test case n failed). That makes them an
oracle independent of our own ISS: the RTL<->ISS co-sim can't catch a spec
misreading that both share, but these can.

We supply a CSR-free environment (verif/conformance/rvtest_env/) because the core has
no Zicsr/traps yet. Tests that genuinely need those are listed in EXCLUDED with
the reason; test_inventory_is_complete fails if any upstream test is neither run
nor explicitly excluded, so nothing can drop out silently.

Run (from the repo root):  python3 -m pytest verif/conformance/ -q
Needs: riscv64-unknown-elf-gcc (scripts/setup-toolchain.sh), iverilog, cocotb.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

try:
    import cocotb
except ImportError:  # pragma: no cover - cocotb is a hard dependency of verif/
    cocotb = None

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
TESTS_SRC = ROOT / "third_party" / "riscv-tests" / "isa"
ENV_DIR = HERE / "rvtest_env"
BUILD_DIR = HERE / "build"
RTL_DIR = ROOT / "rtl" / "core"

RESET_PC = 0x8000_0000
TOHOST_ADDR = 0x8000_1000
SUITES = ("rv64ui", "rv64um")
GCC = "riscv64-unknown-elf-gcc"
OBJCOPY = "riscv64-unknown-elf-objcopy"

# Tests not run, with the reason. Revisit each when its prerequisite lands.
EXCLUDED = {
    "rv64ui-fence_i": "needs Zicsr (csrr/csrw) and fence.i with instruction-memory "
                      "coherence; the core has neither yet (M1.1 follow-up)",
    "rv64ui-ma_data": "misaligned load/store: the spec allows either hardware support "
                      "or a trap, and the core implements neither yet",
}

MAX_ISS_STEPS = 200_000
MAX_RTL_CYCLES = 20_000      # largest test retires ~1.4k instructions; divides add ~65 each


def _all_sources():
    srcs = {}
    for suite in SUITES:
        for f in sorted((TESTS_SRC / suite).glob("*.S")):
            srcs[f"{suite}-{f.stem}"] = f
    return srcs


ALL_SOURCES = _all_sources()
RUN_NAMES = sorted(n for n in ALL_SOURCES if n not in EXCLUDED)


def _require_toolchain():
    missing = [t for t in (GCC, OBJCOPY) if shutil.which(t) is None]
    if missing:
        msg = f"missing {missing}; run scripts/setup-toolchain.sh"
        if os.environ.get("CI"):
            pytest.fail(msg)      # never let CI pass by skipping the gate
        pytest.skip(msg)


def _build(name):
    """Compile one test to a flat little-endian image loaded at RESET_PC."""
    src = ALL_SOURCES[name]
    BUILD_DIR.mkdir(exist_ok=True)
    elf = BUILD_DIR / f"{name}.elf"
    binf = BUILD_DIR / f"{name}.bin"
    subprocess.run(
        [GCC, "-march=rv64im", "-mabi=lp64", "-static", "-mcmodel=medany",
         "-nostdlib", "-nostartfiles", "-Wl,--no-warn-rwx-segments",
         f"-I{ENV_DIR}", f"-I{TESTS_SRC / 'macros' / 'scalar'}",
         f"-T{ENV_DIR / 'link.ld'}", str(src), "-o", str(elf)],
        check=True, capture_output=True, text=True)
    subprocess.run([OBJCOPY, "-O", "binary", str(elf), str(binf)],
                   check=True, capture_output=True, text=True)
    return binf


@pytest.fixture(scope="session")
def built():
    _require_toolchain()
    return {n: _build(n) for n in RUN_NAMES}


def test_inventory_is_complete():
    """Every upstream test is either run or explicitly excluded (and exclusions exist)."""
    stale = set(EXCLUDED) - set(ALL_SOURCES)
    assert not stale, f"EXCLUDED names that no longer exist upstream: {stale}"
    assert len(RUN_NAMES) + len(EXCLUDED) == len(ALL_SOURCES)
    assert len(RUN_NAMES) >= 65, f"expected the full rv64ui+rv64um set, got {len(RUN_NAMES)}"


# ---------------------------------------------------------------------------
# Golden ISS
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", RUN_NAMES)
def test_iss_conformance(built, name):
    sys.path.insert(0, str(ROOT))
    from model.iss.rv64i import Hart, Memory
    mem = Memory(base=RESET_PC)
    mem.load_image(bytearray(built[name].read_bytes()), base=RESET_PC)
    hart = Hart(mem=mem, reset_pc=RESET_PC, tohost_addr=TOHOST_ADDR)
    r = hart.run(max_steps=MAX_ISS_STEPS)
    assert r["halted"], f"{name}: no tohost halt within {MAX_ISS_STEPS} steps (pc={r['pc']:#x})"
    assert r["halt_code"] == (True, 0), f"{name}: failed test case {r['halt_code'][1]}"


# ---------------------------------------------------------------------------
# RTL (cocotb under Icarus)
# ---------------------------------------------------------------------------
def _words(binf):
    data = binf.read_bytes()
    data += b"\0" * (-len(data) % 4)
    return [int.from_bytes(data[i:i + 4], "little") for i in range(0, len(data), 4)]


if cocotb is not None:
    @cocotb.test()
    async def conformance_rtl(dut):
        """Run every built riscv-tests image on the RTL; report all failures by name."""
        sys.path.insert(0, str(ROOT / "verif" / "core"))
        from test_core import run_program  # shared memory model + run harness

        build_dir = Path(os.environ["VORTEX_CONF_BUILD"])
        names = os.environ["VORTEX_CONF_TESTS"].split(",")
        failures = []
        for name in names:
            _retires, _mem, state = await run_program(
                dut, _words(build_dir / f"{name}.bin"), max_cycles=MAX_RTL_CYCLES)
            t = state["tohost"]
            if t is None:
                failures.append(f"{name}: no tohost halt within {MAX_RTL_CYCLES} cycles")
            elif t != 1:
                failures.append(f"{name}: failed test case {t >> 1}")
            else:
                dut._log.info("PASS %s", name)
        dut._log.info("riscv-tests on RTL: %d/%d pass", len(names) - len(failures), len(names))
        assert not failures, "RTL conformance failures:\n  " + "\n  ".join(failures)


def test_rtl_conformance(built):
    try:
        from cocotb_tools.runner import get_runner
    except ImportError:  # older cocotb
        from cocotb.runner import get_runner

    sources = [RTL_DIR / line.strip().split("/")[-1]
               for line in (RTL_DIR / "files.f").read_text().splitlines() if line.strip()]
    sim_build = HERE / "sim_build"
    os.environ["PYTHONPATH"] = os.pathsep.join(
        [str(HERE), str(ROOT / "verif" / "core"), os.environ.get("PYTHONPATH", "")])
    os.environ["VORTEX_CONF_BUILD"] = str(BUILD_DIR)
    os.environ["VORTEX_CONF_TESTS"] = ",".join(RUN_NAMES)

    runner = get_runner(os.getenv("SIM", "icarus"))
    runner.build(sources=[str(s) for s in sources], hdl_toplevel="vortex_core",
                 build_dir=str(sim_build), always=True, timescale=("1ns", "1ps"))
    # runner.test raises (SystemExit) if any cocotb test fails, failing this pytest.
    runner.test(hdl_toplevel="vortex_core", test_module="test_conformance",
                build_dir=str(sim_build), test_dir=str(HERE), timescale=("1ns", "1ps"))
