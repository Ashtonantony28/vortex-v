# 0004. Compiler integration depth: `.insn` macros + clang builtin header in v1, full LLVM patch deferred

- **Status:** Accepted
- **Date:** 2026-11-06

## Context

Kernels need a way to emit Xmlacc instructions from C/C++. There is a spectrum of compiler
integration, from almost none to full first-class support:

1. **Hand-written encodings via GCC/Clang `.insn` inline-assembly macros.** The assembler's
   `.insn` directive lets us emit any instruction by specifying its format and fields, with no
   compiler patch at all. Wrap each Xmlacc op in a macro (or a `static inline` helper) and you
   can write quantized kernels in C today.
2. **A clang builtin / intrinsic header.** A header exposing `__builtin`-style inline functions
   (built on `.insn` under the hood) so kernel code reads as intrinsics rather than raw asm,
   with proper typing of operands.
3. **A full LLVM patch** adding Xmlacc as real instruction descriptors with intrinsics the
   backend understands, enabling instruction selection, scheduling models, and register
   allocation to treat Xmlacc ops as native. This is the "proper" long-term integration but is a
   substantial, maintenance-heavy effort that must track upstream LLVM.

The project's near-term need is simply to **compile and run TinyML kernels** and verify the core
against golden models. That need is fully met by options 1 and 2. A full LLVM patch buys
auto-scheduling and allocation niceties that matter for production compilers but add little to a
research core whose kernels are hand-written and hand-tuned anyway — and it would be a large,
ongoing time sink competing with the microarchitecture work.

## Decision

- **v1 compiler integration is options 1 + 2:** GCC/Clang `.insn` macros for every Xmlacc
  instruction, wrapped in a **clang builtin-style header** (living with the assembler helpers in
  `asm/`) so kernels are written against clean inline functions rather than raw encodings. These
  are generated from / checked against the opcode table in `isa/` so encodings cannot drift.
- **A full LLVM intrinsic + instruction-descriptor patch is deferred to milestone M12**, and M12
  is **optional** (stretch). It will only be done if the project reaches the point where native
  compiler codegen is worth the maintenance burden.

## Consequences

- **Easier:** We can write and run real quantized kernels immediately, with no compiler build or
  patch to maintain. The `.insn`/builtin layer is small and lives next to the ISA definition, so
  encodings stay consistent with `isa/`.
- **Easier:** Keeps the critical path to "TinyML running on hardware" (M10) short — it does not
  block on compiler engineering.
- **Harder / accepted:** No automatic instruction selection, scheduling, or register allocation
  for Xmlacc. Kernels are hand-written and hand-scheduled. For a research core with a small set
  of hot kernels this is acceptable and arguably clarifying.
- **Harder / accepted:** The builtin header must be kept in sync with the opcode table; this is
  enforced by generating/checking it against `isa/` rather than maintaining it by hand.
- **Follow-on work:** If M12 is pursued, it needs its own ADR covering the LLVM version targeted,
  the intrinsic naming, the scheduling model, and the upstream-tracking strategy.
