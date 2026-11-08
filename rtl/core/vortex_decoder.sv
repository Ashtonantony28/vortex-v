// vortex_decoder.sv — combinational RV64I instruction decoder.
//
// Produces register addresses, the sign-extended immediate, the ALU operation
// and operand selects, the writeback source, and memory/branch/jump controls.
// RV64I base integer only (incl. the *W ops). No M/C/CSR/traps.
`ifndef VORTEX_DECODER_SV
`define VORTEX_DECODER_SV
module vortex_decoder
  import vortex_pkg::*;
#(
  parameter int unsigned XLEN = 64
) (
  input  logic [31:0]          inst,

  output logic [4:0]           rs1_addr,
  output logic [4:0]           rs2_addr,
  output logic [4:0]           rs3_addr,   // Xmlacc R4-type (mlacc.macc) 3rd source
  output logic [4:0]           rd_addr,

  output logic [XLEN-1:0]      imm,

  output vortex_pkg::alu_op_e  alu_op,
  output logic                 alu_a_pc,   // ALU a operand = PC (AUIPC/JAL/branch target)
  output logic                 alu_b_imm,  // ALU b operand = immediate (else rs2)

  output vortex_pkg::wb_src_e  wb_src,
  output logic                 reg_we,     // writes architectural rd

  output logic                 is_load,
  output logic                 is_store,
  output logic [1:0]           mem_size,
  output logic                 load_unsigned,

  output logic                 is_branch,
  output logic [2:0]           branch_f3,
  output logic                 is_jal,
  output logic                 is_jalr,

  output logic                 is_muldiv,  // RV64M op -> route to the mul/div unit
  output vortex_pkg::md_op_e   md_op,      // which M op (= funct3)
  output logic                 md_word,    // *W variant (32-bit, sign-extended)

  output logic                 is_xmlacc,  // Xmlacc op -> route to the ML unit
  output vortex_pkg::xmlacc_op_e xm_op,    // which Xmlacc op (= funct3)

  output logic                 illegal
);

  logic [6:0] opcode;
  logic [2:0] funct3;
  logic [6:0] funct7;
  assign opcode = inst[6:0];
  assign funct3 = inst[14:12];
  assign funct7 = inst[31:25];

  assign rs1_addr = inst[19:15];
  assign rs2_addr = inst[24:20];
  assign rs3_addr = inst[31:27];   // R4-type rs3 field (harmless for other fmts)
  assign rd_addr  = inst[11:7];

  // ---- Immediate forms (all sign-extended to XLEN) ----
  logic [XLEN-1:0] imm_i, imm_s, imm_b, imm_u, imm_j;
  assign imm_i = {{(XLEN-12){inst[31]}}, inst[31:20]};
  assign imm_s = {{(XLEN-12){inst[31]}}, inst[31:25], inst[11:7]};
  assign imm_b = {{(XLEN-13){inst[31]}}, inst[31], inst[7], inst[30:25], inst[11:8], 1'b0};
  assign imm_u = {{(XLEN-32){inst[31]}}, inst[31:12], 12'b0};
  assign imm_j = {{(XLEN-21){inst[31]}}, inst[31], inst[19:12], inst[20], inst[30:21], 1'b0};

  // shift-type flag (SRA vs SRL / SUB vs ADD) lives in funct7 bit 30 of inst
  logic alt_op; // inst[30]: 1 => SUB / SRA
  assign alt_op = inst[30];

  always_comb begin
    // defaults: treat as NOP-ish, no side effects
    imm           = imm_i;
    alu_op        = ALU_ADD;
    alu_a_pc      = 1'b0;
    alu_b_imm     = 1'b1;
    wb_src        = WB_ALU;
    reg_we        = 1'b0;
    is_load       = 1'b0;
    is_store      = 1'b0;
    mem_size      = SZ_WORD;
    load_unsigned = 1'b0;
    is_branch     = 1'b0;
    branch_f3     = funct3;
    is_jal        = 1'b0;
    is_jalr       = 1'b0;
    is_muldiv     = 1'b0;
    md_op         = md_op_e'(funct3);
    md_word       = 1'b0;
    is_xmlacc     = 1'b0;
    xm_op         = xmlacc_op_e'(funct3);
    illegal       = 1'b0;

    unique case (opcode)
      // ---- LUI ----
      OPC_LUI: begin
        imm       = imm_u;
        alu_op    = ALU_PASS_B;
        alu_b_imm = 1'b1;
        wb_src    = WB_ALU;
        reg_we    = 1'b1;
      end

      // ---- AUIPC ----
      OPC_AUIPC: begin
        imm       = imm_u;
        alu_op    = ALU_ADD;
        alu_a_pc  = 1'b1;
        alu_b_imm = 1'b1;
        wb_src    = WB_ALU;
        reg_we    = 1'b1;
      end

      // ---- JAL ----
      OPC_JAL: begin
        imm    = imm_j;
        is_jal = 1'b1;
        wb_src = WB_PC4;
        reg_we = 1'b1;
      end

      // ---- JALR ----
      OPC_JALR: begin
        imm     = imm_i;
        is_jalr = 1'b1;
        wb_src  = WB_PC4;
        reg_we  = 1'b1;
        if (funct3 != 3'b000) illegal = 1'b1;
      end

      // ---- BRANCH ----
      OPC_BRANCH: begin
        imm       = imm_b;
        is_branch = 1'b1;
        reg_we    = 1'b0;
        unique case (funct3)
          F3_BEQ, F3_BNE, F3_BLT, F3_BGE, F3_BLTU, F3_BGEU: illegal = 1'b0;
          default: illegal = 1'b1;
        endcase
      end

      // ---- LOAD ----
      OPC_LOAD: begin
        imm       = imm_i;
        alu_op    = ALU_ADD;   // address = rs1 + imm
        alu_b_imm = 1'b1;
        is_load   = 1'b1;
        wb_src    = WB_MEM;
        reg_we    = 1'b1;
        unique case (funct3)
          F3_LB : begin mem_size = SZ_BYTE;   load_unsigned = 1'b0; end
          F3_LH : begin mem_size = SZ_HALF;   load_unsigned = 1'b0; end
          F3_LW : begin mem_size = SZ_WORD;   load_unsigned = 1'b0; end
          F3_LD : begin mem_size = SZ_DOUBLE; load_unsigned = 1'b0; end
          F3_LBU: begin mem_size = SZ_BYTE;   load_unsigned = 1'b1; end
          F3_LHU: begin mem_size = SZ_HALF;   load_unsigned = 1'b1; end
          F3_LWU: begin mem_size = SZ_WORD;   load_unsigned = 1'b1; end
          default: illegal = 1'b1;
        endcase
      end

      // ---- STORE ----
      OPC_STORE: begin
        imm       = imm_s;
        alu_op    = ALU_ADD;   // address = rs1 + imm
        alu_b_imm = 1'b1;
        is_store  = 1'b1;
        reg_we    = 1'b0;
        unique case (funct3)
          F3_LB: mem_size = SZ_BYTE;   // SB
          F3_LH: mem_size = SZ_HALF;   // SH
          F3_LW: mem_size = SZ_WORD;   // SW
          F3_LD: mem_size = SZ_DOUBLE; // SD
          default: illegal = 1'b1;
        endcase
      end

      // ---- OP-IMM (64-bit) ----
      OPC_OP_IMM: begin
        imm       = imm_i;
        alu_b_imm = 1'b1;
        wb_src    = WB_ALU;
        reg_we    = 1'b1;
        unique case (funct3)
          F3_ADD_SUB: alu_op = ALU_ADD;                  // ADDI (no SUBI)
          F3_SLT    : alu_op = ALU_SLT;                  // SLTI
          F3_SLTU   : alu_op = ALU_SLTU;                 // SLTIU
          F3_XOR    : alu_op = ALU_XOR;
          F3_OR     : alu_op = ALU_OR;
          F3_AND    : alu_op = ALU_AND;
          F3_SLL    : begin                              // SLLI (shamt[5:0])
            alu_op = ALU_SLL;
            if (inst[31:26] != 6'b000000) illegal = 1'b1;
          end
          F3_SR     : begin                              // SRLI / SRAI
            alu_op = alu_op_e'(alt_op ? ALU_SRA : ALU_SRL);
            if (!((inst[31:26] == 6'b000000) || (inst[31:26] == 6'b010000)))
              illegal = 1'b1;
          end
          default: illegal = 1'b1;
        endcase
      end

      // ---- OP (64-bit register-register) ----
      OPC_OP: begin
        alu_b_imm = 1'b0;
        wb_src    = WB_ALU;
        reg_we    = 1'b1;
        if (funct7 == F7_MULDIV) begin
          // RV64M: all eight funct3 encodings are legal (MUL..REMU).
          is_muldiv = 1'b1;
          md_op     = md_op_e'(funct3);
          md_word   = 1'b0;
          wb_src    = WB_MULDIV;
        end else begin
          unique case (funct3)
            F3_ADD_SUB: alu_op = alu_op_e'(alt_op ? ALU_SUB : ALU_ADD);
            F3_SLL    : alu_op = ALU_SLL;
            F3_SLT    : alu_op = ALU_SLT;
            F3_SLTU   : alu_op = ALU_SLTU;
            F3_XOR    : alu_op = ALU_XOR;
            F3_SR     : alu_op = alu_op_e'(alt_op ? ALU_SRA : ALU_SRL);
            F3_OR     : alu_op = ALU_OR;
            F3_AND    : alu_op = ALU_AND;
            default   : illegal = 1'b1;
          endcase
          if (!((funct7 == 7'b0000000) || (funct7 == 7'b0100000)))
            illegal = 1'b1;
        end
      end

      // ---- OP-IMM-32 (*IW ops) ----
      OPC_OP_IMM_W: begin
        imm       = imm_i;
        alu_b_imm = 1'b1;
        wb_src    = WB_ALU;
        reg_we    = 1'b1;
        unique case (funct3)
          F3_ADD_SUB: alu_op = ALU_ADDW;                 // ADDIW
          F3_SLL    : begin                              // SLLIW (shamt[4:0])
            alu_op = ALU_SLLW;
            if (inst[31:25] != 7'b0000000) illegal = 1'b1;
          end
          F3_SR     : begin                              // SRLIW / SRAIW
            alu_op = alu_op_e'(alt_op ? ALU_SRAW : ALU_SRLW);
            if (!((inst[31:25] == 7'b0000000) || (inst[31:25] == 7'b0100000)))
              illegal = 1'b1;
          end
          default: illegal = 1'b1;
        endcase
      end

      // ---- OP-32 (*W register-register ops) ----
      OPC_OP_W: begin
        alu_b_imm = 1'b0;
        wb_src    = WB_ALU;
        reg_we    = 1'b1;
        if (funct7 == F7_MULDIV) begin
          // RV64M *W: MULW (f3=000), DIVW/DIVUW/REMW/REMUW (f3=100..111).
          is_muldiv = 1'b1;
          md_op     = md_op_e'(funct3);
          md_word   = 1'b1;
          wb_src    = WB_MULDIV;
          unique case (funct3)
            3'b000, 3'b100, 3'b101, 3'b110, 3'b111: illegal = 1'b0;
            default: illegal = 1'b1;
          endcase
        end else begin
          unique case (funct3)
            F3_ADD_SUB: alu_op = alu_op_e'(alt_op ? ALU_SUBW : ALU_ADDW);
            F3_SLL    : alu_op = ALU_SLLW;
            F3_SR     : alu_op = alu_op_e'(alt_op ? ALU_SRAW : ALU_SRLW);
            default   : illegal = 1'b1;
          endcase
          if (!((funct7 == 7'b0000000) || (funct7 == 7'b0100000)))
            illegal = 1'b1;
        end
      end

      // ---- CUSTOM0 — Xmlacc Tier-1 ML ops ----
      OPC_CUSTOM0: begin
        is_xmlacc = 1'b1;
        xm_op     = xmlacc_op_e'(funct3);
        wb_src    = WB_XMLACC;
        reg_we    = 1'b1;
        alu_b_imm = 1'b0;
        unique case (funct3)
          F3_DOT8, F3_DOT8U, F3_DOT8SU, F3_RQS: begin
            // R-type: funct7 must be zero.
            if (funct7 != 7'b0000000) illegal = 1'b1;
          end
          F3_MACC: begin
            // R4-type: funct2 (inst[26:25]) must be zero; rs3 = inst[31:27].
            if (inst[26:25] != 2'b00) illegal = 1'b1;
          end
          default: illegal = 1'b1;
        endcase
      end

      default: illegal = 1'b1;
    endcase
  end

endmodule : vortex_decoder
`endif
