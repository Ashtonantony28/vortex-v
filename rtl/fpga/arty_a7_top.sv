// arty_a7_top.sv — Digilent Arty A7-100T top level for the Vortex-V SoC (ADR 0003, 0011).
//
// Board clock -> vortex_clkgen -> core clock. The red CK_RST button and the
// clock generator's lock drive reset, released synchronously. UART TX goes out
// on the USB-UART bridge at 115200 baud. LD4..LD7 show the program's LED
// register; RGB LED LD0 shows the result: green = pass, red = fail, blue = running.
// Pins: fpga/arty_a7_100t.xdc.
`ifndef ARTY_A7_TOP_SV
`define ARTY_A7_TOP_SV
module arty_a7_top #(
  parameter int unsigned CORE_HZ    = 50_000_000,   // must match vortex_clkgen's output
  parameter int unsigned BAUD       = 115_200,
  parameter              INIT_FILE  = "hello.hex",
  parameter bit          CORE_OOO   = 1'b1,
  parameter int unsigned WIDTH      = 2,
  parameter int unsigned SPEC_BRANCHES = 1,
  parameter int unsigned LSQ        = 1,
  parameter int unsigned XM_LATENCY = 3,
  parameter int unsigned SYNC_MEM   = 1             // block RAM (ADR 0012)
) (
  input  logic       CLK100MHZ,
  input  logic       ck_rst,          // active low
  output logic       uart_rxd_out,    // FPGA TX -> USB-UART RX
  output logic [3:0] led,
  output logic       led0_r,
  output logic       led0_g,
  output logic       led0_b
);
  localparam int unsigned CLKS_PER_BIT = CORE_HZ / BAUD;

  logic clk, locked, rst_n;
  vortex_clkgen u_clkgen (
    .clk_in  (CLK100MHZ),
    .clk_out (clk),
    .locked  (locked)
  );

  vortex_reset_sync u_rst (
    .clk       (clk),
    .rst_n_in  (ck_rst && locked),
    .rst_n_out (rst_n)
  );

  logic done, pass;
  vortex_soc #(
    .CORE_OOO      (CORE_OOO),
    .WIDTH         (WIDTH),
    .SPEC_BRANCHES (SPEC_BRANCHES),
    .LSQ           (LSQ),
    .XM_LATENCY    (XM_LATENCY),
    .SYNC_MEM      (SYNC_MEM),
    .CLKS_PER_BIT  (CLKS_PER_BIT),
    .INIT_FILE     (INIT_FILE)
  ) u_soc (
    .clk     (clk),
    .rst_n   (rst_n),
    .uart_tx (uart_rxd_out),
    .leds    (led),
    .done    (done),
    .pass    (pass)
  );

  assign led0_g = done && pass;
  assign led0_r = done && !pass;
  assign led0_b = !done;
endmodule : arty_a7_top
`endif
