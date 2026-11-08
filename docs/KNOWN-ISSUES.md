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
- **I5 (BUG, open — found by the UVM verification thread):** `rqs` rounding add can
  overflow in RTL. `rtl/core/vortex_xmlacc.sv` line ~106 computes `t = (x_s + rnd) >>> sh`
  in 64 bits, with `rnd` up to `1<<62`. When `rs1` is near the positive 64-bit limit,
  `x_s + rnd` wraps, so the shifted value and the clamp/SAT come out wrong. The Python ISS and
  numpy golden model use unbounded ints, so they don't wrap, and RTL disagrees with them in
  this corner. The co-sim vectors never reached it. **Fix:** sign-extend `x_s` and `rnd` to
  65 bits, add and shift in 65 bits, then clamp. Add a directed test plus a co-sim vector with
  large `rs1` and `sh` near 63.
