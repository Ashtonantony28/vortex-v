# 0009. M7 load/store queue: out-of-order loads with store-to-load forwarding; L1-D deferred

- **Status:** Accepted
- **Date:** 2026-11-12

## Context

Under ADR 0006 the memory queue is in order: a load runs only at the queue head, so a store
that waits to commit (it writes memory only at commit) holds every younger load behind it,
even loads to other addresses. Commit order is set by the ROB, so one slow instruction ahead
of a store stalls all memory work behind it.

The roadmap's M7 also names a non-blocking L1 data cache. The memory behind the core today is
a single-cycle combinational port in the harness. A cache in front of it would change no
result and could not show a miss penalty until there is a slower memory (an AXI/DDR model or
the FPGA's DDR, M9). Building the queue first gives the cache something to feed.

CLAUDE.md scopes formal to the LSU forwarding and the ROB, through SymbiYosys. SymbiYosys could
not be installed in this environment (only distribution packages are allowed, and it is not
packaged). Plain yosys is.

Options weighed for the queue:

- **Separate load and store queues** with age tags. Standard, but every load-store compare needs
  an age compare, and squash and commit have two queues to keep in step.
- **One unified queue in program order** (the existing memory queue) with loads allowed to run
  from any position. Age is position, so "older store" is "earlier in the queue". Squash stays a
  prefix truncation (ADR 0008).

Options for loads that overlap an older store:

- Forward whenever every load byte is covered by some store, merging bytes from several stores.
  Correct but costly: a byte-select mux per store per byte.
- Forward only from the youngest overlapping store, and only when it covers the whole load.
  Otherwise wait until the stores ahead commit. One mux, and the common case (a load reading
  back what was just stored, same size or smaller) still forwards.

## Decision

1. **Parameter.** `vortex_ooo_core` gets `LSQ` (0 or 1, default 0). `LSQ=0` is exactly the
   ADR 0008 core. A new configuration `vortex_ooo_core_lsq` (`WIDTH=2`, `SPEC_BRANCHES=1`,
   `LSQ=1`) runs every suite.
2. **Unified queue.** The memory queue stays one program-ordered queue (`MEMQ_DEPTH`). Stores
   still write memory only at commit, from the queue head, as before.
3. **Load issue.** A load whose address is known may run from any queue position when every
   older store in the queue has a known address. The oldest such load runs; one load per cycle.
   A committing store has the memory port. A load that forwards does not need the port, so it
   may run in the same cycle as a store commit.
4. **Forwarding.** A new module `vortex_lsq_fwd` compares the load with the older stores. If the
   youngest overlapping store covers every load byte, the load takes its bytes from that store's
   data (shifted by the address difference, then sign- or zero-extended as usual). If it only
   partly overlaps, the load waits. If none overlaps, the load reads memory.
5. **Leaving the queue.** A load stays in the queue after it runs (marked done) and leaves when it
   commits, in either commit slot. Up to two entries leave per cycle (a store in slot 0 and a
   load in slot 1, or two loads). So every queue entry has a live ROB entry, and the squash rule
   of ADR 0008 (keep the queue prefix older than the branch) still holds. Its ROB entry completes
   when it runs, so commit is not held back by the queue.
6. **L1-D deferred.** The non-blocking L1 data cache moves to a follow-on milestone, once a memory
   model with latency exists. The queue's single memory port is where it will attach.
7. **Formal with yosys.** `verif/formal/lsq_fwd_props.sv` checks `vortex_lsq_fwd` against a
   byte-level reference that applies the older stores to memory oldest first. It proves that the
   unit waits exactly when an older address is unknown, that a forwarded load returns exactly the
   reference bytes, that a memory read happens only when no older store overlaps, and that it
   never waits on a partial overlap that does not exist. The proof uses yosys
   `sat -prove-asserts` (the unit is combinational, so a SAT proof covers every input) at
   XLEN=32 with 3 stores. Byte forwarding does not depend on the width. ROB formal stays open.

**Verification.**
- `vortex_ooo_core_lsq` runs the directed tests, ISS co-sim, riscv-tests and the random co-sim,
  plus a 500-seed soak.
- The random generator now aims 40% of loads and stores at the doubleword of a recent store, so
  full forwards, partial overlaps and store-after-store overlaps are common.
- `test_ooo.py` proves the LSQ: behind a store waiting on a divide, a load forwards, the other
  loads run ahead of the queue head (debug counters `dbg_lsq_forwards`, `dbg_lsq_early`), and the
  run takes at most 0.95x the cycles of the in-order queue (measured 94 vs 105).
- `verif/formal/test_formal.py` runs the proof, and runs it again on a mutant that drops the
  byte shift, which must fail. CI installs yosys.
- Mutation checks, each caught by the suites: ignore an unknown older address, run a load that
  partly overlaps, let a memory-reading load share the cycle with a store commit, let the oldest
  overlapping store win, pop a load once it has run instead of at commit, never pop a load
  committing in slot 1, and run loads only from the queue head (caught by `test_ooo.py`).

## Consequences

- **Easier:** loads no longer wait for unrelated stores. Squash and commit keep one queue.
  The cache, when it comes, attaches to one port.
- **Accepted:** one load per cycle and one memory port. A load that partly overlaps an older
  store waits for that store to commit. Loads stay in the queue until commit, so the queue fills
  sooner than before. No memory-dependence prediction is needed, because loads never run ahead
  of an older store with an unknown address.
- **Risk:** the forwarding unit is the first logic proven formally, and only at 32 bits.
  The ROB property and the cache remain open for M7's follow-on.
