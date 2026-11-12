"""Run a bare-metal C program on the Vortex-V SoC (ADR 0011).

sw/hello is compiled with riscv64-unknown-elf-gcc and loaded into the SoC RAM.
The test decodes the SoC's UART line (8N1) and checks what the program prints:
a greeting, a device-ordering check, an int8 dot product computed on the Xmlacc unit and checked by the
program against plain C, and the cycle counter. It also checks the LEDs and the
tohost pass flag.

Run (from the repo root):  python3 -m pytest verif/soc -q
Needs riscv64-unknown-elf-gcc (scripts/setup-toolchain.sh); skipped without it
unless CI is set.
"""
import os
import shutil
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "sw"))
sys.path.insert(0, str(ROOT / "verif" / "core"))

CLKS_PER_BIT = 4          # fast UART for simulation
MAX_CYCLES = 60_000
EXPECTED_DOT = 0x2e8     # sum of a[i]*b[i] in sw/hello/hello.c (744)

# SoC builds: the in-order core, the default OoO core, and every OoO feature on
SOC_CONFIGS = {
    "soc_inorder": {"CORE_OOO": 0},
    "soc_ooo": {"CORE_OOO": 1},
    "soc_ooo_full": {"CORE_OOO": 1, "WIDTH": 2, "SPEC_BRANCHES": 1, "LSQ": 1, "XM_LATENCY": 3},
}

try:
    import cocotb
    from cocotb.clock import Clock
    from cocotb.triggers import FallingEdge, RisingEdge
except ImportError:  # pragma: no cover
    cocotb = None

if cocotb is not None:
    @cocotb.test()
    async def soc_hello(dut):
        """Reset, run until tohost, decode the UART text."""
        cocotb.start_soon(Clock(dut.clk, 10, units="ns").start())
        dut.rst_n.value = 0
        for _ in range(3):
            await RisingEdge(dut.clk)
        await FallingEdge(dut.clk)
        dut.rst_n.value = 1

        text = bytearray()
        start = None          # cycle the current frame's start bit was first seen
        byte = 0
        cyc = 0
        done_at = None
        while cyc < MAX_CYCLES:
            await RisingEdge(dut.clk)
            await FallingEdge(dut.clk)
            cyc += 1
            line = int(dut.uart_tx.value)
            if start is None:
                if line == 0:
                    start, byte = cyc, 0
            else:
                t = cyc - start
                bit, phase = divmod(t, CLKS_PER_BIT)
                if phase == CLKS_PER_BIT // 2:
                    if bit == 0:
                        assert line == 0, f"glitch on the start bit at cycle {cyc}"
                    elif 1 <= bit <= 8:
                        byte |= line << (bit - 1)
                    elif bit == 9:
                        assert line == 1, f"framing error (stop bit low) at cycle {cyc}"
                        text.append(byte)
                        start = None
            if done_at is None and int(dut.done.value) == 1:
                done_at = cyc
            # stop once the program is done and the last frame has been sent
            if done_at is not None and start is None and cyc > done_at + 12 * CLKS_PER_BIT:
                break
        out = text.decode("ascii", errors="replace")
        dut._log.info("UART output:\n%s", out)
        assert done_at is not None, f"no tohost store within {MAX_CYCLES} cycles; UART: {out!r}"
        assert int(getattr(dut, "pass").value) == 1, \
            f"program reported failure; UART: {out!r}"
        lines = out.splitlines()
        assert lines[0] == "Vortex-V SoC: hello", lines
        assert lines[1] == ". mmio order ok", lines
        assert lines[2] == f"dot32 = {EXPECTED_DOT:016x} xmlacc ok", lines
        assert lines[3].startswith("cycles = ") and int(lines[3][9:], 16) > 0, lines
        assert int(dut.leds.value) == 0xA


def _require_gcc():
    if shutil.which("riscv64-unknown-elf-gcc") is None:
        if os.environ.get("CI"):
            pytest.fail("riscv64-unknown-elf-gcc is required in CI")
        pytest.skip("riscv64-unknown-elf-gcc not installed")


@pytest.mark.parametrize("config", list(SOC_CONFIGS))
def test_soc_runs_hello(config):
    _require_gcc()
    from build import build
    from cores import rtl_sources
    try:
        from cocotb_tools.runner import get_runner
    except ImportError:  # older cocotb
        from cocotb.runner import get_runner

    build_dir = HERE / "sim_build" / config
    hexf = build("hello", build_dir / "sw")
    soc_srcs = [str(ROOT / line.strip()) for line in (ROOT / "rtl/soc/files.f").read_text()
                .splitlines() if line.strip() and not line.strip().startswith("//")]
    params = dict(SOC_CONFIGS[config], CLKS_PER_BIT=CLKS_PER_BIT, INIT_FILE=f'"{hexf}"')
    os.environ["PYTHONPATH"] = os.pathsep.join([str(HERE), os.environ.get("PYTHONPATH", "")])
    runner = get_runner(os.getenv("SIM", "icarus"))
    runner.build(sources=rtl_sources() + soc_srcs, hdl_toplevel="vortex_soc",
                 build_dir=str(build_dir), parameters=params, always=True,
                 timescale=("1ns", "1ps"))
    runner.test(hdl_toplevel="vortex_soc", test_module="test_soc", test_dir=str(HERE),
                build_dir=str(build_dir), timescale=("1ns", "1ps"))
