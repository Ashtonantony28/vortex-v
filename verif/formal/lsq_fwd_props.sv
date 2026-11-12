// lsq_fwd_props.sv — formal properties for vortex_lsq_fwd (ADR 0009).
//
// Every input is free. `mem` stands for the 8 memory bytes at the load address.
// The reference applies the valid older stores to those bytes one by one, oldest
// first, exactly as committing them would, then reads the load's bytes. The
// forwarding unit must agree with it whenever it says it can answer.
module lsq_fwd_props #(
  parameter int unsigned XLEN = 32,
  parameter int unsigned N    = 3
) (
  input logic [XLEN-1:0]   ld_addr,
  input logic [1:0]        ld_size,
  input logic [N-1:0]      st_valid,
  input logic [N-1:0]      st_known,
  input logic [N*XLEN-1:0] st_addr,
  input logic [N*2-1:0]    st_size,
  input logic [N*XLEN-1:0] st_data,
  input logic [XLEN-1:0]   mem
);
  localparam int unsigned NB  = XLEN / 8;
  localparam int unsigned AW1 = XLEN + 1;

  logic            unknown, hit, partial;
  logic [XLEN-1:0] data;

  vortex_lsq_fwd #(.XLEN(XLEN), .N(N)) dut (
    .ld_addr  (ld_addr),
    .ld_size  (ld_size),
    .st_valid (st_valid),
    .st_known (st_known),
    .st_addr  (st_addr),
    .st_size  (st_size),
    .st_data  (st_data),
    .unknown  (unknown),
    .hit      (hit),
    .partial  (partial),
    .data     (data)
  );

  // Byte-level reference
  logic [XLEN-1:0] ref_bytes;      // load bytes after applying the stores
  logic [NB-1:0]   from_store;     // byte k was written by some store
  logic            any_unknown, any_overlap;
  always @* begin
    logic [AW1-1:0] a, s_lo, s_hi;
    logic [XLEN-1:0] off;
    ref_bytes   = mem;
    from_store  = '0;
    any_unknown = 1'b0;
    any_overlap = 1'b0;
    for (int j = 0; j < N; j++) begin
      if (st_valid[j] && !st_known[j]) any_unknown = 1'b1;
    end
    for (int k = 0; k < NB; k++) begin
      if (k < (1 << ld_size)) begin
        a = {1'b0, ld_addr} + AW1'(k);
        for (int j = 0; j < N; j++) begin
          s_lo = {1'b0, st_addr[j*XLEN +: XLEN]};
          s_hi = s_lo + (AW1'(1) << st_size[j*2 +: 2]);
          if (st_valid[j] && st_known[j] && (s_lo <= a) && (a < s_hi)) begin
            off = a[XLEN-1:0] - st_addr[j*XLEN +: XLEN];
            ref_bytes[k*8 +: 8] = st_data[j*XLEN + (off[$clog2(NB)-1:0])*8 +: 8];
            from_store[k] = 1'b1;
            any_overlap   = 1'b1;
          end
        end
      end
    end
  end

  always @* begin
    // Access sizes must fit the data width (a doubleword needs XLEN=64).
    if (XLEN < 64) begin
      assume (ld_size <= 2'd2);
      for (int j = 0; j < N; j++) assume (st_size[j*2 +: 2] <= 2'd2);
    end
    // Wait exactly when an older store's address is unknown.
    assert (unknown == any_unknown);
    // The three answers are exclusive.
    assert (!(hit && partial));
    if (!unknown) begin
      // A forwarded load sees exactly what memory would hold after the stores.
      if (hit) begin
        for (int k = 0; k < NB; k++)
          if (k < (1 << ld_size)) assert (data[k*8 +: 8] == ref_bytes[k*8 +: 8]);
      end
      // Reading memory is only allowed when no older store touches the load.
      if (!hit && !partial) assert (!any_overlap && (from_store == '0));
      // No spurious waits: a partial answer means some store really overlaps.
      if (partial) assert (any_overlap);
    end
  end
endmodule
