# 0001. Shape of the ML extension (Xmlacc): packed-SIMD int8 first, systolic later, no RVV in v1

- **Status:** Accepted
- **Date:** 2026-11-06

## Context

Vortex-V needs to accelerate TinyML inference kernels (small CNNs, depthwise/pointwise convs,
fully-connected layers, keyword spotting). Three broad options exist for adding ML compute to
the core:

1. **Full RISC-V Vector (RVV).** Standard, powerful, and toolchain-supported — but enormous. A
   compliant RVV implementation is a multi-year effort on its own (vector register file, LMUL,
   vsetvl semantics, strided/indexed memory, mask registers). It would dominate the project and
   dwarf the out-of-order core we actually want to study.
2. **A custom packed-SIMD integer extension (Xmlacc).** A small set of fixed-width int8
   operations that fit in the existing 64-bit integer datapath: an 8×int8 dot-product, a fused
   multiply-accumulate into a wider accumulator, and a requantize (multiply-shift-saturate)
   instruction. These are exactly the primitives TinyML quantized kernels need.
3. **A systolic matrix engine.** Highest throughput for GEMM/conv, but it is a loosely-coupled
   accelerator with its own control, tiling, and memory movement — a large block that only pays
   off once the scalar core and the ISA plumbing are solid.

TinyML workloads are overwhelmingly **integer** (int8 weights/activations with int32
accumulators and a requantize step); floating point and the full generality of RVV are not
required. We also want ML instructions that **rename and schedule cleanly** through the
out-of-order machinery rather than stalling it, so that the ML extension is a first-class
citizen of the OoO core rather than a bolt-on.

## Decision

We will build Xmlacc in tiers:

- **Tier-1 (v1, built in M3):** packed-SIMD int8 operations in the integer datapath — int8
  dot-product, a fused `macc` (multiply-accumulate) that writes a wide accumulator, and a
  requantize instruction with defined rounding and saturation. These are single-destination,
  fixed-latency instructions that fit the standard rename → issue → execute → writeback flow.
  The fused `macc` in particular is designed to rename as one destination so it does not create
  false dependencies or partial-register hazards in the OoO core.
- **Tier-3 (M11, stretch):** a systolic matrix engine exposed as a separate out-of-order
  functional unit, added only after the scalar core, the ML ops, and FPGA bring-up are done.
- **We will NOT implement full RVV in v1.** Tier-2 (a wider or more general vector form) is
  explicitly left as a future possibility, not a v1 commitment.

## Consequences

- **Easier:** The ML extension stays small enough to design, encode, model, and verify in the
  same cadence as the rest of the ISA. A numpy golden model per op is tractable and bit-exact.
  The ops drop into the existing 64-bit datapath and the OoO scheduler without special cases.
- **Easier:** TinyML kernels get the exact primitives they need (quantized MAC + requantize)
  without the compiler and runtime complexity of RVV.
- **Harder / accepted risk:** We give up standard vector toolchain support. Kernels must be
  written against Xmlacc via `.insn` macros / intrinsics (see ADR 0004), not auto-vectorized.
- **Harder / accepted risk:** The fused `macc`'s wide accumulator semantics must be nailed down
  precisely (accumulator width, overflow/saturation behavior) in `isa/` and the golden model,
  because the OoO renamer depends on clean single-destination semantics.
- **Follow-on work:** Tier-3's systolic engine will need its own ADR covering its coupling model
  (how it interacts with the LSQ and the ROB) when we reach M11.
- A future ADR may revisit adding a constrained RVV subset if TinyML needs outgrow Xmlacc; this
  decision does not preclude that, it only keeps it out of v1.
