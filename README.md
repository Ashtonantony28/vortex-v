# Vortex-V

A 2-wide out-of-order superscalar **RV64GC** core with a custom integer-ML extension
(**Xmlacc**), targeting a **Digilent Arty A7-100T** FPGA and running TinyML kernels.

> **Status: M3 — in-order RV64IM core + Xmlacc Tier-1, in simulation.** There is a working
> in-order RV64I+M core in SystemVerilog (`rtl/core/`) that runs under Icarus via cocotb and is
> checked instruction-by-instruction against a Python ISS golden model (`model/iss/`), including
> the five Tier-1 Xmlacc ML instructions. It also passes the self-checking riscv-tests rv64ui +
> rv64um suites (65 tests) on both the ISS and the RTL. Not built yet: the out-of-order engine
> (M4+), the C extension, CSRs/traps, `riscv-arch-test`, Spike, FPGA bitstream, and benchmarks.
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
│   └── adr/                  # architecture decision records 0001-0006
├── rtl/core/                 # in-order RV64IM + Xmlacc core; files.f is the filelist
├── isa/                      # Xmlacc opcode table + encoding tests
├── model/                    # numpy golden model for ML ops; model/iss = RV64IM+Xmlacc ISS
├── asm/                      # Xmlacc assembler / .insn helpers
├── scripts/setup-toolchain.sh
├── third_party/riscv-tests/  # vendored rv64ui/rv64um sources (BSD-3, pinned commit)
└── verif/
    ├── core/                 # directed cocotb tests + lockstep co-sim; rvasm, harness, cores
    ├── conformance/          # riscv-tests on the ISS and on every core
    └── crv/                  # seeded constrained-random programs, co-simmed vs the ISS
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
```

## Roadmap

See [docs/ROADMAP.md](docs/ROADMAP.md) for the full M0..M12 plan. Short version: scaffolding (M0,
in progress) → in-order RV64IMC (M1) → Spike Xmlacc plugin + golden (M2) → Tier-1 ML ops (M3) →
OoO scalar (M4) → 2-wide superscalar (M5) → branch prediction (M6) → non-blocking L1-D + LSQ (M7)
→ ML engine as OoO FU (M8) → FPGA bring-up (M9) → TinyML on hardware (M10) → systolic matrix
engine (M11) → LLVM intrinsics (M12).

## License

TBD before first external release.
