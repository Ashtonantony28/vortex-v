# CLAUDE.md — conventions for Vortex-V

This file is the contract for anyone (human or agent) touching this repository. Read it before
making changes. It encodes the discipline carried over from `tiny-gpu-extended`.

## Prime directives

1. **Every change ships with a Verify command.** No PR is complete unless its description names
   the exact command that proves it works (a `pytest` invocation, a cocotb test, an `sby` task,
   a lint run). "I eyeballed it" is not verification. If a change cannot yet be verified, say so
   explicitly and link the milestone that will make it verifiable.
2. **ML ops are bit-exact against a numpy golden model.** Every Xmlacc instruction has a golden
   model in `model/` that defines the *reference* result (including saturation, rounding mode,
   and requantize behavior). RTL and the Spike plugin must match it bit-for-bit on the shared
   fixtures. Approximate equality is never acceptable for integer ML ops.
3. **In-order correctness precedes out-of-order cleverness.** Do not add renaming, a ROB, or
   speculation to a feature until the in-order path for that feature passes co-sim. Correctness
   is proven on the simple machine first (see ADR 0005).

## Architecture Decision Records

- Any non-obvious or hard-to-reverse decision gets an ADR in `docs/adr/`, numbered sequentially,
  using `docs/adr/0000-template.md`.
- An ADR is immutable once `Accepted`. To change a decision, write a **new** ADR that supersedes
  the old one and set the old one's status to `Superseded by NNNN`.
- Reference the relevant ADR number in code comments and PR descriptions when you implement or
  depend on a decision.

## SystemVerilog coding standards

- **Parameterize, do not hardcode.** Widths, depths, counts (issue width, ROB entries, LSQ depth,
  int8 lane count) are module parameters with sensible defaults, never literals buried in logic.
- **No magic numbers.** Every constant is a named `localparam` or comes from a shared package
  (`rtl/pkg/`). Opcode and funct fields come from the Xmlacc package generated off `isa/`, never
  retyped by hand.
- **Features default off.** New or experimental features (speculation depth beyond 1, the systolic
  engine, prefetchers) are gated by a parameter that defaults to the conservative/disabled value.
  A fresh instantiation must build the known-good configuration.
- **Explicit over implicit.** Fully-qualified port connections (`.name(name)`), explicit
  `logic`/`logic signed`, no implicit nets, no latches. Reset strategy is synchronous unless a
  module documents otherwise.
- **One module per file**, filename matches module name. Packages in `rtl/pkg/`.
- **Lint clean.** Code must pass Verilator lint (`-Wall`) with no new warnings before merge. The
  lint job is a placeholder in CI until RTL lands; once it lands, it becomes a gate.
- **Vendor-neutral RTL.** No Xilinx primitives in the core logic. Memories and clocking go through
  thin wrappers in `rtl/fpga/` so the core can later target yosys/ECP5 (see ADR 0003).

## Verification expectations

- cocotb testbenches live in `verif/` and are driven from the numpy golden models in `model/`.
- Lockstep Spike co-sim is the default check for any instruction-level change.
- riscv-dv constrained-random streams catch what directed tests miss.
- Formal (SymbiYosys) is scoped to the LSU store-to-load forwarding and the ROB; keep those
  properties green.
- Fixtures are committed and bit-exact. Regenerating a fixture is a reviewed change, not a
  silent overwrite.

## Branch / PR discipline

- Never commit directly to the default branch. Branch per feature/milestone:
  `m4-ooo-rename`, `xmlacc-tier1-macc`, `fix-lsq-forwarding`.
- One logical change per PR. Keep diffs reviewable.
- PR description must contain: what changed, the **Verify command**, the ADR(s) touched or relied
  on, and the roadmap milestone it advances.
- Red CI never merges. The Python model/ISA/asm test job is the hard gate today.
- Commit messages explain *why*, not just *what*.

## Directory ownership

- `rtl/`, `verif/`, `docs/`, root config: core RTL + verification work.
- `isa/`, `model/`, `asm/`: owned by their respective work streams — coordinate before editing.
