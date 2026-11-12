# 0012. Block-RAM memory interface: SYNC_MEM, next-pc fetch and a one-cycle load stage

- **Status:** Accepted
- **Date:** 2026-11-12

## Context

The M1 memory interface reads combinationally: the core puts out an address and uses the data in
the same cycle, for both instruction fetch and loads. FPGA block RAM registers its read, so data
comes back one cycle after the address. ADR 0011 therefore built the SoC RAM as distributed (LUT)
RAM, which costs LUTs and makes a long fetch-to-decode path (KNOWN-ISSUES I18). The board run
(M9) should use block RAM.

Options weighed for fetch:

- **A fetch stage** (register the instruction, decode a cycle later). The usual fix, but every
  redirect (taken branch, JALR, mispredict, a predicted JAL or branch at decode) costs one more
  bubble, and the predictor and checkpoint code assume decode sees pc's instruction.
- **Fetch from the next pc.** The memory is given the pc the core will hold next cycle, which the
  core already computes. The instruction for pc then arrives exactly when pc does, with no bubble.
  The cost is timing: the next-pc logic (decode, prediction, branch resolution, squash) now feeds
  the RAM address input. Whether that meets 50 MHz is a question for Vivado.

For loads, the result arrives a cycle after the load runs, so writeback has to wait a cycle.

## Decision

1. **Parameter.** `vortex_ooo_core` gets `SYNC_MEM` (0 or 1, default 0). `SYNC_MEM=0` is exactly
   the ADR 0011 core. The in-order `vortex_core` keeps the combinational interface.
2. **Next-pc fetch.** The pc update moves into one combinational next-pc block (`pc_n`): dispatch,
   then a resolving branch or JALR, then a squash, later sources winning as before. With
   `SYNC_MEM=1`, `imem_addr` is `pc_n` (and `RESET_PC` while in reset), and `imem_addr1` is
   `imem_addr + 4`. The core's own uses of "pc + 4" now use an internal `pc1`, not the port.
3. **Load stage.** A load still runs (picks its queue entry, drives the data port, marks itself
   done) in one cycle. With `SYNC_MEM=1` its tags, and a forwarded value, wait one cycle in
   `lw_*`, and the load writes the register, wakes dependents and completes its ROB entry when the
   data arrives. A squash in the cycle the load runs drops it if it is younger than the branch,
   because its ROB entry and destination register can be handed out again the next cycle.
4. **Memory side.** `vortex_ram` gets `SYNC`: every read port registers its output. The SoC
   passes `SYNC_MEM` to the core and the RAM, and registers the MMIO read data and the
   RAM/MMIO select so they line up. `arty_a7_top` builds `SYNC_MEM=1`. The harness has a
   matching memory model (selected from the configuration's `SYNC_MEM`).

**Verification.**
- A new configuration `vortex_ooo_core_sync` (`WIDTH=2`, `SPEC_BRANCHES=1`, `LSQ=1`,
  `XM_LATENCY=3`, `SYNC_MEM=1`) runs every suite, plus a 500-seed soak. The same core against
  the combinational memory model fails, so the latency is really being exercised.
- `verif/soc` runs the C program on a block-RAM SoC (`soc_ooo_sync`): 4374 cycles against 4265
  with combinational memory.
- Mutation checks: fetch from pc instead of next pc; write back loads in the cycle they run;
  keep a load a squash made dead; drop the squash from next pc. Each is caught by the suites.

## Consequences

- **Easier:** the program and data fit in block RAM on the board, and the SoC is what an FPGA
  build expects.
- **Accepted:** load-to-use latency grows by one cycle (about 2.6% more cycles on the SoC
  program). The next-pc path into the RAM address is long. If Vivado says it does not meet
  timing, the fallback is a fetch stage (option 1 above), which a later ADR would add.
- **Follow-on:** the in-order core has no `SYNC_MEM`; the in-order SoC stays on distributed RAM.
