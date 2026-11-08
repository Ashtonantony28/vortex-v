// vortex_xmlacc.sv — Xmlacc Tier-1 custom ML execution unit (combinational).
//
// Implements the five frozen Tier-1 ops of the Xmlacc extension (opcode
// CUSTOM0), per docs/XMLACC-SPEC-v1.md and the numpy golden model
// model/xmlacc_model.py (the bit-for-bit oracle):
//
//   dot8   : rd = SX64( sum_{i=0..7} sext8(rs1[i]) * sext8(rs2[i]) )
//   dot8u  : rd = ZX64( sum_{i}     zext8(rs1[i]) * zext8(rs2[i]) )
//   dot8su : rd = SX64( sum_{i}     sext8(rs1[i]) * zext8(rs2[i]) )
//   macc   : rd = rs3 + sum_{i} sext8(rs1[i]) * sext8(rs2[i])   (64-bit wrap)
//   rqs    : sh=rs2[5:0]; t=(rs1 + (sh? 1<<(sh-1):0)) >>>s sh;
//            rd = SX64(clamp(t,-128,127)); SAT=1 if the clamp fired.
//
// A GP register packs 8 int8 lanes, lane i = bits [8i+7:8i] (byte 0 = [7:0]).
//
// Datapath: 8 parallel int8 multipliers feeding an adder tree, computed for all
// three signedness combinations in parallel; the result mux selects per op. The
// dot sum cannot overflow int32 (8*127*127 < 2^31; unsigned 8*255*255 fits in
// 20 bits), so a 32-bit accumulator is exact and is sign/zero-extended to 64.
// Purely combinational (single-cycle in the core); synthesizable, no `*`-free
// requirement here (Tier-1 is packed-SIMD, small multipliers).
`ifndef VORTEX_XMLACC_SV
`define VORTEX_XMLACC_SV
module vortex_xmlacc
  import vortex_pkg::*;
#(
  parameter int unsigned XLEN = 64
) (
  input  vortex_pkg::xmlacc_op_e  op,
  input  logic [XLEN-1:0]         rs1,   // packed int8 lanes (activations)
  input  logic [XLEN-1:0]         rs2,   // packed int8 lanes (weights) / shamt for rqs
  input  logic [XLEN-1:0]         rs3,   // macc accumulator

  output logic [XLEN-1:0]         result,
  output logic                    sat    // rqs clamp fired (valid only for XM_RQS)
);

  localparam int unsigned NLANES = 8;

  // -------------------------------------------------------------------------
  // Per-lane products for the three signedness combinations.
  //  - ss : signed   x signed    (dot8, macc)
  //  - uu : unsigned x unsigned  (dot8u)
  //  - su : signed   x unsigned  (dot8su)
  // Each int8*int8 product fits in 16 bits; sign/zero-extend to 32 before the
  // tree so the accumulate is exact in 32 bits.
  // -------------------------------------------------------------------------
  logic signed [31:0] acc_ss;
  logic signed [31:0] acc_su;
  logic        [31:0] acc_uu;

  always_comb begin
    acc_ss = '0;
    acc_su = '0;
    acc_uu = '0;
    for (int unsigned i = 0; i < NLANES; i++) begin
      logic signed [7:0] a_s;
      logic signed [7:0] b_s;
      logic        [7:0] a_u;
      logic        [7:0] b_u;
      logic signed [15:0] p_ss;
      logic signed [15:0] p_su;
      logic        [15:0] p_uu;

      a_s = rs1[8*i +: 8];
      b_s = rs2[8*i +: 8];
      a_u = rs1[8*i +: 8];
      b_u = rs2[8*i +: 8];

      p_ss = a_s * b_s;                              // signed   x signed
      p_su = a_s * $signed({1'b0, b_u});             // signed   x unsigned(>=0)
      p_uu = a_u * b_u;                              // unsigned x unsigned

      acc_ss = acc_ss + 32'(p_ss);
      acc_su = acc_su + 32'(p_su);
      acc_uu = acc_uu + {16'b0, p_uu};
    end
  end

  // -------------------------------------------------------------------------
  // dot results, extended to XLEN.
  // -------------------------------------------------------------------------
  logic [XLEN-1:0] dot8_res;    // SX64(acc_ss)
  logic [XLEN-1:0] dot8u_res;   // ZX64(acc_uu)
  logic [XLEN-1:0] dot8su_res;  // SX64(acc_su)
  logic [XLEN-1:0] macc_res;    // rs3 + SX64(acc_ss)

  assign dot8_res   = {{(XLEN-32){acc_ss[31]}}, acc_ss};
  assign dot8u_res  = {{(XLEN-32){1'b0}},       acc_uu};
  assign dot8su_res = {{(XLEN-32){acc_su[31]}}, acc_su};
  assign macc_res   = rs3 + {{(XLEN-32){acc_ss[31]}}, acc_ss};

  // -------------------------------------------------------------------------
  // rqs: round-half-up, arithmetic shift right, clamp to int8, sticky SAT.
  // -------------------------------------------------------------------------
  logic [5:0]              sh;
  logic signed [XLEN-1:0]  x_s;        // rs1 as signed 64
  logic signed [XLEN-1:0]  rnd;        // 1<<(sh-1) if sh>0 else 0
  logic signed [XLEN-1:0]  t;          // (x + rnd) >>> sh
  logic signed [7:0]       clamped;    // final int8 result
  logic                    rqs_sat;

  assign sh  = rs2[5:0];
  assign x_s = rs1;
  assign rnd = (sh != 6'd0) ? (64'sd1 <<< (sh - 6'd1)) : 64'sd0;
  assign t   = (x_s + rnd) >>> sh;

  always_comb begin
    if (t < -64'sd128) begin
      clamped = -8'sd128;
      rqs_sat = 1'b1;
    end else if (t > 64'sd127) begin
      clamped = 8'sd127;
      rqs_sat = 1'b1;
    end else begin
      clamped = t[7:0];   // in range: low 8 bits reconstruct the signed value
      rqs_sat = 1'b0;
    end
  end

  logic [XLEN-1:0] rqs_res;
  assign rqs_res = {{(XLEN-8){clamped[7]}}, clamped};  // SX64(int8)

  // -------------------------------------------------------------------------
  // Result / SAT mux.
  // -------------------------------------------------------------------------
  always_comb begin
    unique case (op)
      XM_DOT8   : result = dot8_res;
      XM_DOT8U  : result = dot8u_res;
      XM_DOT8SU : result = dot8su_res;
      XM_MACC   : result = macc_res;
      XM_RQS    : result = rqs_res;
      default   : result = dot8_res;
    endcase
    sat = (op == XM_RQS) ? rqs_sat : 1'b0;
  end

endmodule : vortex_xmlacc
`endif
