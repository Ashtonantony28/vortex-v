// vortex_lsq_fwd.sv — store-to-load forwarding check for one load (ADR 0009).
//
// Given a load and the N stores older than it (index 0 oldest, N-1 youngest),
// decide whether the load may read memory, must take its bytes from a store, or
// must wait:
//   unknown — some older store has no address yet: wait.
//   hit     — the youngest store that overlaps the load covers all its bytes:
//             `data` holds the load's bytes, right-justified (extend outside).
//   partial — the youngest overlapping store covers only some bytes: wait for
//             it to commit, then read memory.
//   none of these — no older store overlaps: read memory.
// Address ranges are compared in XLEN+1 bits, so nothing wraps.
//
// Ports are flattened vectors so yosys can prove properties on this module
// directly (verif/formal/lsq_fwd_props.sv).
`ifndef VORTEX_LSQ_FWD_SV
`define VORTEX_LSQ_FWD_SV
module vortex_lsq_fwd #(
  parameter int unsigned XLEN = 64,
  parameter int unsigned N    = 8
) (
  input  logic [XLEN-1:0]   ld_addr,
  input  logic [1:0]        ld_size,     // SZ_* encoding: 1 << ld_size bytes
  input  logic [N-1:0]      st_valid,    // an older store occupies slot j
  input  logic [N-1:0]      st_known,    // its address and data are captured
  input  logic [N*XLEN-1:0] st_addr,
  input  logic [N*2-1:0]    st_size,
  input  logic [N*XLEN-1:0] st_data,     // right-justified store data
  output logic              unknown,
  output logic              hit,
  output logic              partial,
  output logic [XLEN-1:0]   data
);

  localparam int unsigned AW1 = XLEN + 1;
  localparam int unsigned OW  = $clog2(XLEN / 8);   // byte offset within a word

  logic [AW1-1:0] l_lo, l_hi;
  assign l_lo = {1'b0, ld_addr};
  assign l_hi = l_lo + (AW1'(1) << ld_size);

  logic            ov_found, ov_cover;
  logic [OW-1:0]   ov_off;
  logic [XLEN-1:0] ov_data;
  always_comb begin
    logic [AW1-1:0] s_lo, s_hi;
    unknown  = 1'b0;
    ov_found = 1'b0;
    ov_cover = 1'b0;
    ov_off   = '0;
    ov_data  = '0;
    for (int j = 0; j < N; j++) begin
      s_lo = {1'b0, st_addr[j*XLEN +: XLEN]};
      s_hi = s_lo + (AW1'(1) << st_size[j*2 +: 2]);
      if (st_valid[j]) begin
        if (!st_known[j]) begin
          unknown = 1'b1;
        end else if ((l_lo < s_hi) && (s_lo < l_hi)) begin
          // Later (younger) overlapping stores replace earlier ones.
          ov_found = 1'b1;
          ov_cover = (s_lo <= l_lo) && (l_hi <= s_hi);
          ov_off   = OW'(ld_addr[OW-1:0] - st_addr[j*XLEN +: OW]);
          ov_data  = st_data[j*XLEN +: XLEN];
        end
      end
    end
  end

  assign hit     = !unknown && ov_found && ov_cover;
  assign partial = !unknown && ov_found && !ov_cover;
  assign data    = ov_data >> {ov_off, 3'b000};

endmodule : vortex_lsq_fwd
`endif
