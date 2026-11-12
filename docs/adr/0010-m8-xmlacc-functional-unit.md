# 0010. M8 Xmlacc as an out-of-order functional unit: parameterised latency, own writeback port

- **Status:** Accepted
- **Date:** 2026-11-12

## Context

Since M4 (ADR 0006), Xmlacc ops already go through rename, the issue queue and the ROB. The
accumulator of `macc` is a third renamed source (`rs3`), so there are no false dependences. But
the unit is combinational and shares the single-cycle writeback port with the ALU. A `dot8` or
`macc` is eight 8x8 multiplies, an adder tree and a 64-bit add in one cycle. On the Arty A7 (M9)
that is likely the core's critical path, and the fix is to spread it over more cycles.

A multi-cycle Xmlacc unit is also the rehearsal the plan asks for before the systolic engine
(M11): a long-latency unit with ops in flight that a mispredict must drop.

Options weighed:

- **Iterative unit** (one op at a time, like the divider). Simple, but throughput drops to one op
  every few cycles, and ML kernels issue Xmlacc ops back to back.
- **Pipelined unit** (one op per cycle, fixed latency). Throughput is kept when there is enough
  independent work, for example several output channels with separate accumulators.
- **Hand-split pipeline stages** (products, then adder tree, then accumulate). Best timing, but
  where to cut depends on synthesis results that do not exist until M9.

## Decision

1. **Parameter.** `vortex_ooo_core` gets `XM_LATENCY` (1 to 8, default 1). `XM_LATENCY=1` is
   exactly the ADR 0009 core: Xmlacc on the single-cycle port.
2. **Pipelined unit at `XM_LATENCY` > 1.** The shared `vortex_xmlacc` computes at issue, and its
   result passes through `XM_LATENCY - 1` register stages, so a dependent op issues
   `XM_LATENCY` cycles later. The unit accepts one op per cycle, so it never blocks issue. For
   now the stages are output registers, and synthesis retiming is expected to balance them (to be
   checked at M9). A hand-split pipeline can replace them later without changing the interface.
3. **Own writeback port.** The last stage writes the PRF, wakes dependents, and completes the ROB
   entry (with the `rqs` SAT bit) on its own port. The PRF goes to 6 write ports (ADR 0007
   consequences).
4. **Squash.** On a mispredict, every stage holding an op younger than the branch is
   invalidated, and so is an op entering the unit that cycle. A dropped op writes nothing and
   cannot set SAT (ADR 0008 item 5 gains this step).
5. **Issue stays on port 0** with the other multi-cycle units. A second Xmlacc unit on port 1 is
   follow-on work.
6. **MAC counter.** `dbg_xm_macs` counts int8 multiply-accumulates of completed ops (8 per `dot8`,
   `dot8u`, `dot8su` and `macc`; 0 per `rqs`). Ops dropped by a squash are not counted.

**Verification.**
- A sixth configuration `vortex_ooo_core_xm` (`WIDTH=2`, `SPEC_BRANCHES=1`, `LSQ=1`,
  `XM_LATENCY=3`) runs every suite, plus a 500-seed random soak.
- New directed co-sim tests: two interleaved `macc` chains with independent dot products, a
  `macc` waiting on a late accumulator (from a divide) while younger Xmlacc ops pass it, and a
  mispredict with Xmlacc ops (including a saturating `rqs`) inside the pipeline.
- `test_ooo.py` runs an int8 matrix-vector kernel (4 channels, 16 vectors, 512 MACs). It checks
  the MAC count exactly and that `XM_LATENCY=3` keeps at least 85% of the single-cycle MACs per
  cycle.
- Mutation checks on the new logic (see the PR).

## Consequences

- **Easier:** the Xmlacc path can be made as deep as timing needs, with no change to rename,
  issue or commit. The systolic engine (M11) can reuse the same in-flight and squash pattern.
- **Accepted:** dependent chains (one accumulator) slow down by `XM_LATENCY` per op. Kernels need
  several accumulators to hide it. Throughput is bounded by issue port 0, which also carries
  loads, multiplies and divides: the kernel above reaches about 5.3 MACs/cycle, where the
  in-order core reaches about 5.2.
- **Follow-on:** a second Xmlacc unit on port 1, hand-split stages once M9 timing exists, and
  `mlstat` as a real CSR once Zicsr lands (SAT is still a debug port).
