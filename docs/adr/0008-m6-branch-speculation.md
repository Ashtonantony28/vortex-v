# 0008. M6 branch speculation: bimodal predictor, one predicted branch in flight, checkpoint recovery

- **Status:** Accepted
- **Date:** 2026-11-12

## Context

Under ADR 0006/0007, dispatch stops at every conditional branch until it executes. On the
constrained-random mix the two-wide core is then barely faster than one-wide
(KNOWN-ISSUES I9). M6 adds a predictor and speculative execution with misprediction recovery.

Speculation is the riskiest feature so far. A squash that misses one structure (RAT, free
list, ROB, issue queue, memory queue, a long-running divide) corrupts state, sometimes only
much later. CLAUDE.md asks that speculation depth beyond 1 be gated behind a parameter that
defaults off. The options weighed:

- **Predictor.** gshare or TAGE predict better than a bimodal table, but the gain is small at
  one branch in flight, and they add history-recovery state to every squash. A table of 2-bit
  counters indexed by PC needs no recovery and is the standard baseline.
- **Depth.** With several unresolved branches, every branch needs its own checkpoint and
  squash has to pick the right one. With one, there is a single checkpoint, and "younger than
  the branch" is a single ROB age compare.
- **JALR.** Its target is a register value. Predicting it needs a BTB or return stack, and it
  is rarer than conditional branches. It keeps stalling for now.

## Decision

1. **Parameters.** `vortex_ooo_core` gets `SPEC_BRANCHES` (0 or 1, default 0) and `BHT_ENTRIES`
   (default 64). `SPEC_BRANCHES=0` is exactly the ADR 0007 core. Depth above 1 is out of scope
   for M6.
2. **Prediction at decode.** Each conditional branch reads a 2-bit counter indexed by
   `pc[log2(BHT_ENTRIES)+1:2]`, and predicts taken when bit 1 is set. Counters reset to weakly
   not-taken. The taken target is `pc + imm`, known at decode, so no BTB is needed. Fetch
   continues at the predicted pc. The counter trains when the branch executes.
3. **One predicted branch in flight.** A second conditional branch waits at dispatch until the
   first resolves. A branch still ends its dispatch group (ADR 0007 item 3). JALR still stops
   dispatch until it executes. JAL redirects at decode.
4. **Checkpoint at dispatch.** The checkpoint holds the speculative RAT right after the branch
   (including a same-group slot 0 write), the free-list head, and the branch's ROB index.
5. **Recovery on a mispredict** (at the branch's execute cycle; dispatch is held off that cycle):
   - Restore the RAT and the free-list head. The free-list count gets back every preg popped since the checkpoint.
   - Truncate the ROB to the branch.
   - Invalidate issue-queue entries younger than the branch.
   - Truncate the memory queue to its entries older than the branch. They are a prefix, and a younger load may be leaving the head that same cycle.
   - Mark a multiplier or divider op younger than the branch as dead, so its result is dropped when it finishes.
   - Clear `fe_wait`, since any JALR waiting now is younger than the branch.
   - Redirect fetch to the correct pc.

   Results written this cycle into squashed pregs are harmless: those pregs are free again and are re-marked not-ready when reallocated.
6. **What may run speculatively.** ALU, multiply, divide, Xmlacc and loads may all run
   speculatively. Loads have no side effects in this memory system. Stores still write only at
   commit, so memory and `tohost` are never touched by a wrong path.

**Verification.**
- A fourth configuration, `vortex_ooo_core_spec` (`WIDTH=2`, `SPEC_BRANCHES=1`), runs every
  suite.
- The random co-sim reports predicted and mispredicted branch counts: random branches
  mispredict about half the time, so recovery is exercised hundreds of times per run.
- `test_ooo.py` proves speculation on a branchy loop: every branch predicted, at least one
  recovery, and at most 0.8x the cycles of the non-speculative two-wide core.
- Mutation checks drop each recovery step in turn.

## Consequences

- **Easier:** depth 1 keeps recovery to one checkpoint and age compares. Deeper speculation
  later means more checkpoints and choosing which one to restore, not a redesign.
- **Accepted:** with depth 1, a loop whose body has a second branch serialises on it. JALR (calls
  and returns) still stall. Random code gains little, because its branches are random by
  construction. A return stack, a BTB and depth > 1 are follow-on work.
- **Risk:** this is the first time the core throws work away. Every structure that holds
  in-flight state must be covered by the squash. New structures added later (the M7 LSQ, the M8
  ML unit) must join the squash, and the mutation checks must cover them.
