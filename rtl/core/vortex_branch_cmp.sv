// vortex_branch_cmp.sv — conditional-branch comparator shared by both cores.
//
// Evaluates a BRANCH funct3 condition on two register operands. Shared by
// vortex_core (in-order) and vortex_ooo_core so a fix lands once (ADR 0006).
`ifndef VORTEX_BRANCH_CMP_SV
`define VORTEX_BRANCH_CMP_SV
module vortex_branch_cmp
  import vortex_pkg::*;
#(
  parameter int unsigned XLEN = 64
) (
  input  logic [2:0]      f3,     // BRANCH funct3
  input  logic [XLEN-1:0] a,      // rs1
  input  logic [XLEN-1:0] b,      // rs2
  output logic            taken
);

  logic cmp_eq, cmp_lt, cmp_ltu;
  assign cmp_eq  = (a == b);
  assign cmp_lt  = ($signed(a) < $signed(b));
  assign cmp_ltu = (a < b);

  always_comb begin
    unique case (f3)
      F3_BEQ : taken = cmp_eq;
      F3_BNE : taken = ~cmp_eq;
      F3_BLT : taken = cmp_lt;
      F3_BGE : taken = ~cmp_lt;
      F3_BLTU: taken = cmp_ltu;
      F3_BGEU: taken = ~cmp_ltu;
      default: taken = 1'b0;
    endcase
  end

endmodule : vortex_branch_cmp
`endif
