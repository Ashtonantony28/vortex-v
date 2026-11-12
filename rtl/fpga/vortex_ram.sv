// vortex_ram.sv — unified instruction/data RAM for the SoC (ADR 0011).
//
// Behavioural, vendor-neutral model (ADR 0003): two 32-bit instruction read
// ports, one data read port and one data write port with byte enables.
// SYNC=0: reads are combinational (the M1 interface); on an FPGA this maps to
// distributed (LUT) RAM. SYNC=1: every read port registers its result, so a read
// returns one cycle after its address, which is how block RAM behaves (ADR 0012).
// Use SYNC=1 with a core built with SYNC_MEM=1.
//
// Accesses are naturally aligned (the core has no misaligned support yet).
// The image is loaded from INIT_FILE: one 64-bit little-endian word per line, in
// hex, starting at BASE.
`ifndef VORTEX_RAM_SV
`define VORTEX_RAM_SV
module vortex_ram #(
  parameter int unsigned XLEN       = 64,
  parameter int unsigned BYTES      = 16384,          // power of 2
  parameter logic [XLEN-1:0] BASE   = 64'h8000_0000,
  parameter              INIT_FILE  = "",  // untyped: Icarus overrides it with -P
  parameter bit          SYNC       = 1'b0
) (
  input  logic             clk,

  input  logic [XLEN-1:0]  iaddr0,
  output logic [31:0]      idata0,
  input  logic [XLEN-1:0]  iaddr1,
  output logic [31:0]      idata1,

  input  logic [XLEN-1:0]  daddr,
  output logic [XLEN-1:0]  drdata,     // the addressed bytes in the low bits
  input  logic             dwe,
  input  logic [1:0]       dsize,      // 0=byte .. 3=doubleword
  input  logic [XLEN-1:0]  dwdata
);
  localparam int unsigned NB    = XLEN / 8;             // bytes per word
  localparam int unsigned OW    = $clog2(NB);           // byte offset bits
  localparam int unsigned WORDS = BYTES / NB;
  localparam int unsigned IW    = $clog2(WORDS);        // word index bits

  logic [XLEN-1:0] mem [WORDS];

  initial begin
    for (int i = 0; i < WORDS; i++) mem[i] = '0;
    if (INIT_FILE != "") $readmemh(INIT_FILE, mem);
  end

  // Word index and byte offset of an address inside the RAM window
  /* verilator lint_off UNUSEDSIGNAL */   // only the word-index bits of the offset matter
  function automatic logic [IW-1:0] widx(input logic [XLEN-1:0] a);
    logic [XLEN-1:0] off;
    off  = a - BASE;
    widx = off[OW +: IW];
  endfunction
  /* verilator lint_on UNUSEDSIGNAL */

  // Read ports, as they would be combinationally
  logic [XLEN-1:0] iw0, iw1;
  logic [31:0]     i0_c, i1_c;
  logic [XLEN-1:0] d_c;
  logic [OW-1:0]   dofs;
  assign iw0  = mem[widx(iaddr0)];
  assign iw1  = mem[widx(iaddr1)];
  assign i0_c = iaddr0[2] ? iw0[63:32] : iw0[31:0];
  assign i1_c = iaddr1[2] ? iw1[63:32] : iw1[31:0];
  assign dofs = daddr[OW-1:0];
  assign d_c  = mem[widx(daddr)] >> {dofs, 3'b000};

  if (SYNC) begin : g_sync
    // Registered outputs: the data for this cycle's addresses appears next cycle.
    // A read in the same cycle as a write to the same word returns the old data.
    always_ff @(posedge clk) begin
      idata0 <= i0_c;
      idata1 <= i1_c;
      drdata <= d_c;
    end
  end else begin : g_comb
    assign idata0 = i0_c;
    assign idata1 = i1_c;
    assign drdata = d_c;
  end

  // Data write: byte enables from size and offset
  logic [NB-1:0]   be;
  logic [XLEN-1:0] wshift;
  assign be     = NB'(((1 << (1 << dsize)) - 1) << dofs);
  assign wshift = dwdata << {dofs, 3'b000};

  always_ff @(posedge clk) begin
    if (dwe) begin
      for (int b = 0; b < NB; b++)
        if (be[b]) mem[widx(daddr)][b*8 +: 8] <= wshift[b*8 +: 8];
    end
  end
endmodule : vortex_ram
`endif
