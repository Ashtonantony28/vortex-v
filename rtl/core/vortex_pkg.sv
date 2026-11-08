// vortex_pkg.sv — shared constants for the Vortex-V M1 in-order RV64I core.
//
// Per CLAUDE.md, opcode/funct fields and ALU op codes live in a shared package
// rather than as magic numbers buried in logic. The M1 folder rules confine this
// milestone's RTL to rtl/core/, so this package lives here (not rtl/pkg/); a
// later milestone can relocate it once the folder constraint is lifted.
`ifndef VORTEX_PKG_SV
`define VORTEX_PKG_SV
package vortex_pkg;

  // ---- Base opcodes (inst[6:0]) ----
  localparam logic [6:0] OPC_LOAD     = 7'b0000011;
  localparam logic [6:0] OPC_OP_IMM   = 7'b0010011;
  localparam logic [6:0] OPC_AUIPC    = 7'b0010111;
  localparam logic [6:0] OPC_OP_IMM_W = 7'b0011011; // ADDIW/SLLIW/SRLIW/SRAIW
  localparam logic [6:0] OPC_STORE    = 7'b0100011;
  localparam logic [6:0] OPC_OP       = 7'b0110011;
  localparam logic [6:0] OPC_LUI      = 7'b0110111;
  localparam logic [6:0] OPC_OP_W     = 7'b0111011; // ADDW/SUBW/SLLW/SRLW/SRAW
  localparam logic [6:0] OPC_BRANCH   = 7'b1100011;
  localparam logic [6:0] OPC_JALR     = 7'b1100111;
  localparam logic [6:0] OPC_JAL      = 7'b1101111;
  localparam logic [6:0] OPC_CUSTOM0  = 7'b0001011; // Xmlacc Tier-1 ML ops

  // ---- funct3 selects within the Xmlacc (CUSTOM0) family ----
  localparam logic [2:0] F3_DOT8   = 3'b000;
  localparam logic [2:0] F3_DOT8U  = 3'b001;
  localparam logic [2:0] F3_DOT8SU = 3'b010;
  localparam logic [2:0] F3_MACC   = 3'b100; // R4-type (reads rs3)
  localparam logic [2:0] F3_RQS    = 3'b101;

  // ---- funct7 that selects the RV64M mul/div family within OP / OP-32 ----
  localparam logic [6:0] F7_MULDIV = 7'b0000001;

  // ---- funct3 for OP / OP-IMM ----
  localparam logic [2:0] F3_ADD_SUB = 3'b000; // ADD/SUB/ADDI
  localparam logic [2:0] F3_SLL     = 3'b001;
  localparam logic [2:0] F3_SLT     = 3'b010;
  localparam logic [2:0] F3_SLTU    = 3'b011;
  localparam logic [2:0] F3_XOR     = 3'b100;
  localparam logic [2:0] F3_SR      = 3'b101; // SRL/SRA/SRLI/SRAI
  localparam logic [2:0] F3_OR      = 3'b110;
  localparam logic [2:0] F3_AND     = 3'b111;

  // ---- funct3 for BRANCH ----
  localparam logic [2:0] F3_BEQ  = 3'b000;
  localparam logic [2:0] F3_BNE  = 3'b001;
  localparam logic [2:0] F3_BLT  = 3'b100;
  localparam logic [2:0] F3_BGE  = 3'b101;
  localparam logic [2:0] F3_BLTU = 3'b110;
  localparam logic [2:0] F3_BGEU = 3'b111;

  // ---- funct3 for LOAD / STORE (also encodes size) ----
  localparam logic [2:0] F3_LB  = 3'b000;
  localparam logic [2:0] F3_LH  = 3'b001;
  localparam logic [2:0] F3_LW  = 3'b010;
  localparam logic [2:0] F3_LD  = 3'b011;
  localparam logic [2:0] F3_LBU = 3'b100;
  localparam logic [2:0] F3_LHU = 3'b101;
  localparam logic [2:0] F3_LWU = 3'b110;

  // ---- dmem_size encoding (matches M1-INTERFACE-v1.md) ----
  localparam logic [1:0] SZ_BYTE   = 2'd0;
  localparam logic [1:0] SZ_HALF   = 2'd1;
  localparam logic [1:0] SZ_WORD   = 2'd2;
  localparam logic [1:0] SZ_DOUBLE = 2'd3;

  // ---- ALU operation select ----
  typedef enum logic [4:0] {
    ALU_ADD,
    ALU_SUB,
    ALU_SLL,
    ALU_SLT,
    ALU_SLTU,
    ALU_XOR,
    ALU_SRL,
    ALU_SRA,
    ALU_OR,
    ALU_AND,
    ALU_ADDW,
    ALU_SUBW,
    ALU_SLLW,
    ALU_SRLW,
    ALU_SRAW,
    ALU_PASS_B  // LUI: pass the (already-formed) immediate through
  } alu_op_e;

  // ---- writeback result source ----
  typedef enum logic [2:0] {
    WB_ALU,     // ALU result
    WB_MEM,     // load data
    WB_PC4,     // pc + 4 (JAL / JALR link)
    WB_MULDIV,  // RV64M multiply/divide unit result
    WB_XMLACC   // Xmlacc (custom ML) unit result
  } wb_src_e;

  // ---- Xmlacc operation select (= funct3 within CUSTOM0) ----
  typedef enum logic [2:0] {
    XM_DOT8   = 3'b000, // rd = SX64( sum sext8(rs1[i]) * sext8(rs2[i]) )
    XM_DOT8U  = 3'b001, // rd = ZX64( sum zext8(rs1[i]) * zext8(rs2[i]) )
    XM_DOT8SU = 3'b010, // rd = SX64( sum sext8(rs1[i]) * zext8(rs2[i]) )
    XM_MACC   = 3'b100, // rd = rs3 + sum sext8(rs1[i]) * sext8(rs2[i])
    XM_RQS    = 3'b101  // requantize int32->int8: round/ASR/clamp, sticky SAT
  } xmlacc_op_e;

  // ---- RV64M operation select (funct3 within the F7_MULDIV family) ----
  // Encoding matches inst[14:12] so the decoder can cast funct3 directly.
  typedef enum logic [2:0] {
    MD_MUL    = 3'b000, // MUL  / MULW   : low XLEN (or low 32) of product
    MD_MULH   = 3'b001, // MULH          : high 64, signed   x signed
    MD_MULHSU = 3'b010, // MULHSU        : high 64, signed   x unsigned
    MD_MULHU  = 3'b011, // MULHU         : high 64, unsigned x unsigned
    MD_DIV    = 3'b100, // DIV  / DIVW   : signed quotient
    MD_DIVU   = 3'b101, // DIVU / DIVUW  : unsigned quotient
    MD_REM    = 3'b110, // REM  / REMW   : signed remainder
    MD_REMU   = 3'b111  // REMU / REMUW  : unsigned remainder
  } md_op_e;

endpackage : vortex_pkg
`endif
