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
  localparam logic [6:0] OPC_MISC_MEM = 7'b0001111; // FENCE / FENCE.I
  localparam logic [6:0] OPC_SYSTEM   = 7'b1110011; // Zicsr, ECALL/EBREAK/MRET/WFI

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

  // ---- vortex_ooo_core functional-unit class of an issue-queue entry (ADR 0006) ----
  typedef enum logic [2:0] {
    FU_ALU,  // ALU, branches, JAL/JALR (1 cycle)
    FU_MUL,  // RV64M multiplies
    FU_DIV,  // RV64M divides/remainders (iterative, one in flight)
    FU_XM,   // Xmlacc ops (1 cycle)
    FU_AGU,  // load/store address + store data into the memory queue
    FU_SYS   // SYSTEM / illegal / FENCE.I: runs alone at the ROB head (ADR 0014)
  } fu_e;

  // ---------------------------------------------------------------------------
  // Zicsr and machine-mode traps (ADR 0014)
  // ---------------------------------------------------------------------------
  typedef enum logic [2:0] {
    SYS_CSR,     // CSRRW/CSRRS/CSRRC and the immediate forms
    SYS_ECALL,
    SYS_EBREAK,
    SYS_MRET,
    SYS_WFI,     // no interrupts yet: a no-op
    SYS_FENCEI   // a no-op that serializes, so younger instructions are fetched after it
  } sys_op_e;

  localparam logic [31:0] INST_ECALL  = 32'h0000_0073;
  localparam logic [31:0] INST_EBREAK = 32'h0010_0073;
  localparam logic [31:0] INST_MRET   = 32'h3020_0073;
  localparam logic [31:0] INST_WFI    = 32'h1050_0073;
  localparam logic [2:0]  F3_FENCE    = 3'b000;
  localparam logic [2:0]  F3_FENCEI   = 3'b001;
  localparam logic [2:0]  F3_PRIV     = 3'b000;   // SYSTEM funct3 for ECALL..WFI
  localparam logic [1:0]  CSR_OP_RW   = 2'b01;    // funct3[1:0]
  localparam logic [1:0]  CSR_OP_RS   = 2'b10;
  localparam logic [1:0]  CSR_OP_RC   = 2'b11;

  localparam logic [11:0] CSR_MSTATUS   = 12'h300;
  localparam logic [11:0] CSR_MISA      = 12'h301;
  localparam logic [11:0] CSR_MIE       = 12'h304;
  localparam logic [11:0] CSR_MTVEC     = 12'h305;
  localparam logic [11:0] CSR_MSCRATCH  = 12'h340;
  localparam logic [11:0] CSR_MEPC      = 12'h341;
  localparam logic [11:0] CSR_MCAUSE    = 12'h342;
  localparam logic [11:0] CSR_MTVAL     = 12'h343;
  localparam logic [11:0] CSR_MIP       = 12'h344;
  localparam logic [11:0] CSR_MLCFG     = 12'h800;
  localparam logic [11:0] CSR_MLSTAT    = 12'h801;
  localparam logic [11:0] CSR_MCYCLE    = 12'hB00;
  localparam logic [11:0] CSR_MINSTRET  = 12'hB02;
  localparam logic [11:0] CSR_CYCLE     = 12'hC00;
  localparam logic [11:0] CSR_INSTRET   = 12'hC02;
  localparam logic [11:0] CSR_MVENDORID = 12'hF11;
  localparam logic [11:0] CSR_MARCHID   = 12'hF12;
  localparam logic [11:0] CSR_MIMPID    = 12'hF13;
  localparam logic [11:0] CSR_MHARTID   = 12'hF14;

  localparam logic [63:0] CAUSE_ILLEGAL    = 64'd2;
  localparam logic [63:0] CAUSE_BREAKPOINT = 64'd3;
  localparam logic [63:0] CAUSE_ECALL_M    = 64'd11;
  // Misaligned traps (ADR 0016): a jump target, a load or a store address
  localparam logic [63:0] CAUSE_MISALIGNED_FETCH = 64'd0;
  localparam logic [63:0] CAUSE_MISALIGNED_LOAD  = 64'd4;
  localparam logic [63:0] CAUSE_MISALIGNED_STORE = 64'd6;
  // Machine timer interrupt (ADR 0015): mcause bit 63 set, code 7; mie.MTIE/mip.MTIP bit 7.
  localparam int unsigned IRQ_M_TIMER      = 7;
  localparam logic [63:0] CAUSE_M_TIMER_IRQ = (64'd1 << 63) | 64'd7;

  // mstatus fields: MIE, MPIE writable; MPP reads M (no other privilege modes).
  localparam int unsigned MSTATUS_MIE_BIT  = 3;
  localparam int unsigned MSTATUS_MPIE_BIT = 7;
  localparam logic [63:0] MSTATUS_MPP_M    = 64'h1800;
  // misa: MXL=2 (RV64), extensions I, M and X (non-standard: Xmlacc).
  localparam logic [63:0] MISA_VALUE       = (64'd2 << 62) | (64'd1 << 8) | (64'd1 << 12)
                                           | (64'd1 << 23);

  // Does `csr` exist, and if the access writes it, is it writable? Addresses with
  // bits 11:10 == 3 are read-only (privileged spec).
  function automatic logic csr_legal(input logic [11:0] csr, input logic writes);
    return ((csr == CSR_MSTATUS)  || (csr == CSR_MISA)      || (csr == CSR_MIE)
         || (csr == CSR_MTVEC)    || (csr == CSR_MSCRATCH)  || (csr == CSR_MEPC)
         || (csr == CSR_MCAUSE)   || (csr == CSR_MTVAL)     || (csr == CSR_MIP)
         || (csr == CSR_MLCFG)    || (csr == CSR_MLSTAT)    || (csr == CSR_MCYCLE)
         || (csr == CSR_MINSTRET) || (csr == CSR_CYCLE)     || (csr == CSR_INSTRET)
         || (csr == CSR_MVENDORID) || (csr == CSR_MARCHID)  || (csr == CSR_MIMPID)
         || (csr == CSR_MHARTID))
        && !(writes && (csr[11:10] == 2'b11));
  endfunction

endpackage : vortex_pkg
`endif
