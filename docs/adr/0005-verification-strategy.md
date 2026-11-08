# 0005. Verification strategy: Spike lockstep co-sim + riscv-dv + scoped formal + bit-exact golden, in-order before OoO

- **Status:** Accepted
- **Date:** 2026-11-06

## Context

An out-of-order superscalar core with a custom ISA extension has an enormous state space and
many subtle correctness traps: register renaming and ROB recovery, speculative execution and
misprediction rollback, store-to-load forwarding in the load/store unit, memory ordering, and
the exact numerical behavior of the Xmlacc int8 ops (saturation, rounding, requantize). Directed
unit tests alone cannot cover this; neither can any single technique. We need a layered strategy,
and we need to sequence the build so correctness is established on a simple machine before
complexity is added.

Techniques considered and their roles:

- **Golden ISA simulator (Spike).** Spike is the reference RISC-V ISA simulator. Extended with
  an **Xmlacc plugin** it becomes the architectural oracle for *every* instruction, including the
  ML ops. Running the core in **lockstep co-simulation** — comparing architectural state against
  Spike on **every retired instruction** — catches divergence the moment it happens, which is
  exactly what an OoO core (where the bug may be far from its cause) needs.
- **riscv-dv constrained-random.** Google's riscv-dv generates large, legal, constrained-random
  instruction streams that exercise corner cases directed tests miss, co-simulated against Spike.
- **Formal (SymbiYosys).** Full-core formal is intractable, but formal is the *right* tool for a
  few bounded, high-risk, high-value properties. We scope it to **LSU store-to-load forwarding**
  (a classic source of subtle, data-dependent bugs) and the **ROB** (in-order retirement,
  recovery correctness). These are small enough to prove and catastrophic if wrong.
- **Bit-exact numpy golden models.** The Xmlacc ops are defined by numpy reference models in
  `model/`. RTL and the Spike plugin must match them **bit-for-bit** on shared fixtures — no
  tolerance, because quantized ML correctness depends on exact saturation/rounding.

Sequencing matters as much as technique. If renaming, the ROB, and speculation are introduced at
the same time as the base ISA, a divergence could be an ISA bug or an OoO bug and the two are
hard to separate. Building a **correct in-order core first** proves the ISA, the decode, the
co-sim harness, and the golden models on a machine with no speculative state — so that when OoO
is added, any new divergence is unambiguously an OoO bug.

## Decision

- **Lockstep Spike co-simulation on every retired instruction** is the primary correctness check,
  using a Spike build extended with an **Xmlacc plugin** (milestone M2).
- **riscv-dv constrained-random co-simulation** against Spike is run continuously from M4 onward
  to stress the OoO machinery.
- **SymbiYosys formal verification is scoped** to (1) LSU store-to-load forwarding and (2) the
  ROB. Those properties are kept green; formal is not attempted on the whole core.
- **Xmlacc ops are verified bit-exact** against the numpy golden models in `model/`, with
  committed fixtures, in both the Spike plugin and the RTL.
- **The in-order RV64IMC core is built and proven first (M1)**, before renaming, the ROB, or
  speculation are introduced (M4). The ISA is proven on the in-order machine so later divergences
  isolate cleanly to the OoO logic.

## Consequences

- **Easier:** Bugs surface at the instruction they occur on (lockstep), corner cases get found
  automatically (riscv-dv), the two scariest structures get mathematical proof (formal), and ML
  numerics are pinned exactly (golden). Each layer catches a class the others miss.
- **Easier:** The in-order-first sequencing makes OoO bring-up tractable — a regression after M4
  points at the new machinery, not at a reopened ISA question.
- **Harder / accepted:** Standing up Spike co-sim, the Xmlacc plugin, riscv-dv, and the formal
  harness is significant up-front infrastructure (M2 is largely about this). We accept that cost
  because it pays back across every later milestone.
- **Harder / accepted:** Formal is deliberately *not* whole-core; properties outside the LSU/ROB
  scope rely on co-sim and random testing, not proof.
- **Follow-on work:** The co-sim comparison points (what architectural state is checked, how
  speculative state is excluded until retirement) must be specified precisely in `verif/` when
  the harness is built. The systolic engine (M11) will need its own golden-model and co-sim story.
