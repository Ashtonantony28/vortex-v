# 0013. A second Xmlacc unit on issue port 1 (XM_PORTS)

- **Status:** Accepted
- **Date:** 2026-11-12

## Context

ADR 0010 made Xmlacc a functional unit that issues only on port 0, which it shares with loads,
multiplies and divides. On the int8 matrix-vector kernel in `verif/core/test_ooo.py` (4 output
channels, 16 activation vectors, 512 MACs) port 0 is the bottleneck: about 5.3 MACs/cycle, close
to the in-order core's 5.2 (KNOWN-ISSUES I16). Port 1 only runs ALU ops and branches, so in this
kernel it sits mostly idle while maccs queue behind loads on port 0.

Options weighed:

- **Make the one unit wider** (more int8 lanes per op). Changes the ISA-visible op shape and the
  golden model, which is out of scope for a microarchitecture change.
- **A second unit on port 1.** No ISA change, reuses `vortex_xmlacc` as is, and the kernel's four
  accumulators are independent, so two units can each take one per cycle. Costs a second
  multiplier array, a third register-file read port for port 1 (the `macc` accumulator), and one
  more writeback port when the unit is pipelined.

## Decision

1. **Parameter.** `vortex_ooo_core` gets `XM_PORTS` (1 or 2, default 1). `XM_PORTS=2` needs
   `WIDTH=2`; anything else stops elaboration with `$fatal`. `XM_PORTS=1` is exactly the ADR 0012
   core.
2. **Selection.** Port 1's pick accepts an ALU op, or with `XM_PORTS=2` an Xmlacc op whose third
   source (the accumulator) is ready as well as the first two. Port 0 still accepts Xmlacc ops,
   so either port can take one, and the oldest-first pick decides which.
3. **Second unit.** A second `vortex_xmlacc` reads port 1's three operands. With `XM_LATENCY=1`
   its result goes out on port 1's existing writeback. With `XM_LATENCY>1` it enters its own
   copy of the ADR 0010 pipeline: the pipelines are now arrays indexed by unit, each with the
   same per-stage squash kill and its own writeback port. Port 1's same-cycle writeback
   (`p1_fire`) is then suppressed for the Xmlacc op.
4. **Status and counting.** A requantize on port 1 sets the ROB's saturation bit the same way as
   on port 0, so `mlstat` is unchanged. `dbg_xm_macs` adds up to two MAC ops per cycle.
5. **Builds.** `vortex_soc` passes `XM_PORTS` through and stops elaboration on `XM_PORTS=2` with
   the in-order core. `arty_a7_top` builds `XM_PORTS=2`. The board configuration in the
   verification suites, `vortex_ooo_core_sync`, now has `XM_PORTS=2`, and so does the
   `soc_ooo_sync` SoC test.

**Verification.**
- Two configurations have the second unit: `vortex_ooo_core_xm2` (`WIDTH=2`, `SPEC_BRANCHES=1`,
  `LSQ=1`, `XM_PORTS=2`, single-cycle Xmlacc, so port 1 writes the result back itself) and the
  board configuration `vortex_ooo_core_sync` (adds `XM_LATENCY=3` and `SYNC_MEM=1`). Both run
  every suite; `vortex_ooo_core_sync` also passes a 500-seed random co-sim soak.
- `verif/crv` gets an Xmlacc-heavy program mix (30% Xmlacc ops, 14% branches) that runs on every
  configuration with a pipelined or second unit (`test_random_cosim_xm_mix`), so mispredicts
  land on maccs in flight.
- `test_ooo.py::test_second_xmlacc_unit_raises_mac_throughput`: the kernel takes 67 cycles
  (7.64 MACs/cycle) against 97 (5.28) for `vortex_ooo_core_xm` with one unit, and the MAC
  counter reads exactly 512. The check needs at least 1.25x. The two-unit core also has
  block-RAM memory, which only slows it down, so the comparison is conservative.
- Mutation checks, each caught by the suites: issue on port 1 without the accumulator ready;
  write port 1's Xmlacc op back in the issue cycle with the ALU result when pipelined; send the
  ALU result instead of the Xmlacc result (single-cycle); skip the squash in unit 1's later
  pipeline stages; count only unit 0's MACs; drop port 1's saturation bit (single-cycle and
  pipelined).
- One mutant survives and is equivalent: skipping the squash on the cycle an op *enters* unit 1.
  Port 1 picks first and takes the oldest ready ALU-class op, and branches are ALU-class, so when
  port 0 resolves a branch, port 1's op is always older than it. A probe over 100 Xmlacc-heavy
  seeds saw 324 cycles with a port-1 Xmlacc issue and a squash, all older than the branch. The
  kill stays in the RTL so the pipeline does not depend on the pick order.

## Consequences

- **Easier:** int8 kernels get about 1.45x the MAC rate with no software change.
- **Accepted:** a second multiplier array (DSP slices on the Arty A7), a third read port on the
  register file for port 1, and one more writeback/wakeup port when pipelined. Whether this
  still meets 50 MHz is a question for Vivado (M9). If it does not, `XM_PORTS=1` is a parameter
  change in `arty_a7_top`.
- **Follow-on:** with Xmlacc off port 0's critical path the kernel is now limited by loads on
  port 0. A second load port is a larger change (two LSQ ports) and is not planned yet.
