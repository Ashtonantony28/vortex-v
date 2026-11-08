// vortex_alu.sv — combinational RV64I ALU (64-bit ops + RV64 *W ops).
//
// The *W operations compute a 32-bit result and sign-extend it to XLEN, per the
// RV64I spec. Shift amounts: 64-bit shifts use the low 6 bits of b; W shifts use
// the low 5 bits.
`ifndef VORTEX_ALU_SV
`define VORTEX_ALU_SV
module vortex_alu
  import vortex_pkg::*;
#(
  parameter int unsigned XLEN = 64
) (
  input  vortex_pkg::alu_op_e      op,
  input  logic [XLEN-1:0]          a,
  input  logic [XLEN-1:0]          b,
  output logic [XLEN-1:0]          y
);

  logic signed [XLEN-1:0] a_s;
  logic signed [XLEN-1:0] b_s;
  assign a_s = a;
  assign b_s = b;

  // 32-bit views for the *W ops
  logic        [31:0]      a32;
  logic        [31:0]      b32;
  logic signed [31:0]      a32_s;
  assign a32   = a[31:0];
  assign b32   = b[31:0];
  assign a32_s = a[31:0];

  logic [5:0] sh64; // 64-bit shift amount
  logic [4:0] sh32; // 32-bit shift amount
  assign sh64 = b[5:0];
  assign sh32 = b[4:0];

  logic [31:0] w_res; // 32-bit intermediate for *W ops (sign-extended below)

  always_comb begin
    y     = '0;
    w_res = '0;
    unique case (op)
      ALU_ADD : y = a + b;
      ALU_SUB : y = a - b;
      ALU_SLL : y = a << sh64;
      ALU_SLT : y = (a_s < b_s) ? {{(XLEN-1){1'b0}}, 1'b1} : '0;
      ALU_SLTU: y = (a   < b  ) ? {{(XLEN-1){1'b0}}, 1'b1} : '0;
      ALU_XOR : y = a ^ b;
      ALU_SRL : y = a >> sh64;
      ALU_SRA : y = a_s >>> sh64;
      ALU_OR  : y = a | b;
      ALU_AND : y = a & b;

      ALU_ADDW: begin w_res = a32 + b32;            y = {{32{w_res[31]}}, w_res}; end
      ALU_SUBW: begin w_res = a32 - b32;            y = {{32{w_res[31]}}, w_res}; end
      ALU_SLLW: begin w_res = a32 << sh32;          y = {{32{w_res[31]}}, w_res}; end
      ALU_SRLW: begin w_res = a32 >> sh32;          y = {{32{w_res[31]}}, w_res}; end
      ALU_SRAW: begin w_res = a32_s >>> sh32;       y = {{32{w_res[31]}}, w_res}; end

      ALU_PASS_B: y = b;
      default : y = '0;
    endcase
  end

endmodule : vortex_alu
`endif
