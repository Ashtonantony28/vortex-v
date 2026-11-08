# Known issues / cleanup backlog

Tracked honestly as we go (tiny-gpu audit discipline). None of these are
correctness failures — the full suite is green — but they're real and owned.

## M1
- **I1 (cosmetic):** the core logs `illegal/unsupported instruction 00000000 at
  pc=0x80000000` for the first cycle(s) at reset, before the combinational imem
  read resolves / while the TB is still presenting 0. All programs still retire
  correctly and signal tohost PASS. Fix at M1.1: gate the illegal-instr warning on
  a "fetch valid" condition, or hold the core in reset until imem is driven.
- **I2:** `vortex_pkg.sv` lives in `rtl/core/` but CLAUDE.md wants packages in
  `rtl/pkg/`. Relocate when the M0 folder-ownership constraint no longer applies.
- **I3 (scope, expected):** RV64I base only — no M (mul/div), C (compressed), CSRs,
  or traps/interrupts beyond the tohost halt. These are M1.1 / later milestones.
- **I4:** iverilog prints benign "sorry" notes (ignores `unique` case qualifier;
  always_comb sensitivity note). Verilator `--lint-only -Wall` is the real gate and
  is clean. Revisit if we drop iverilog.
