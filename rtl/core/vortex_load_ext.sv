// vortex_load_ext.sv — load-data sign/zero extension shared by both cores.
//
// The data port returns `size` bytes right-justified (docs/M1-INTERFACE-v1.md);
// this widens them to XLEN per the load's funct3 (ADR 0006: shared, not copied).
`ifndef VORTEX_LOAD_EXT_SV
`define VORTEX_LOAD_EXT_SV
module vortex_load_ext
  import vortex_pkg::*;
#(
  parameter int unsigned XLEN = 64
) (
  input  logic [1:0]      size,       // SZ_* encoding
  input  logic            is_unsigned,
  input  logic [XLEN-1:0] raw,        // right-justified bytes from dmem
  output logic [XLEN-1:0] data
);

  always_comb begin
    unique case (size)
      SZ_BYTE  : data = is_unsigned ? {{(XLEN-8){1'b0}},       raw[7:0]}
                                    : {{(XLEN-8){raw[7]}},     raw[7:0]};
      SZ_HALF  : data = is_unsigned ? {{(XLEN-16){1'b0}},      raw[15:0]}
                                    : {{(XLEN-16){raw[15]}},   raw[15:0]};
      SZ_WORD  : data = is_unsigned ? {{(XLEN-32){1'b0}},      raw[31:0]}
                                    : {{(XLEN-32){raw[31]}},   raw[31:0]};
      SZ_DOUBLE: data = raw;
      default  : data = raw;
    endcase
  end

endmodule : vortex_load_ext
`endif
