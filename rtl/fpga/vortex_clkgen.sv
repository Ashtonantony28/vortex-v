// vortex_clkgen.sv — core clock from the board clock (ADR 0003, ADR 0011).
//
// The generic model passes the input clock through (simulation, lint, yosys).
// With VORTEX_XILINX defined (the Vivado build, fpga/build.tcl) it instantiates
// an MMCME2_BASE: VCO = IN_HZ * MULT, out = VCO / DIV. Defaults: 100 MHz in,
// 1000 MHz VCO, 50 MHz out. `locked` holds the SoC in reset until the clock is stable.
`ifndef VORTEX_CLKGEN_SV
`define VORTEX_CLKGEN_SV
/* verilator lint_off UNUSEDPARAM */   // used only by the Xilinx branch
module vortex_clkgen #(
  parameter real IN_PERIOD_NS = 10.0,   // board clock period
  parameter real MULT         = 10.0,   // MMCM feedback multiplier
  parameter real DIV          = 20.0    // MMCM output divider
) (
  input  logic clk_in,
  output logic clk_out,
  output logic locked
);
`ifdef VORTEX_XILINX
  logic clk_fb, clk_unbuf;
  MMCME2_BASE #(
    .CLKIN1_PERIOD    (IN_PERIOD_NS),
    .CLKFBOUT_MULT_F  (MULT),
    .CLKOUT0_DIVIDE_F (DIV)
  ) u_mmcm (
    .CLKIN1   (clk_in),
    .CLKFBIN  (clk_fb),
    .CLKFBOUT (clk_fb),
    .CLKOUT0  (clk_unbuf),
    .LOCKED   (locked),
    .PWRDWN   (1'b0),
    .RST      (1'b0)
  );
  BUFG u_bufg (.I(clk_unbuf), .O(clk_out));
`else
  assign clk_out = clk_in;
  assign locked  = 1'b1;
`endif
endmodule : vortex_clkgen
/* verilator lint_on UNUSEDPARAM */
`endif
