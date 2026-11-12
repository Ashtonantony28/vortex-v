// vortex_reset_sync.sv — asynchronous assert, synchronous release (ADR 0011).
//
// rst_n_out drops as soon as rst_n_in drops, and rises STAGES clock edges after
// rst_n_in rises, so every flop leaves reset on the same edge.
`ifndef VORTEX_RESET_SYNC_SV
`define VORTEX_RESET_SYNC_SV
module vortex_reset_sync #(
  parameter int unsigned STAGES = 2
) (
  input  logic clk,
  input  logic rst_n_in,
  output logic rst_n_out
);
  logic [STAGES-1:0] sync;
  always_ff @(posedge clk or negedge rst_n_in) begin
    if (!rst_n_in) sync <= '0;
    else           sync <= {sync[STAGES-2:0], 1'b1};
  end
  assign rst_n_out = sync[STAGES-1];
endmodule : vortex_reset_sync
`endif
