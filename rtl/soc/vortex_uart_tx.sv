// vortex_uart_tx.sv — 8N1 UART transmitter (ADR 0011).
//
// `valid` with `!busy` starts a frame: one start bit, 8 data bits LSB first, one
// stop bit, each CLKS_PER_BIT cycles long. The line idles high. Asynchronous
// active-low reset, like the core it serves.
`ifndef VORTEX_UART_TX_SV
`define VORTEX_UART_TX_SV
module vortex_uart_tx #(
  parameter int unsigned CLKS_PER_BIT = 868     // 100 MHz / 115200 baud
) (
  input  logic       clk,
  input  logic       rst_n,
  input  logic       valid,
  input  logic [7:0] data,
  output logic       busy,
  output logic       tx
);
  localparam int unsigned CW         = (CLKS_PER_BIT > 1) ? $clog2(CLKS_PER_BIT) : 1;
  localparam int unsigned FRAME_BITS = 10;      // start + 8 data + stop

  logic [CW-1:0] clk_cnt;
  logic [3:0]    bit_cnt;
  logic [8:0]    shreg;                         // {stop, data}: next bits to send

  assign busy = (bit_cnt != 4'd0);

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      clk_cnt <= '0;
      bit_cnt <= 4'd0;
      shreg   <= '1;
      tx      <= 1'b1;
    end else if (!busy) begin
      tx <= 1'b1;
      if (valid) begin
        shreg   <= {1'b1, data};
        bit_cnt <= 4'(FRAME_BITS);
        clk_cnt <= '0;
        tx      <= 1'b0;                        // start bit goes out now
      end
    end else if (clk_cnt == CW'(CLKS_PER_BIT - 1)) begin
      clk_cnt <= '0;
      bit_cnt <= bit_cnt - 4'd1;
      shreg   <= {1'b1, shreg[8:1]};
      tx      <= shreg[0];                      // data bits, then stop, then idle
    end else begin
      clk_cnt <= clk_cnt + CW'(1);
    end
  end
endmodule : vortex_uart_tx
`endif
