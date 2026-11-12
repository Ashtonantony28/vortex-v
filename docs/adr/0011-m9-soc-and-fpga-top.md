# 0011. M9 SoC and FPGA top: unified RAM, MMIO UART/LEDs, ordered device loads

- **Status:** Accepted
- **Date:** 2026-11-12

## Context

M9 brings the core up on the Arty A7-100T (ADR 0003). Until now the core has only run inside the
cocotb harness, which serves memory from Python and watches `tohost`. On a board there is no
harness. The core needs a memory holding the program, a way to print (the USB-UART), and a
visible pass/fail signal. Vivado and the board are not available in the CI or cloud environment,
so everything that can be built and checked in simulation should be finished first. Synthesis,
timing and the on-board run come after.

The core's M1 memory interface reads combinationally (instruction and data in the same cycle).
FPGA block RAM reads synchronously. Changing the interface means a fetch stage and a load stage
in both cores, and is a bigger change than this step.

The LSQ (ADR 0009) lets loads run ahead of older stores to other addresses. For RAM that is
correct. For a device it is not: a program that writes the UART and then reads the UART's busy
bit must see the write. The SoC test showed exactly this: with `LSQ=1`, a status read issued
while the UART write waited to commit behind a divide read "not busy".

## Decision

1. **`vortex_soc`** (`rtl/soc/`): one core, either `vortex_core` or `vortex_ooo_core`, with all of
   its parameters passed through. It has one unified RAM and a small MMIO block. Address map:
   - RAM at `0x8000_0000`, 16 KiB by default. `tohost` (`0x8000_1000`) stays inside it, so the M1
     pass/fail contract holds on the board.
   - MMIO at `0x1000_0000`: UART TX data, UART status (busy), 4 LEDs, and a 64-bit cycle counter.
   - Every MMIO read is free of side effects, so a speculative read is harmless (KNOWN-ISSUES
     I12). Writes happen at commit.
2. **`vortex_ram`** (`rtl/fpga/`, ADR 0003 wrapper): behavioural, with two fetch ports, one data
   read port and one byte-enabled write port. It reads combinationally, so it maps to distributed
   RAM. 16 KiB is affordable in LUTs on the A7-100T. Moving to block RAM needs the synchronous
   interface above (follow-on).
3. **Device loads run in order.** `vortex_ooo_core` gets `IO_LIMIT` (default `0x8000_0000`). In LSQ
   mode, a load below it runs only from the memory-queue head, after every older memory
   operation. RAM loads are unaffected, and so is every existing suite (all their data is in RAM).
4. **UART:** `vortex_uart_tx`, 8N1, with `CLKS_PER_BIT` as a parameter (115200 baud on the board,
   4 clocks per bit in simulation). Software polls the busy bit. There is no receiver yet.
5. **Board top:** `arty_a7_top` (`rtl/fpga/`) contains:
   - `vortex_clkgen`: a pass-through in generic builds, an MMCM (100 to 50 MHz) when
     `VORTEX_XILINX` is defined
   - `vortex_reset_sync`: asynchronous assert, synchronous release, from CK_RST and the MMCM lock
   - the SoC
   - the RGB LED: blue while running, then green for pass or red for fail

   `fpga/arty_a7_100t.xdc` holds the pins. `fpga/build.tcl` is the Vivado non-project flow.
   Neither has been run yet.
6. **Software:** `sw/` holds the bare-metal start-up (`crt0.S`), a linker script and
   `vortex.h`. `vortex.h` has the MMIO helpers and the Xmlacc ops as `.insn`. `sw/build.py`
   compiles with riscv64-unknown-elf-gcc for RV64IM and writes the RAM image.
7. **Reset style:** SoC flops use an asynchronous active-low reset, like the cores, and the board
   top synchronises its release. CLAUDE.md asks modules that do not use a synchronous reset to
   say so, and each one does.

**Verification.**
- `verif/soc/test_soc.py` builds `sw/hello` with gcc, runs it on the SoC (in-order core, default
  OoO core, and the OoO core with every feature on), and decodes the UART line. The program
  prints a greeting, checks the device ordering above, computes a 32-element int8 dot product on
  Xmlacc and checks it against plain C, and prints the cycle counter. The test also checks the
  LEDs and the pass flag.
- Verilator `-Wall` lint covers `vortex_soc` (in-order and OoO) and `arty_a7_top` in CI.
- A yosys synthesis estimate was attempted, but yosys 0.33 cannot parse the cores' package
  imports. Utilisation and timing wait for Vivado.

## Consequences

- **Easier:** the same C program runs in simulation and, once built, on the board. CoreMark and
  the M10 TinyML kernels can use `sw/` as is.
- **Accepted:** combinational memory means distributed RAM and a long path from fetch address
  through decode. The 50 MHz target is a guess until Vivado reports timing. There is no UART
  receiver, and device reads must stay free of side effects until the core can hold a load until
  it is non-speculative.
- **Follow-on (rest of M9):** a Vivado run (utilisation, timing, bitstream), the on-board
  smoke test, a synchronous-read memory interface for block RAM, a UART receiver, and CoreMark.
