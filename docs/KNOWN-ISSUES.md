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

## M4
- **I6 (performance, by design for now):** the OoO core reuses the shared `vortex_muldiv` unit
  for both its multiplier and its divider (two instances; ADR 0006 "shared, never copied"). That
  unit takes a multiply in one cycle and returns it the next, so a multiply has 2-cycle latency
  and 1-per-2-cycles throughput, not the 1-cycle multiplier ADR 0006 item 5 describes. Results are
  correct; only overlap is lower. Fix with a pipelined multiplier when M5 widens issue.
- **I7 (performance, by design for M4):** commit is 1 per cycle and the in-order core already
  runs ALU ops at 1 per cycle, so OoO only wins where multi-cycle units overlap (`test_ooo.py`
  uses multiplies behind a divide). Wider commit (M5) and speculation (M6) are where IPC grows.
- **I8 (verification gap):** `dbg_ooo_completions` counts only single-cycle and multiplier
  completions; divider and load completions ahead of older work are not counted. It is a proof
  aid, not a performance counter.

## M5
- **I9 (performance, by design for M5):** on the constrained-random mix the two-wide core is only
  about 7% faster than one-wide (500-seed soak: 4.28 ms vs 4.58 ms simulated). Branches stop
  dispatch until they resolve and memory goes one access per cycle, so groups of two rarely form.
  Independent straight-line code reaches 2 per cycle. M6 (speculation) and M7 (LSQ) are the fix.
- **I10:** issue port 1 is ALU/branch only (ADR 0007 item 5), so two multiplies, two loads or two
  Xmlacc ops never issue in the same cycle.

## M6
- **I11 (performance, by design for M6):** one predicted branch in flight (ADR 0008). A loop with
  a second branch in its body waits at that branch for the first to resolve. JALR (calls and
  returns) still stall. Random code gains almost nothing (500-seed soak: 4.31 ms vs 4.28 ms
  simulated without speculation), because its branches are random by construction.
- **I12:** speculative loads read memory before older branches resolve. That is safe here,
  because the data memory has no side effects on reads. A future MMIO region with read side
  effects must hold its loads until they are non-speculative.

## M7
- **I13 (performance, by design for M7):** one load per cycle and one data-memory port, and a
  load that only partly overlaps an older store waits for that store to commit (ADR 0009).
  Loads stay in the memory queue until they commit, so `MEMQ_DEPTH` fills sooner than at M6.
- **I14 (verification gap):** the forwarding proof runs at XLEN=32 with 3 older stores (yosys
  SAT, since SymbiYosys is not installed). A one-off run at XLEN=64 with 4 stores passed in 22
  minutes, too slow for CI. The core uses up to 7 older stores; beyond the proven sizes it is
  covered by simulation only. ROB formal is not done.
- **I15:** the L1 data cache named in the M7 roadmap row is deferred until a memory model with
  latency exists (ADR 0009 item 6).

## M8
- **I16 (performance, by design for M8):** Xmlacc issues only on port 0, which it shares with
  loads, multiplies and divides. An int8 matrix-vector kernel reaches about 5.3 MACs/cycle, close
  to the in-order core's 5.2, because port 0 is the bottleneck. A second Xmlacc unit on port 1 is
  the fix (ADR 0010 follow-on).
- **I17:** the pipelined Xmlacc stages (`XM_LATENCY` > 1) are output registers that rely on
  synthesis retiming. Whether that meets timing on the Arty A7 is unknown until M9.
