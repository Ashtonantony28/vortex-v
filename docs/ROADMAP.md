# Vortex-V Roadmap

Milestones M0 through M12, plus a stretch tier. Each milestone names a concrete deliverable and
the **Verify command** that proves it is done — nothing is "done" without a passing Verify
(see [CLAUDE.md](../CLAUDE.md)).

Verify commands for milestones beyond the current one are the *intended* checks; the tools they
invoke (Verilator, Spike, riscv-dv, sby, Vivado) are not wired up yet. They become real as each
milestone makes them real. Only the M0 gate (`pytest isa/ model/ asm/ verif/ -q`) runs today.

Legend: `[~]` in progress · `[ ]` not started · `[x]` done

| # | Status | Milestone | Deliverable | Verify command |
|---|--------|-----------|-------------|----------------|
| M0 | `[x]` | Scaffolding | Repo skeleton, ADRs 0001–0005, roadmap, ISA table, numpy golden model, assembler, CI | `pytest isa/ model/ asm/ verif/ -q` |
| M1 | `[~]` | In-order RV64IMC | **Done:** in-order RV64I + RV64M core in RTL, Python ISS golden model, cocotb lockstep co-sim vs ISS, and the self-checking **riscv-tests** rv64ui + rv64um suites (65 tests) passing on both ISS and RTL. **Left:** C extension; Zicsr + traps (unblocks `fence_i`, `ma_data`); the signature-based `riscv-arch-test` suite | `pytest verif/core/ verif/conformance/ -q` |
| M2 | `[ ]` | Spike Xmlacc plugin + golden | Spike extended with Xmlacc plugin; numpy golden models wired as the oracle; shared bit-exact fixtures | `pytest model/ -q && spike --isa=rv64gc_xmlacc --extlib=build/xmlacc.so ...` (plugin matches golden on fixtures) |
| M3 | `[x]` | Tier-1 ML ops (in-order) | Xmlacc Tier-1 int8 dot-product / fused macc / requantize in ISS + in-order core; co-sim vs ISS, ISS cross-checked vs numpy golden. RTL CSR decode for mlstat deferred (SAT on a debug port) | `pytest model/ verif/core/ -q` |
| M4 | `[x]` | OoO scalar | Rename + ROB + single-issue out-of-order execution (ADR 0006). `vortex_ooo_core` passes every suite the in-order core does (directed, ISS co-sim, riscv-tests, 500 random seeds) and `verif/core/test_ooo.py` proves out-of-order completion and a 0.72x cycle count vs in-order behind a divide | `pytest verif/ -q` with `vortex_ooo_core` in `verif/core/cores.py`; `VORTEX_RANDOM_SEEDS=0-499` for a soak |
| M5 | `[x]` | Superscalar 2-wide | Widen rename/issue/retire to 2-wide (ADR 0002, ADR 0007). `vortex_ooo_core` with `WIDTH=2` passes every suite and 500 random seeds; `test_ooo.py` shows 2 commits per cycle (38 vs 69 cycles vs in-order on independent code). riscv-dv still stands in as `verif/crv` | `pytest verif/ -q -k w2`; soak: `VORTEX_RANDOM_SEEDS=0-499 pytest verif/crv/ -q -k w2` |
| M6 | `[ ]` | Branch prediction + speculation | Predictor + speculative fetch/execute + misprediction recovery via ROB | `pytest verif/ -k speculation -q && make cosim RISCV_DV=1` (recovery correct; co-sim green) |
| M7 | `[ ]` | Non-blocking L1-D + LSQ forwarding | Non-blocking L1 data cache, load/store queue with store-to-load forwarding; formal on forwarding | `sby -f verif/formal/lsu_forwarding.sby && sby -f verif/formal/rob.sby` (formal passes) |
| M8 | `[ ]` | ML engine as OoO FU | Xmlacc Tier-1 ops wired as a first-class out-of-order functional unit (clean rename, no false deps) | `make cosim RISCV_DV=1 && pytest verif/ -k xmlacc_ooo -q` |
| M9 | `[ ]` | FPGA bring-up | Synthesizable build on Arty A7-100T; timing closed; blinky + serial + boots a program | `make bitstream BOARD=arty_a7_100t` (Vivado P&R meets timing; on-board smoke test passes) |
| M10 | `[ ]` | TinyML on hardware | A quantized TinyML kernel (e.g. keyword spotting) compiled with `.insn`/builtins runs on the FPGA and matches golden | `make hw_test KERNEL=kws` (on-hardware output bit-exact vs numpy golden) |
| M11 | `[ ]` | Systolic matrix engine (Tier-3) | Systolic int8 matrix engine as a separate OoO functional unit (per ADR 0001) | `pytest verif/ -k systolic -q && make cosim` (bit-exact vs golden; co-sim green) |
| M12 | `[ ]` | LLVM intrinsics (optional) | Full LLVM intrinsic + instruction-descriptor patch for Xmlacc (per ADR 0004; stretch) | `make llvm_test` (kernels compiled via native intrinsics match `.insn` builds bit-for-bit) |
| Stretch | `[ ]` | Beyond v1 | Candidates: ECP5/yosys retarget (ADR 0003), deeper branch predictor, L2 cache, Tier-2 vector form, perf-counter + profiling flow | Defined per item when scoped (each gets its own ADR) |

## Current focus

**M0 — scaffolding (in progress).** Repository skeleton, the five accepted ADRs, this roadmap,
`.gitignore`, `CLAUDE.md`, and CI with the Python model/ISA/asm job as the gate and a
placeholder Verilator-lint job. Next up is M1: a correct in-order RV64IMC core verified in
lockstep against Spike.
