# Xmlacc v1 — canonical ISA spec (single source of truth)

This is the frozen v1 contract for the custom ML extension. The ISA table, the
numpy golden model, the assembler, the Spike plugin, and (later) the RTL all
derive from THIS document. Revisable only via an ADR.

## Decisions locked (v1)
- **ML engine shape:** packed-SIMD Tier-1 first (int8 lanes inside GP registers),
  systolic array as Tier-3 showpiece. No full RVV in v1.
- **Issue width:** 2-wide superscalar.
- **FPGA board:** Digilent Arty A7-100T (Artix-7), free Vivado.
- **Toolchain v1:** GCC `.insn` macros + a clang builtin header. Full LLVM
  intrinsic patch deferred to M12 (optional).
- **Synthesis:** Vivado/Artix for v1; RTL kept vendor-neutral for a later
  ECP5/yosys retarget.

## Encoding formats
- XLEN = 64. A GP register holds 8 packed int8 lanes (byte 0 = bits [7:0]).
- R-type:  `funct7[31:25] | rs2[24:20] | rs1[19:15] | funct3[14:12] | rd[11:7] | opcode[6:0]`
- R4-type: `rs3[31:27] | funct2[26:25] | rs2[24:20] | rs1[19:15] | funct3[14:12] | rd[11:7] | opcode[6:0]`
- opcode CUSTOM0 = 0b0001011 (0x0B).

## Tier-1 instructions (all opcode CUSTOM0)
| mnemonic      | fmt | funct3 | funct7/funct2 | semantics |
|---------------|-----|--------|---------------|-----------|
| `mlacc.dot8`    | R   | 0b000  | funct7=0b0000000 | rd = SX64( Σ_{i=0..7} sext8(rs1[i]) · sext8(rs2[i]) ) |
| `mlacc.dot8u`   | R   | 0b001  | funct7=0b0000000 | rd = ZX64( Σ zext8(rs1[i]) · zext8(rs2[i]) ) |
| `mlacc.dot8su`  | R   | 0b010  | funct7=0b0000000 | rd = SX64( Σ sext8(rs1[i]) · zext8(rs2[i]) )  (signed act × unsigned weight) |
| `mlacc.macc`    | R4  | 0b100  | funct2=0b00      | rd = rs3 + Σ sext8(rs1[i]) · sext8(rs2[i])   (fused, renames cleanly) |
| `mlacc.rqs`     | R   | 0b101  | funct7=0b0000000 | sh = rs2[5:0]; t = (rs1 + (sh? 1<<(sh-1):0)) >>s sh; rd = SX64(clamp(t, -128, 127)) |

Notes:
- `dot8` result is a signed int32 that cannot overflow (8 · 127 · 127 < 2^31), sign-extended to 64.
- `macc` is the GEMM inner-loop op: accumulate-in-register, so the OoO engine renames the
  accumulator chain instead of serializing on a CSR.
- `rqs` is the int32→int8 requantize every quantized net needs; rounding is round-half-up.
  `mlstat.SAT` sticky-sets if the clamp fired.

## CSRs (custom user range)
| addr  | name   | meaning |
|-------|--------|---------|
| 0x800 | mlcfg  | reserved for Tier-2/3 tile config (v1: RAZ/WI) |
| 0x801 | mlstat | bit0 = SAT sticky (set by rqs clamp); W1C |

## Tier-2 / Tier-3 (reserved, specced later)
- Tier-2 packed-SIMD lanes: opcode CUSTOM1. Specced at M-stretch.
- Tier-3 systolic `mlacc.mm.*` (load weights / stream / drain): opcode CUSTOM2. Specced at M11.
