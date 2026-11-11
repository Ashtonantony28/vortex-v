# 0006. M4 out-of-order microarchitecture: PRF renaming, ROB, unified issue queue, no speculation

- **Status:** Accepted
- **Date:** 2026-11-11

## Context

M4 is the first out-of-order milestone: "rename + ROB + single-issue out-of-order execution"
(ROADMAP). The in-order RV64IM + Xmlacc core passes lockstep co-sim against the ISS and the
riscv-tests rv64ui/rv64um suites, so per CLAUDE.md directive 3 those features may now go
out-of-order. M5 widens to 2-wide (ADR 0002), M6 adds branch prediction and speculative
recovery, M7 adds a real load/store queue with store-to-load forwarding and formal on it.

Each technique an OoO core needs brings its own class of bug: renaming (stale or leaked
physical registers), wakeup/select (lost wakeups, starvation), in-order commit (imprecise
state), speculation (incomplete squash), and memory ordering (a load passing an aliasing
store). If M4 introduces all of them at once, a co-sim mismatch could come from any of them.
The choices below make M4 introduce only the first three, so every M4 bug is a rename,
scheduling or commit bug, and the later classes arrive one milestone at a time.

Options weighed:

- **Renaming.** (a) ROB holds results (P6 style): values are copied from ROB to the register
  file at commit, and operands are read from either place. (b) Merged physical register file
  (R10K/BOOM style): a RAT maps architectural to physical registers, a free list recycles them,
  and the ROB holds only metadata. (b) has no value copy at commit and no ROB read ports, and
  M6 recovery becomes a RAT checkpoint restore. It is the standard choice for new designs.
- **Scheduling.** Distributed per-FU issue queues (the original plan) scale better, but a
  unified queue with per-FU select is simpler to get right at 1-wide and splits naturally at M5.
- **Branches in M4.** Full speculation needs squash and recovery, which is M6's job. The
  no-speculation alternative stops dispatch after a conditional branch or JALR until it
  resolves. JAL needs no stall because its target is known at decode.
- **Memory in M4.** A full LSQ with forwarding is M7. The conservative alternative keeps memory
  operations in program order among themselves and performs stores at commit.

## Decision

We will build M4 as a new top module `vortex_ooo_core` with exactly the same ports as
`vortex_core` (docs/M1-INTERFACE-v1.md, including the `dbg_*` retire ports), and keep the
in-order core as the reference baseline for IPC comparison and as a fallback.

1. **Front end, 1-wide.** Fetch and decode one instruction per cycle. JAL redirects at decode.
   After a conditional branch or JALR, dispatch stops until that instruction executes and
   redirects fetch. Nothing is fetched down a wrong path, so there is nothing to squash.
2. **Renaming.** A merged physical register file of `NUM_PREGS` (default 64) × 64-bit flops,
   a speculative RAT, an architectural (committed) RAT, and a FIFO free list. `p0` is hardwired
   to zero and `x0` always maps to it. Each ROB entry records `{arch rd, new preg, old preg}`.
   The old preg is freed at commit. Each preg has a ready bit, cleared at rename and set at
   writeback.
3. **ROB.** `ROB_DEPTH` entries (default 32), allocated in order at dispatch and committed in
   order, one per cycle in M4. An entry commits when it is complete. Commit drives the `dbg_*`
   retire ports, so the existing ISS co-sim and conformance suites check the OoO core unchanged.
4. **Scheduling.** One unified, age-ordered issue queue of `IQ_DEPTH` entries (default 16)
   holding source preg tags and ready bits. Each cycle at most one instruction issues: the oldest
   one whose sources are ready and whose FU can accept it. Issue reads up to three PRF operands
   (three ports, for Xmlacc `macc`).
5. **Functional units.** ALU and branch (1 cycle), multiplier (1 cycle), divider (the existing
   iterative unit, non-pipelined, one op in flight), Xmlacc (1 cycle). Each FU has its own
   writeback port and wakeup broadcast, so a divide finishing in the same cycle as an ALU op
   needs no arbitration. Completion is genuinely out of order: independent ALU ops issue,
   complete and wait in the ROB behind an older in-flight divide.
6. **Memory, in order.** Loads and stores enter an in-order memory queue at dispatch. Only its
   head may access the data port. A load executes when its address operand is ready. A store
   executes only when it is the ROB head, and writes memory as it commits. So stores are never
   speculative or out of order, and loads never pass older stores. `tohost` therefore only fires
   for committed stores.
7. **Xmlacc `mlstat.SAT`.** It is set at commit of a saturating `rqs`, not at execute.
8. **Parameters, no literals.** `NUM_PREGS`, `ROB_DEPTH`, `IQ_DEPTH` and `MEMQ_DEPTH` are
   parameters checked by elaboration-time assertions (e.g. `NUM_PREGS >= 32 + ROB_DEPTH`
   fails, so the free list can never underflow while the ROB has space).

**Verification for M4** (extends ADR 0005; Spike and riscv-dv are not yet available, see M2):

- Every existing suite is parameterised by toplevel and run on both cores: directed cocotb
  tests, lockstep co-sim against the ISS, and riscv-tests rv64ui/rv64um.
- A seeded constrained-random program generator (RV64IM + Xmlacc, terminating by
  construction: forward branches and bounded loops, address-safe loads and stores) is
  co-simulated against the ISS on both cores. This stands in for riscv-dv until it is installed.
- OoO is proven, not assumed: a debug completion stream lets tests assert that some
  instruction completed before an older one, and that the OoO core needs fewer cycles than
  the in-order core on a program with independent work behind a divide.

## Consequences

- The existing harness carries over: same ports and retire contract, so co-sim and conformance
  apply on day one. Generalising the testbench to take a toplevel is the first step.
- Throughput in M4 is limited: no speculation means the window is mostly straight-line code
  between branches, and in-order memory serialises loads. That is deliberate. The M4 claim is
  correct out-of-order execution with measurable overlap behind long-latency ops. Real IPC gains
  arrive with M5 (width), M6 (speculation) and M7 (LSQ).
- M6 needs a RAT checkpoint per in-flight branch and ROB/IQ/free-list squash. The PRF design
  makes that a restore, not a value rollback. M7 replaces the in-order memory queue.
- Two cores now share decode, FU and package code. Shared pieces are refactored into modules
  both instantiate, never copied, so a fix (like the I5 rqs fix) lands once.
- A flop-based PRF with three read and four write ports is fine at 64 entries on Artix-7, but
  may need LUTRAM banking or fewer ports at M9 timing closure. Revisit then.
