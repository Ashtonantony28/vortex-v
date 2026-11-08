// vortex_regfile.sv — 32-entry XLEN-wide integer register file.
//
// x0 is hardwired to zero: writes to x0 are ignored and reads return 0.
// THREE combinational read ports, one synchronous write port. The third read
// port (rs3) serves the Xmlacc R4-type `mlacc.macc` instruction, which reads
// three source registers; a flop-based register file makes an extra read port a
// cheap set of muxes, so macc stays single-cycle (no extra stall) and the core's
// one-pulse-per-retire contract is unaffected. Synchronous active-low reset
// clears the architectural registers (x0 stays 0 regardless).
`ifndef VORTEX_REGFILE_SV
`define VORTEX_REGFILE_SV
module vortex_regfile #(
  parameter int unsigned XLEN  = 64,
  parameter int unsigned NREGS = 32,
  parameter int unsigned AW    = $clog2(NREGS)
) (
  input  logic              clk,
  input  logic              rst_n,

  input  logic [AW-1:0]     rs1_addr,
  output logic [XLEN-1:0]   rs1_data,
  input  logic [AW-1:0]     rs2_addr,
  output logic [XLEN-1:0]   rs2_data,
  input  logic [AW-1:0]     rs3_addr,
  output logic [XLEN-1:0]   rs3_data,

  input  logic              we,
  input  logic [AW-1:0]     rd_addr,
  input  logic [XLEN-1:0]   rd_data
);

  logic [XLEN-1:0] regs [NREGS-1:0];

  // Combinational reads with x0 forced to zero.
  assign rs1_data = (rs1_addr == '0) ? '0 : regs[rs1_addr];
  assign rs2_data = (rs2_addr == '0) ? '0 : regs[rs2_addr];
  assign rs3_data = (rs3_addr == '0) ? '0 : regs[rs3_addr];

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      for (int unsigned i = 0; i < NREGS; i++) begin
        regs[i] <= '0;
      end
    end else if (we && (rd_addr != '0)) begin
      regs[rd_addr] <= rd_data;
    end
  end

endmodule : vortex_regfile
`endif
