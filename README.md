# Vortex-V

A 2-wide out-of-order superscalar **RV64GC** core with a custom integer-ML extension
(**Xmlacc**), targeting a **Digilent Arty A7-100T** FPGA and running TinyML kernels.

> **Status: M9 (simulation side) — two-wide speculative out-of-order RV64IM + Xmlacc core in an SoC for the Arty A7, in simulation.** Two
> cores live in `rtl/core/`: the in-order reference `vortex_core` and the out-of-order
> `vortex_ooo_core` (register renaming, 32-entry ROB, issue queue, separate
> multiplier/divider/Xmlacc units; ADR 0006). Its `WIDTH` parameter selects 1-wide or 2-wide
> dispatch, issue and commit (ADR 0007), and `SPEC_BRANCHES=1` adds a bimodal branch predictor
> with speculative execution and checkpoint recovery (ADR 0008). `LSQ=1` lets loads run out of
> order past older stores, with store-to-load forwarding (ADR 0009; the forwarding unit is proven
> with yosys). `XM_LATENCY` makes the Xmlacc unit a pipelined functional unit with its own
> writeback port (ADR 0010). `vortex_soc` wraps a core with RAM, a UART and LEDs for the Arty A7,
> and runs gcc-built C programs from `sw/` (ADR 0011); `SYNC_MEM=1` lets the core use block RAM
> (ADR 0012). All seven core configurations run
> under Icarus via cocotb and are checked
> instruction-by-instruction against a Python ISS golden model (`model/iss/`), on directed tests,
> the riscv-tests rv64ui + rv64um suites (65 tests), and seeded constrained-random programs. Not
> built yet: the L1 data cache, the C extension, CSRs/traps, `riscv-arch-test`, Spike, the FPGA
> bitstream (Vivado not yet run), and benchmarks.
> Every claim here is backed by a `Verify` command in [docs/ROADMAP.md](docs/ROADMAP.md).

## What this is

Vortex-V is a from-scratch application-class RISC-V core designed to be a serious study of
out-of-order execution *and* a practical TinyML accelerator. The custom **Xmlacc** extension
adds integer (int8) dot-product / multiply-accumulate / requantize instructions that rename
and schedule cleanly through the out-of-order machinery, with a systolic matrix engine added
later as a separate functional unit.

It is the spiritual successor to the author's **tiny-gpu-extended** project and keeps the same
engineering discipline: SystemVerilog RTL, cocotb testbenches driven by numpy golden models,
bit-exact fixtures, architecture decision records for every non-obvious choice, and
severity-ranked audits.

### Design pillars

- **In-order first, OoO second.** A correct in-order RV64IMC core (M1) proves the ISA plumbing
  and the co-sim harness *before* register renaming, a ROB, or speculation are introduced (M4).
- **Integer ML, not full vector.** Xmlacc is a small, packed-SIMD int8 extension (Tier-1), with
  an optional systolic array (Tier-3). No full RVV in v1. See
  [ADR 0001](docs/adr/0001-ml-engine-shape.md).
- **FPGA timing closure drives width.** 2-wide, not 4-wide, because wakeup/select + bypass is a
  timing trap on Artix-7. See [ADR 0002](docs/adr/0002-issue-width.md).
- **Everything is verified against a golden reference.** Spike lockstep co-sim on every retired
  instruction, riscv-dv constrained-random co-sim, scoped SymbiYosys formal on the LSU and ROB,
  and bit-exact numpy golden models for the ML ops. See
  [ADR 0005](docs/adr/0005-verification-strategy.md).

## Directory layout

```
vortex-v/
├── README.md                 # this file
├── CLAUDE.md                 # conventions for agents/contributors
├── .github/workflows/ci.yml  # two gates: pytest + cocotb RTL sim, and Verilator lint
├── docs/
│   ├── ROADMAP.md            # M0..M12 + stretch, with Verify commands
│   ├── KNOWN-ISSUES.md       # open and fixed issues, with regression tests
│   ├── XMLACC-SPEC-v1.md     # the frozen Xmlacc ISA spec
│   ├── M1-INTERFACE-v1.md    # core port / retire contract shared by every core
│   └── adr/                  # architecture decision records 0001-0011
├── rtl/core/                 # in-order + out-of-order RV64IM + Xmlacc cores; files.f is the filelist
├── rtl/soc/                  # vortex_soc: core + RAM + UART + LEDs (ADR 0011)
├── rtl/fpga/                 # vendor wrappers (RAM, clock) and the Arty A7 top
├── fpga/                     # Arty A7 pin constraints and Vivado build script
├── sw/                       # bare-metal C runtime and programs for the SoC
├── isa/                      # Xmlacc opcode table + encoding tests
├── model/                    # numpy golden model for ML ops; model/iss = RV64IM+Xmlacc ISS
├── asm/                      # Xmlacc assembler / .insn helpers
├── scripts/setup-toolchain.sh
├── third_party/riscv-tests/  # vendored rv64ui/rv64um sources (BSD-3, pinned commit)
└── verif/
    ├── core/                 # directed cocotb tests + lockstep co-sim; rvasm, harness, cores
    ├── conformance/          # riscv-tests on the ISS and on every core
    ├── crv/                  # seeded constrained-random programs, co-simmed vs the ISS
    ├── formal/               # yosys SAT proof of the LSQ forwarding unit
    ├── workloads/            # NeuroFlight int8 policy, bit-exact on every core
    └── soc/                  # gcc-built C program on the SoC, checked over the UART
```

`rtl/core/files.f` is the RTL filelist shared by lint and simulation.

## Toolchain

Installed today (`scripts/setup-toolchain.sh`): Icarus, Verilator, cocotb, numpy, pytest, and the RISC-V GCC cross-compiler. The rest arrive at their milestones.

| Tool | Role | Notes |
|---|---|---|
| **Verilator** | Primary RTL simulator for cocotb co-sim | Cycle-accurate, fast; the default once RTL lands |
| **Icarus Verilog** | Fallback simulator | Slower; used where a feature trips Verilator |
| **cocotb** | Python testbench framework | Drives RTL from numpy golden models |
| **Spike** (`riscv-isa-sim`) | Golden ISA simulator | Extended with an **Xmlacc plugin** for lockstep co-sim |
| **riscv-dv** | Constrained-random instruction stream generator | Co-sim against Spike |
| **SymbiYosys** (sby) | Formal verification | Scoped to LSU forwarding and the ROB |
| **Vivado** (free WebPACK) | Synthesis / place-and-route / bitstream | Arty A7-100T; RTL kept vendor-neutral for later yosys/ECP5 |
| **riscv64 GCC / Clang** | Cross-compiling kernels | `.insn` macros + a clang builtin header in v1 (see ADR 0004) |
| **Python 3.11+, numpy, pytest** | Golden models, ISA/asm/encoding tests | The real CI gate today |

## Quickstart

```bash
# from the repo root (Ubuntu 24.04; installs iverilog, verilator, riscv64 gcc, cocotb, numpy, pytest)
sudo scripts/setup-toolchain.sh

# the two CI gates
pytest isa/ model/ asm/ verif/ -q          # models, RTL co-sim, riscv-tests, random co-sim
verilator --lint-only -Wall --top-module vortex_core -f rtl/core/files.f
verilator --lint-only -Wall --top-module vortex_ooo_core -f rtl/core/files.f
verilator --lint-only -Wall --top-module vortex_ooo_core -GWIDTH=2 -f rtl/core/files.f
verilator --lint-only -Wall --top-module vortex_ooo_core -GWIDTH=2 -GSPEC_BRANCHES=1 -f rtl/core/files.f
```

## Roadmap

See [docs/ROADMAP.md](docs/ROADMAP.md) for the full M0..M12 plan. Short version: scaffolding (M0,
in progress) → in-order RV64IMC (M1) → Spike Xmlacc plugin + golden (M2) → Tier-1 ML ops (M3) →
OoO scalar (M4) → 2-wide superscalar (M5) → branch prediction (M6) → non-blocking L1-D + LSQ (M7)
→ ML engine as OoO FU (M8) → FPGA bring-up (M9) → TinyML on hardware (M10) → systolic matrix
engine (M11) → LLVM intrinsics (M12).

## License

TBD before first external release.
