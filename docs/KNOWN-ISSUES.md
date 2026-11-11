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

## M3
- **I5 (BUG, FIXED in `fix-rqs-round-overflow`; found by the UVM verification thread):** the
  `rqs` rounding add in `rtl/core/vortex_xmlacc.sv` was 64 bits wide. With `rs1` near INT64_MAX,
  `rs1 + (1<<(sh-1))` wrapped negative before the shift, e.g. `rqs(INT64_MAX, sh=63)` returned -1
  instead of 1, and `sh=1` clamped LOW (-128) instead of HIGH (+127). Now computed in XLEN+1 bits.
  Regression coverage: directed cocotb test `xmlacc_rqs_round_overflow`, co-sim programs
  `rqs_edges` (INT64_MAX/MIN x 5 shifts) and `rqs_random` (64 seeded full-range values x every
  shift 0..63). Each fails on the old RTL and passes on the fix.
