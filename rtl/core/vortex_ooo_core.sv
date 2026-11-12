// vortex_ooo_core.sv — Vortex-V M4 out-of-order RV64IM + Xmlacc core (top level).
//
// Implements ADR 0006 (M4) and, with WIDTH=2, ADR 0007 (M5 two-wide). It has the
// vortex_core ports (docs/M1-INTERFACE-v1.md) plus the additive v1.1 ports: a
// second fetch port (imem_addr1 = pc + 4) and a second retire slot (dbg1_*).
// Every suite that checks the in-order core checks this one at both widths.
//
// WIDTH=2: up to 2 instructions dispatch, issue and commit per cycle. Slot 1
// dispatches behind a non-control-flow slot 0 and renames against it; issue
// port 1 is ALU/branch only; commit slot 1 takes the next complete ROB entry.
//
//   fetch/decode/rename/dispatch (1 per cycle)
//     -> ROB (in order) + unified age-ordered issue queue + in-order memory queue
//   issue: oldest ready entry whose FU is free, 1 per cycle, reads 3 PRF operands
//   FUs:   ALU/branch (1 cycle), multiplier, divider (iterative), Xmlacc (1 cycle),
//          AGU (computes the address and captures store data into the memory queue)
//   memory queue head: a load accesses dmem when its address is known; a store
//          only when it is also the ROB head, writing memory as it commits
//   commit: in order, 1 per cycle, drives dbg_*; frees the old physical register
//
// Branches: with SPEC_BRANCHES=0 (default) dispatch stops after a conditional
// branch until it executes (ADR 0006). With SPEC_BRANCHES=1 (ADR 0008) a 2-bit
// BHT predicts it, fetch continues down the predicted path, and a mispredict
// restores a RAT/free-list checkpoint and squashes everything younger. JALR
// always stops dispatch until it executes. JAL redirects at decode.
//
// Wakeup is a global per-preg ready vector: a producer sets its bit as it writes
// the PRF at the end of its execute cycle, and a consumer can issue the next
// cycle reading the PRF directly, so 1-cycle ops issue back to back with no
// bypass network.
//
// Reset is asynchronous active-low, matching the in-order core and shared units.
`ifndef VORTEX_OOO_CORE_SV
`define VORTEX_OOO_CORE_SV
module vortex_ooo_core
  import vortex_pkg::*;
#(
  parameter int unsigned      XLEN        = 64,
  parameter logic [XLEN-1:0]  RESET_PC    = 64'h8000_0000,
  parameter logic [XLEN-1:0]  TOHOST_ADDR = 64'h8000_1000,
  parameter int unsigned      NUM_PREGS   = 64,  // physical registers (power of 2)
  parameter int unsigned      ROB_DEPTH   = 32,  // reorder buffer entries (power of 2)
  parameter int unsigned      IQ_DEPTH    = 16,  // unified issue queue entries
  parameter int unsigned      MEMQ_DEPTH  = 8,   // in-order memory queue (power of 2)
  parameter int unsigned      WIDTH       = 1,   // 1 = ADR 0006 scalar, 2 = ADR 0007 two-wide
  parameter int unsigned      SPEC_BRANCHES = 0, // unresolved predicted branches in flight (0/1)
  parameter int unsigned      BHT_ENTRIES = 64,  // 2-bit branch counters (power of 2)
  parameter int unsigned      LSQ         = 0    // 1 = loads pass older stores (ADR 0009)
) (
  input  logic              clk,
  input  logic              rst_n,

  // Instruction fetch
  output logic [XLEN-1:0]   imem_addr,
  input  logic [31:0]       imem_rdata,
  output logic [XLEN-1:0]   imem_addr1,   // v1.1: second fetch port, always pc + 4
  input  logic [31:0]       imem_rdata1,

  // Data memory
  output logic              dmem_req,
  output logic              dmem_we,
  output logic [XLEN-1:0]   dmem_addr,
  output logic [1:0]        dmem_size,
  output logic [XLEN-1:0]   dmem_wdata,
  input  logic [XLEN-1:0]   dmem_rdata,

  // Debug / retire introspection (driven at commit)
  output logic              dbg_retire_valid,
  output logic [XLEN-1:0]   dbg_retire_pc,
  output logic              dbg_wb_valid,
  output logic [4:0]        dbg_wb_rd,
  output logic [XLEN-1:0]   dbg_wb_data,
  output logic              dbg_mlstat_sat,

  // v1.1: second retire slot (younger than slot 0 in the same cycle; WIDTH=2 only)
  output logic              dbg1_retire_valid,
  output logic [XLEN-1:0]   dbg1_retire_pc,
  output logic              dbg1_wb_valid,
  output logic [4:0]        dbg1_wb_rd,
  output logic [XLEN-1:0]   dbg1_wb_data
);

  // ---------------------------------------------------------------------------
  // Sizes
  // ---------------------------------------------------------------------------
  localparam int unsigned NARCH     = 32;                  // architectural x-registers
  localparam int unsigned AW        = $clog2(NARCH);
  localparam int unsigned PW        = $clog2(NUM_PREGS);
  localparam int unsigned RW        = $clog2(ROB_DEPTH);
  localparam int unsigned QW        = $clog2(IQ_DEPTH);
  localparam int unsigned MW        = $clog2(MEMQ_DEPTH);
  localparam int unsigned NFREE0    = NUM_PREGS - NARCH;   // free pregs after reset
  localparam int unsigned DBG_CNT_W = 32;
  localparam logic [XLEN-1:0] INST_BYTES = XLEN'(4);
  localparam bit          DUAL      = (WIDTH == 2);
  localparam bit          SPEC      = (SPEC_BRANCHES != 0);
  localparam int unsigned BW        = $clog2(BHT_ENTRIES);
  localparam bit          USE_LSQ   = (LSQ != 0);

  // Parameter checks (ADR 0006 item 8). Evaluated at time zero in simulation and
  // by lint; a bad configuration stops the run before any instruction executes.
  initial begin
    if (NUM_PREGS < NARCH + ROB_DEPTH)
      $fatal(1, "vortex_ooo_core: NUM_PREGS (%0d) must be >= 32 + ROB_DEPTH (%0d)",
             NUM_PREGS, ROB_DEPTH);
    if ((1 << PW) != NUM_PREGS || (1 << RW) != ROB_DEPTH || (1 << MW) != MEMQ_DEPTH)
      $fatal(1, "vortex_ooo_core: NUM_PREGS, ROB_DEPTH and MEMQ_DEPTH must be powers of 2");
    if (SPEC_BRANCHES > 1 || (1 << BW) != BHT_ENTRIES)
      $fatal(1, "vortex_ooo_core: SPEC_BRANCHES must be 0 or 1 and BHT_ENTRIES a power of 2");
    if (WIDTH < 1 || WIDTH > 2)
      $fatal(1, "vortex_ooo_core: WIDTH must be 1 or 2 (ADR 0002), got %0d", WIDTH);
    if (IQ_DEPTH < 2 || IQ_DEPTH > ROB_DEPTH || MEMQ_DEPTH > ROB_DEPTH)
      $fatal(1, "vortex_ooo_core: need 2 <= IQ_DEPTH <= ROB_DEPTH and MEMQ_DEPTH <= ROB_DEPTH");
  end

  // ---------------------------------------------------------------------------
  // State declarations
  // ---------------------------------------------------------------------------
  // Front end
  logic [XLEN-1:0] pc;
  logic            fe_wait;    // a branch/JALR is in flight; dispatch is stopped
  logic            halted;     // the tohost store has committed

  // Branch speculation (ADR 0008; unused when SPEC_BRANCHES=0)
  logic [1:0]      bht        [BHT_ENTRIES];
  logic            spec_active;              // a predicted branch is unresolved
  logic [RW-1:0]   br_rob;                   // its ROB index
  logic [PW-1:0]   ckpt_rat   [NARCH];       // speculative RAT just after it
  logic [PW-1:0]   ckpt_fl_head;             // free-list head just after it
  logic            mul_kill, div_kill;       // running op was squashed: drop result

  // Physical register file, ready bits, rename tables, free list
  logic [XLEN-1:0] prf        [NUM_PREGS];
  logic            preg_ready [NUM_PREGS];
  logic [PW-1:0]   spec_rat   [NARCH];
  logic [PW-1:0]   arch_rat   [NARCH];
  logic [PW-1:0]   fl_q       [NUM_PREGS];
  logic [PW-1:0]   fl_head, fl_tail;
  logic [PW:0]     fl_count;

  // Reorder buffer
  logic [XLEN-1:0] rob_pc       [ROB_DEPTH];
  logic [AW-1:0]   rob_rd       [ROB_DEPTH];
  logic            rob_has_rd   [ROB_DEPTH];
  logic [PW-1:0]   rob_newp     [ROB_DEPTH];
  logic [PW-1:0]   rob_oldp     [ROB_DEPTH];
  logic            rob_complete [ROB_DEPTH];
  logic            rob_sat      [ROB_DEPTH];
  logic            rob_illegal  [ROB_DEPTH];
  logic            rob_load     [ROB_DEPTH];   // LSQ: a load, popped from the memq at commit
  logic [RW-1:0]   rob_head, rob_tail;
  logic [RW:0]     rob_count;

  // Issue queue
  logic            iq_valid   [IQ_DEPTH];
  logic [2:0]      iq_fu      [IQ_DEPTH];
  logic [RW-1:0]   iq_rob     [IQ_DEPTH];
  logic [MW-1:0]   iq_memq    [IQ_DEPTH];
  logic [XLEN-1:0] iq_pc      [IQ_DEPTH];
  logic [XLEN-1:0] iq_imm     [IQ_DEPTH];
  logic [PW-1:0]   iq_src1    [IQ_DEPTH];
  logic [PW-1:0]   iq_src2    [IQ_DEPTH];
  logic [PW-1:0]   iq_src3    [IQ_DEPTH];
  logic [PW-1:0]   iq_dst     [IQ_DEPTH];
  logic            iq_has_rd  [IQ_DEPTH];
  logic [4:0]      iq_alu_op  [IQ_DEPTH];
  logic            iq_a_pc    [IQ_DEPTH];
  logic            iq_b_imm   [IQ_DEPTH];
  logic            iq_link    [IQ_DEPTH];   // result is pc + 4 (JAL/JALR)
  logic            iq_branch  [IQ_DEPTH];
  logic            iq_jalr    [IQ_DEPTH];
  logic            iq_spec    [IQ_DEPTH];   // predicted branch (ADR 0008)
  logic            iq_pred    [IQ_DEPTH];   // its predicted direction
  logic [2:0]      iq_bf3     [IQ_DEPTH];
  logic [2:0]      iq_md_op   [IQ_DEPTH];
  logic            iq_md_word [IQ_DEPTH];
  logic [2:0]      iq_xm_op   [IQ_DEPTH];

  // Memory queue
  logic            mq_store [MEMQ_DEPTH];
  logic [RW-1:0]   mq_rob   [MEMQ_DEPTH];
  logic            mq_ready [MEMQ_DEPTH];   // address (and store data) captured
  logic [XLEN-1:0] mq_addr  [MEMQ_DEPTH];
  logic [XLEN-1:0] mq_data  [MEMQ_DEPTH];
  logic [1:0]      mq_size  [MEMQ_DEPTH];
  logic            mq_uns   [MEMQ_DEPTH];
  logic [PW-1:0]   mq_dst   [MEMQ_DEPTH];
  logic            mq_has_rd[MEMQ_DEPTH];
  logic            mq_done  [MEMQ_DEPTH];   // LSQ: load already executed
  logic [MW-1:0]   mq_head, mq_tail;
  logic [MW:0]     mq_count;

  // Debug: completions (single-cycle and multiplier ports) while an older ROB
  // entry was still incomplete. Nonzero proves out-of-order completion (ADR 0006 verification).
  logic [DBG_CNT_W-1:0] dbg_ooo_completions;
  // Debug: cycles in which two instructions committed (WIDTH=2 proof aid).
  logic [DBG_CNT_W-1:0] dbg_dual_commits;
  // Debug: predicted branches resolved, and how many of them mispredicted.
  logic [DBG_CNT_W-1:0] dbg_spec_branches, dbg_mispredicts;
  // Debug (LSQ): loads answered from a store, and loads executed behind an
  // older memory operation still in the queue.
  logic [DBG_CNT_W-1:0] dbg_lsq_forwards, dbg_lsq_early;

  // ---------------------------------------------------------------------------
  // Fetch + decode
  // ---------------------------------------------------------------------------
  logic [31:0] inst;
  assign imem_addr = pc;
  assign inst      = imem_rdata;

  logic [31:0] inst1;
  assign imem_addr1 = pc + INST_BYTES;
  assign inst1      = imem_rdata1;

  logic [4:0]       rs1_addr, rs2_addr, rs3_addr, rd_addr;
  logic             use_rs1, use_rs2, use_rs3;
  logic [XLEN-1:0]  imm;
  alu_op_e          alu_op;
  logic             alu_a_pc, alu_b_imm;
  wb_src_e          wb_src;
  logic             reg_we;
  logic             is_load, is_store;
  logic [1:0]       mem_size;
  logic             load_unsigned;
  logic             is_branch;
  logic [2:0]       branch_f3;
  logic             is_jal, is_jalr;
  logic             is_muldiv;
  md_op_e           md_op;
  logic             md_word;
  logic             is_xmlacc;
  xmlacc_op_e       xm_op;
  logic             dec_illegal;

  vortex_decoder #(.XLEN(XLEN)) u_decoder (
    .inst          (inst),
    .rs1_addr      (rs1_addr),
    .rs2_addr      (rs2_addr),
    .rs3_addr      (rs3_addr),
    .rd_addr       (rd_addr),
    .use_rs1       (use_rs1),
    .use_rs2       (use_rs2),
    .use_rs3       (use_rs3),
    .imm           (imm),
    .alu_op        (alu_op),
    .alu_a_pc      (alu_a_pc),
    .alu_b_imm     (alu_b_imm),
    .wb_src        (wb_src),
    .reg_we        (reg_we),
    .is_load       (is_load),
    .is_store      (is_store),
    .mem_size      (mem_size),
    .load_unsigned (load_unsigned),
    .is_branch     (is_branch),
    .branch_f3     (branch_f3),
    .is_jal        (is_jal),
    .is_jalr       (is_jalr),
    .is_muldiv     (is_muldiv),
    .md_op         (md_op),
    .md_word       (md_word),
    .is_xmlacc     (is_xmlacc),
    .xm_op         (xm_op),
    .illegal       (dec_illegal)
  );


  // Slot 1 decoder (its outputs are ignored when WIDTH=1)
  logic [4:0]       rs1_addr1, rs2_addr1, rs3_addr1, rd_addr1;
  logic             use_rs1_1, use_rs2_1, use_rs3_1;
  logic [XLEN-1:0]  imm1;
  alu_op_e          alu_op1;
  logic             alu_a_pc1, alu_b_imm1;
  wb_src_e          wb_src1;
  logic             reg_we1;
  logic             is_load1, is_store1;
  logic [1:0]       mem_size1;
  logic             load_unsigned1;
  logic             is_branch1;
  logic [2:0]       branch_f3_1;
  logic             is_jal1, is_jalr1;
  logic             is_muldiv1;
  md_op_e           md_op1;
  logic             md_word1;
  logic             is_xmlacc1;
  xmlacc_op_e       xm_op1;
  logic             dec_illegal1;

  vortex_decoder #(.XLEN(XLEN)) u_decoder1 (
    .inst          (inst1),
    .rs1_addr      (rs1_addr1),
    .rs2_addr      (rs2_addr1),
    .rs3_addr      (rs3_addr1),
    .rd_addr       (rd_addr1),
    .use_rs1       (use_rs1_1),
    .use_rs2       (use_rs2_1),
    .use_rs3       (use_rs3_1),
    .imm           (imm1),
    .alu_op        (alu_op1),
    .alu_a_pc      (alu_a_pc1),
    .alu_b_imm     (alu_b_imm1),
    .wb_src        (wb_src1),
    .reg_we        (reg_we1),
    .is_load       (is_load1),
    .is_store      (is_store1),
    .mem_size      (mem_size1),
    .load_unsigned (load_unsigned1),
    .is_branch     (is_branch1),
    .branch_f3     (branch_f3_1),
    .is_jal        (is_jal1),
    .is_jalr       (is_jalr1),
    .is_muldiv     (is_muldiv1),
    .md_op         (md_op1),
    .md_word       (md_word1),
    .is_xmlacc     (is_xmlacc1),
    .xm_op         (xm_op1),
    .illegal       (dec_illegal1)
  );

  // Functional-unit class of a decoded instruction
  function automatic fu_e fu_class(input logic ld_or_st, input logic md, input md_op_e mop,
                                   input logic xm);
    if (ld_or_st) return FU_AGU;
    if (md) begin
      if ((mop == MD_MUL) || (mop == MD_MULH) || (mop == MD_MULHSU) || (mop == MD_MULHU))
        return FU_MUL;
      return FU_DIV;
    end
    if (xm) return FU_XM;
    return FU_ALU;
  endfunction

  fu_e d_fu, d_fu1;
  assign d_fu  = fu_class(is_load || is_store, is_muldiv, md_op, is_xmlacc);
  assign d_fu1 = fu_class(is_load1 || is_store1, is_muldiv1, md_op1, is_xmlacc1);

  logic d_has_rd, d_is_mem, d_ctrl_wait;
  assign d_has_rd    = reg_we && (rd_addr != '0);
  assign d_is_mem    = is_load || is_store;
  assign d_ctrl_wait = is_jalr || (is_branch && !SPEC);   // stop dispatch until it resolves

  logic d_has_rd1, d_is_mem1, d_ctrl_wait1;
  assign d_has_rd1    = reg_we1 && (rd_addr1 != '0);
  assign d_is_mem1    = is_load1 || is_store1;
  assign d_ctrl_wait1 = is_jalr1 || (is_branch1 && !SPEC);

  // Branch prediction at decode: 2-bit counter, predict taken if bit 1 is set.
  logic d_spec0, d_spec1, pred0, pred1;
  assign d_spec0 = SPEC && is_branch;
  assign d_spec1 = SPEC && is_branch1;
  assign pred0   = bht[pc[BW+1:2]][1];
  assign pred1   = bht[imem_addr1[BW+1:2]][1];

  // ---------------------------------------------------------------------------
  // Rename
  // ---------------------------------------------------------------------------
  logic [PW-1:0] d_src1, d_src2, d_src3, d_newp, d_oldp;
  assign d_src1 = use_rs1 ? spec_rat[rs1_addr] : '0;
  assign d_src2 = use_rs2 ? spec_rat[rs2_addr] : '0;
  assign d_src3 = use_rs3 ? spec_rat[rs3_addr] : '0;
  assign d_newp = d_has_rd ? fl_q[fl_head] : '0;
  assign d_oldp = spec_rat[rd_addr];

  // Slot 1 renames against slot 0 of the same group (ADR 0007 item 4).
  logic [PW-1:0] d_src1_1, d_src2_1, d_src3_1, d_newp1, d_oldp1;
  logic [PW-1:0] fl_head1;
  assign d_src1_1 = !use_rs1_1 ? '0 : (d_has_rd && (rs1_addr1 == rd_addr)) ? d_newp
                                    : spec_rat[rs1_addr1];
  assign d_src2_1 = !use_rs2_1 ? '0 : (d_has_rd && (rs2_addr1 == rd_addr)) ? d_newp
                                    : spec_rat[rs2_addr1];
  assign d_src3_1 = !use_rs3_1 ? '0 : (d_has_rd && (rs3_addr1 == rd_addr)) ? d_newp
                                    : spec_rat[rs3_addr1];
  assign fl_head1 = fl_head + PW'(d_has_rd);
  assign d_newp1  = d_has_rd1 ? fl_q[fl_head1] : '0;
  assign d_oldp1  = (d_has_rd && (rd_addr1 == rd_addr)) ? d_newp : spec_rat[rd_addr1];

  // First two free issue-queue slots
  logic          iq_has_free, iq_has_free2;
  logic [QW-1:0] iq_free_idx, iq_free_idx2;
  always_comb begin
    iq_has_free  = 1'b0;
    iq_has_free2 = 1'b0;
    iq_free_idx  = '0;
    iq_free_idx2 = '0;
    for (int i = 0; i < IQ_DEPTH; i++) begin
      if (!iq_valid[i]) begin
        if (!iq_has_free) begin
          iq_has_free = 1'b1;
          iq_free_idx = QW'(i);
        end else if (!iq_has_free2) begin
          iq_has_free2 = 1'b1;
          iq_free_idx2 = QW'(i);
        end
      end
    end
  end

  logic squash;   // a predicted branch resolved the other way this cycle

  logic dispatch;
  assign dispatch = !halted && !fe_wait && !squash
                 && !(d_spec0 && spec_active)   // one predicted branch at a time
                 && (rob_count != (RW+1)'(ROB_DEPTH))
                 && iq_has_free
                 && (!d_is_mem || (mq_count != (MW+1)'(MEMQ_DEPTH)))
                 && (!d_has_rd || (fl_count != '0));

  // Slot 1 goes only behind a dispatching, non-control-flow slot 0, and only if
  // there is room for both instructions.
  logic [MW:0] mq_need;
  logic [PW:0] fl_need;
  assign mq_need = (MW+1)'(d_is_mem) + (MW+1)'(d_is_mem1);
  assign fl_need = (PW+1)'(d_has_rd) + (PW+1)'(d_has_rd1);

  logic [RW-1:0] rob_tail1;
  logic [MW-1:0] mq_tail1;
  assign rob_tail1 = rob_tail + RW'(1);
  assign mq_tail1  = mq_tail + MW'(d_is_mem);

  logic dispatch1;
  assign dispatch1 = DUAL && dispatch && !(is_jal || is_branch || is_jalr)
                  && !(d_spec1 && spec_active)
                  && (rob_count <= (RW+1)'(ROB_DEPTH - 2))
                  && iq_has_free2
                  && ((mq_count + mq_need) <= (MW+1)'(MEMQ_DEPTH))
                  && (fl_need <= fl_count);

  // ---------------------------------------------------------------------------
  // Issue select: oldest (by ROB age) valid entry whose sources are ready and
  // whose functional unit can accept it.
  // ---------------------------------------------------------------------------
  logic mul_busy, div_busy;

  // Port 1 (WIDTH=2): oldest ready ALU/branch entry.
  logic          issue1;
  logic [QW-1:0] s1_idx;
  always_comb begin
    logic [RW-1:0] best_age, age;
    issue1   = 1'b0;
    s1_idx   = '0;
    best_age = '0;
    for (int i = 0; i < IQ_DEPTH; i++) begin
      age = iq_rob[i] - rob_head;
      if (DUAL && iq_valid[i] && (fu_e'(iq_fu[i]) == FU_ALU)
          && preg_ready[iq_src1[i]] && preg_ready[iq_src2[i]]
          && (!issue1 || (age < best_age))) begin
        issue1   = 1'b1;
        s1_idx   = QW'(i);
        best_age = age;
      end
    end
  end

  // Port 0: oldest ready entry of any unit that port 1 did not take.
  logic          issue;
  logic [QW-1:0] s_idx;
  always_comb begin
    logic [RW-1:0] best_age, age;
    logic          fu_ok;
    issue    = 1'b0;
    s_idx    = '0;
    best_age = '0;
    for (int i = 0; i < IQ_DEPTH; i++) begin
      unique case (fu_e'(iq_fu[i]))
        FU_MUL : fu_ok = !mul_busy;
        FU_DIV : fu_ok = !div_busy;
        default: fu_ok = 1'b1;
      endcase
      age = iq_rob[i] - rob_head;
      if (iq_valid[i] && fu_ok && !(issue1 && (QW'(i) == s1_idx))
          && preg_ready[iq_src1[i]] && preg_ready[iq_src2[i]]
          && preg_ready[iq_src3[i]] && (!issue || (age < best_age))) begin
        issue    = 1'b1;
        s_idx    = QW'(i);
        best_age = age;
      end
    end
  end

  // Issue-stage fields and operand reads
  fu_e             i_fu;
  logic [XLEN-1:0] op1, op2, op3, i_pc, i_imm;
  assign i_fu  = fu_e'(iq_fu[s_idx]);
  assign op1   = prf[iq_src1[s_idx]];
  assign op2   = prf[iq_src2[s_idx]];
  assign op3   = prf[iq_src3[s_idx]];
  assign i_pc  = iq_pc[s_idx];
  assign i_imm = iq_imm[s_idx];

  // ---------------------------------------------------------------------------
  // ALU / branch FU
  // ---------------------------------------------------------------------------
  logic [XLEN-1:0] alu_a, alu_b, alu_y, alu_res;
  assign alu_a   = iq_a_pc[s_idx]  ? i_pc  : op1;
  assign alu_b   = iq_b_imm[s_idx] ? i_imm : op2;
  assign alu_res = iq_link[s_idx]  ? (i_pc + INST_BYTES) : alu_y;

  vortex_alu #(.XLEN(XLEN)) u_alu (
    .op (alu_op_e'(iq_alu_op[s_idx])),
    .a  (alu_a),
    .b  (alu_b),
    .y  (alu_y)
  );

  logic br_taken;
  vortex_branch_cmp #(.XLEN(XLEN)) u_branch_cmp (
    .f3    (iq_bf3[s_idx]),
    .a     (op1),
    .b     (op2),
    .taken (br_taken)
  );

  logic [XLEN-1:0] jalr_target, br_target;
  assign jalr_target = (op1 + i_imm) & ~XLEN'(1);   // LSB cleared per spec
  assign br_target   = i_pc + i_imm;

  logic redirect;
  assign redirect = issue && (i_fu == FU_ALU)
                 && (iq_jalr[s_idx] || (iq_branch[s_idx] && !iq_spec[s_idx]));

  // ---------------------------------------------------------------------------
  // Issue port 1: second ALU / branch unit (WIDTH=2)
  // ---------------------------------------------------------------------------
  logic [XLEN-1:0] p1_op1, p1_op2, p1_pc, p1_imm, p1_a, p1_b, p1_y, p1_res;
  assign p1_op1 = prf[iq_src1[s1_idx]];
  assign p1_op2 = prf[iq_src2[s1_idx]];
  assign p1_pc  = iq_pc[s1_idx];
  assign p1_imm = iq_imm[s1_idx];
  assign p1_a   = iq_a_pc[s1_idx]  ? p1_pc  : p1_op1;
  assign p1_b   = iq_b_imm[s1_idx] ? p1_imm : p1_op2;
  assign p1_res = iq_link[s1_idx]  ? (p1_pc + INST_BYTES) : p1_y;

  vortex_alu #(.XLEN(XLEN)) u_alu1 (
    .op (alu_op_e'(iq_alu_op[s1_idx])),
    .a  (p1_a),
    .b  (p1_b),
    .y  (p1_y)
  );

  logic p1_taken;
  vortex_branch_cmp #(.XLEN(XLEN)) u_branch_cmp1 (
    .f3    (iq_bf3[s1_idx]),
    .a     (p1_op1),
    .b     (p1_op2),
    .taken (p1_taken)
  );

  logic redirect1;
  assign redirect1 = issue1 && (iq_jalr[s1_idx] || (iq_branch[s1_idx] && !iq_spec[s1_idx]));

  // ---------------------------------------------------------------------------
  // Predicted-branch resolution (ADR 0008). At most one is in flight, so only
  // one port can resolve one in a cycle.
  // ---------------------------------------------------------------------------
  logic            res0, res1, resolve, res_taken;
  logic [XLEN-1:0] res_pc, res_imm, squash_pc;
  assign res0      = issue && (i_fu == FU_ALU) && iq_branch[s_idx] && iq_spec[s_idx];
  assign res1      = issue1 && iq_branch[s1_idx] && iq_spec[s1_idx];
  assign resolve   = res0 || res1;
  assign res_taken = res0 ? br_taken : p1_taken;
  assign res_pc    = res0 ? i_pc : p1_pc;
  assign res_imm   = res0 ? i_imm : p1_imm;
  assign squash    = resolve && (res_taken != (res0 ? iq_pred[s_idx] : iq_pred[s1_idx]));
  assign squash_pc = res_taken ? (res_pc + res_imm) : (res_pc + INST_BYTES);

  // Squash bookkeeping: what is older than the branch survives.
  logic [RW-1:0] br_age;
  assign br_age = br_rob - rob_head;

  // Memory-queue entries older than the branch (a prefix of the queue)
  logic [MW:0] mq_keep;
  always_comb begin
    logic [MW-1:0] idx;
    mq_keep = '0;
    for (int i = 0; i < MEMQ_DEPTH; i++) begin
      idx = mq_head + MW'(i);
      if (((MW+1)'(i) < mq_count) && ((mq_rob[idx] - rob_head) < br_age))
        mq_keep = mq_keep + (MW+1)'(1);
    end
  end

  // ---------------------------------------------------------------------------
  // Xmlacc FU (combinational)
  // ---------------------------------------------------------------------------
  logic [XLEN-1:0] xm_result;
  logic            xm_sat;
  vortex_xmlacc #(.XLEN(XLEN)) u_xmlacc (
    .op     (xmlacc_op_e'(iq_xm_op[s_idx])),
    .rs1    (op1),
    .rs2    (op2),
    .rs3    (op3),
    .result (xm_result),
    .sat    (xm_sat)
  );

  // Single-cycle writeback port: whichever of ALU / Xmlacc issued this cycle.
  logic            sc_fire;
  logic [XLEN-1:0] sc_data;
  logic            sc_sat;
  assign sc_fire = issue && ((i_fu == FU_ALU) || (i_fu == FU_XM));
  assign sc_data = (i_fu == FU_XM) ? xm_result : alu_res;
  assign sc_sat  = (i_fu == FU_XM) && (xmlacc_op_e'(iq_xm_op[s_idx]) == XM_RQS) && xm_sat;

  // ---------------------------------------------------------------------------
  // Multiplier and divider FUs: two instances of the shared RV64M unit, so a
  // multiply can run while a divide is in flight.
  // ---------------------------------------------------------------------------
  logic            mul_start, mul_done, div_start, div_done;
  logic [XLEN-1:0] mul_result, div_result;
  assign mul_start = issue && (i_fu == FU_MUL);
  assign div_start = issue && (i_fu == FU_DIV);

  vortex_muldiv #(.XLEN(XLEN)) u_mul (
    .clk     (clk),
    .rst_n   (rst_n),
    .start   (mul_start),
    .op      (md_op_e'(iq_md_op[s_idx])),
    .is_word (iq_md_word[s_idx]),
    .a       (op1),
    .b       (op2),
    .busy    (mul_busy),
    .done    (mul_done),
    .result  (mul_result)
  );

  vortex_muldiv #(.XLEN(XLEN)) u_div (
    .clk     (clk),
    .rst_n   (rst_n),
    .start   (div_start),
    .op      (md_op_e'(iq_md_op[s_idx])),
    .is_word (iq_md_word[s_idx]),
    .a       (op1),
    .b       (op2),
    .busy    (div_busy),
    .done    (div_done),
    .result  (div_result)
  );

  // Destination tags of the op each unit is running (latched at start)
  logic [PW-1:0] mul_dst, div_dst;
  logic          mul_has_rd, div_has_rd;
  logic [RW-1:0] mul_rob, div_rob;

  // ---------------------------------------------------------------------------
  // Memory queue head: loads when their address is known, stores at commit.
  // ---------------------------------------------------------------------------
  logic [XLEN-1:0] agu_addr;
  assign agu_addr = op1 + i_imm;

  logic mq_nonempty, ld_fire, st_fire;
  assign mq_nonempty = (mq_count != '0);
  assign ld_fire = !halted && mq_nonempty && !mq_store[mq_head] && mq_ready[mq_head];
  assign st_fire = !halted && mq_nonempty &&  mq_store[mq_head] && mq_ready[mq_head]
                && (rob_count != '0) && (mq_rob[mq_head] == rob_head);

  // ---------------------------------------------------------------------------
  // LSQ (ADR 0009): any load in the queue may execute once every older store in
  // the queue has its address. A load fully covered by the youngest overlapping
  // older store takes its bytes from that store; a partial overlap waits for the
  // store to commit. Stores still write only at commit. One load per cycle;
  // a committing store has the data port first.
  // ---------------------------------------------------------------------------
  logic [MEMQ_DEPTH*XLEN-1:0] q_addr, q_data;
  logic [MEMQ_DEPTH*2-1:0]    q_size;
  logic [MEMQ_DEPTH-1:0]      q_store, q_known;
  always_comb begin
    logic [MW-1:0] idx;
    for (int j = 0; j < MEMQ_DEPTH; j++) begin
      idx = mq_head + MW'(j);
      q_addr[j*XLEN +: XLEN] = mq_addr[idx];
      q_data[j*XLEN +: XLEN] = mq_data[idx];
      q_size[j*2 +: 2]       = mq_size[idx];
      q_store[j]             = ((MW+1)'(j) < mq_count) && mq_store[idx];
      q_known[j]             = mq_ready[idx];
    end
  end

  logic [MEMQ_DEPTH-1:0] f_unknown, f_hit, f_partial;
  logic [XLEN-1:0]       f_data [MEMQ_DEPTH];
  for (genvar gi = 0; gi < MEMQ_DEPTH; gi++) begin : g_fwd
    // The load at queue offset gi sees offsets 0..gi-1 as its older entries.
    logic [MEMQ_DEPTH-1:0] older;
    assign older = MEMQ_DEPTH'((1 << gi) - 1);
    vortex_lsq_fwd #(.XLEN(XLEN), .N(MEMQ_DEPTH)) u_fwd (
      .ld_addr  (q_addr[gi*XLEN +: XLEN]),
      .ld_size  (q_size[gi*2 +: 2]),
      .st_valid (q_store & older),
      .st_known (q_known),
      .st_addr  (q_addr),
      .st_size  (q_size),
      .st_data  (q_data),
      .unknown  (f_unknown[gi]),
      .hit      (f_hit[gi]),
      .partial  (f_partial[gi]),
      .data     (f_data[gi])
    );
  end

  // Oldest load that can execute now
  logic          lq_found;
  logic [MW-1:0] lq_off;
  always_comb begin
    logic [MW-1:0] idx;
    lq_found = 1'b0;
    lq_off   = '0;
    for (int i = MEMQ_DEPTH - 1; i >= 0; i--) begin
      idx = mq_head + MW'(i);
      if (((MW+1)'(i) < mq_count) && !mq_store[idx] && mq_ready[idx] && !mq_done[idx]
          && !f_unknown[i] && !f_partial[i] && (f_hit[i] || !st_fire)) begin
        lq_found = 1'b1;
        lq_off   = MW'(i);
      end
    end
  end

  // Load execution, either mode: index, whether it forwards, and queue pop.
  logic          lx_fire, lx_hit;
  logic [1:0]    mq_pop;     // entries leaving the memory queue head this cycle
  logic [MW-1:0] lx_idx;
  assign lx_fire = USE_LSQ ? (!halted && lq_found) : ld_fire;
  assign lx_idx  = USE_LSQ ? (mq_head + lq_off) : mq_head;
  assign lx_hit  = USE_LSQ && f_hit[lq_off];

  assign dmem_req   = st_fire || (lx_fire && !lx_hit);
  assign dmem_we    = st_fire;
  assign dmem_addr  = st_fire ? mq_addr[mq_head] : mq_addr[lx_idx];
  assign dmem_size  = st_fire ? mq_size[mq_head] : mq_size[lx_idx];
  assign dmem_wdata = mq_data[mq_head];

  logic [XLEN-1:0] ld_data;
  vortex_load_ext #(.XLEN(XLEN)) u_load_ext (
    .size        (mq_size[lx_idx]),
    .is_unsigned (mq_uns[lx_idx]),
    .raw         (lx_hit ? f_data[lq_off] : dmem_rdata),
    .data        (ld_data)
  );

  // ---------------------------------------------------------------------------
  // Commit
  // ---------------------------------------------------------------------------
  logic commit;
  assign commit = !halted && (rob_count != '0) && (rob_complete[rob_head] || st_fire);

  logic [XLEN-1:0] commit_data;
  assign commit_data = prf[rob_newp[rob_head]];

  // Commit slot 1: the next entry, if complete, unless slot 0 is the tohost store.
  // Stores never set rob_complete, so a store can only commit in slot 0.
  logic [RW-1:0]   rob_head1;
  logic            tohost_commit, commit1;
  logic [XLEN-1:0] commit_data1;
  assign rob_head1     = rob_head + RW'(1);
  assign tohost_commit = st_fire && (mq_addr[mq_head] == TOHOST_ADDR);
  assign commit1       = DUAL && commit && !tohost_commit && (rob_count >= (RW+1)'(2))
                      && rob_complete[rob_head1];
  assign commit_data1  = prf[rob_newp[rob_head1]];

  // LSQ: a load stays in the queue until it commits, and leaves then (it is the
  // queue head, or next after a store committing in slot 0). So every queue
  // entry has a live ROB entry, which the squash age compare relies on (ADR 0009).
  assign mq_pop  = USE_LSQ ? (2'(st_fire) + 2'(commit && rob_load[rob_head])
                                          + 2'(commit1 && rob_load[rob_head1]))
                           : 2'(st_fire || ld_fire);

  // Is any ROB entry older than the op completing now on the single-cycle port
  // (sc) or the multiplier port (mul) still incomplete? Feeds the debug counter.
  logic older_incomplete_sc, older_incomplete_mul, older_incomplete_p1;
  always_comb begin
    logic [RW-1:0] age_j, age_sc, age_mul, age_p1;
    older_incomplete_sc  = 1'b0;
    older_incomplete_mul = 1'b0;
    older_incomplete_p1  = 1'b0;
    age_sc  = iq_rob[s_idx] - rob_head;
    age_mul = mul_rob - rob_head;
    age_p1  = iq_rob[s1_idx] - rob_head;
    for (int j = 0; j < ROB_DEPTH; j++) begin
      age_j = RW'(j) - rob_head;
      if ((age_j < age_sc)  && !rob_complete[j]) older_incomplete_sc  = 1'b1;
      if ((age_j < age_mul) && !rob_complete[j]) older_incomplete_mul = 1'b1;
      if ((age_j < age_p1)  && !rob_complete[j]) older_incomplete_p1  = 1'b1;
    end
  end

  logic [1:0] ooo_events;
  assign ooo_events = 2'(sc_fire && older_incomplete_sc) + 2'(mul_done && older_incomplete_mul)
                    + 2'(issue1 && older_incomplete_p1);

  // ---------------------------------------------------------------------------
  // Sequential state
  // ---------------------------------------------------------------------------
  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      pc        <= RESET_PC;
      fe_wait   <= 1'b0;
      halted    <= 1'b0;
      for (int p = 0; p < NUM_PREGS; p++) begin
        prf[p]        <= '0;
        preg_ready[p] <= 1'b1;
        fl_q[p]       <= (p < NFREE0) ? PW'(NARCH + p) : '0;
      end
      for (int r = 0; r < NARCH; r++) begin
        spec_rat[r] <= PW'(r);
        arch_rat[r] <= PW'(r);
        ckpt_rat[r] <= PW'(r);
      end
      fl_head  <= '0;
      fl_tail  <= PW'(NFREE0);
      fl_count <= (PW+1)'(NFREE0);
      for (int e = 0; e < ROB_DEPTH; e++) begin
        rob_pc[e]       <= '0;
        rob_rd[e]       <= '0;
        rob_has_rd[e]   <= 1'b0;
        rob_newp[e]     <= '0;
        rob_oldp[e]     <= '0;
        rob_complete[e] <= 1'b0;
        rob_sat[e]      <= 1'b0;
        rob_illegal[e]  <= 1'b0;
        rob_load[e]     <= 1'b0;
      end
      rob_head  <= '0;
      rob_tail  <= '0;
      rob_count <= '0;
      for (int q = 0; q < IQ_DEPTH; q++) begin
        iq_valid[q]   <= 1'b0;
        iq_fu[q]      <= FU_ALU;
        iq_rob[q]     <= '0;
        iq_memq[q]    <= '0;
        iq_pc[q]      <= '0;
        iq_imm[q]     <= '0;
        iq_src1[q]    <= '0;
        iq_src2[q]    <= '0;
        iq_src3[q]    <= '0;
        iq_dst[q]     <= '0;
        iq_has_rd[q]  <= 1'b0;
        iq_alu_op[q]  <= ALU_ADD;
        iq_a_pc[q]    <= 1'b0;
        iq_b_imm[q]   <= 1'b0;
        iq_link[q]    <= 1'b0;
        iq_branch[q]  <= 1'b0;
        iq_jalr[q]    <= 1'b0;
        iq_spec[q]    <= 1'b0;
        iq_pred[q]    <= 1'b0;
        iq_bf3[q]     <= '0;
        iq_md_op[q]   <= '0;
        iq_md_word[q] <= 1'b0;
        iq_xm_op[q]   <= '0;
      end
      for (int m = 0; m < MEMQ_DEPTH; m++) begin
        mq_store[m]  <= 1'b0;
        mq_rob[m]    <= '0;
        mq_ready[m]  <= 1'b0;
        mq_addr[m]   <= '0;
        mq_data[m]   <= '0;
        mq_size[m]   <= '0;
        mq_uns[m]    <= 1'b0;
        mq_dst[m]    <= '0;
        mq_has_rd[m] <= 1'b0;
        mq_done[m]   <= 1'b0;
      end
      mq_head  <= '0;
      mq_tail  <= '0;
      mq_count <= '0;
      mul_dst    <= '0;
      mul_has_rd <= 1'b0;
      mul_rob    <= '0;
      div_dst    <= '0;
      div_has_rd <= 1'b0;
      div_rob    <= '0;
      dbg_retire_valid    <= 1'b0;
      dbg_retire_pc       <= '0;
      dbg_wb_valid        <= 1'b0;
      dbg_wb_rd           <= '0;
      dbg_wb_data         <= '0;
      dbg_mlstat_sat      <= 1'b0;
      dbg_ooo_completions <= '0;
      dbg_dual_commits    <= '0;
      dbg_spec_branches   <= '0;
      dbg_mispredicts     <= '0;
      dbg_lsq_forwards    <= '0;
      dbg_lsq_early       <= '0;
      spec_active         <= 1'b0;
      br_rob              <= '0;
      ckpt_fl_head        <= '0;
      mul_kill            <= 1'b0;
      div_kill            <= 1'b0;
      for (int b = 0; b < BHT_ENTRIES; b++) bht[b] <= 2'b01;   // weakly not-taken
      dbg1_retire_valid   <= 1'b0;
      dbg1_retire_pc      <= '0;
      dbg1_wb_valid       <= 1'b0;
      dbg1_wb_rd          <= '0;
      dbg1_wb_data        <= '0;
    end else begin
      // ---- Dispatch: rename, allocate ROB / IQ / memory-queue entries ----
      if (dispatch) begin
        if (is_jal || (d_spec0 && pred0)) begin
          pc <= pc + imm;
        end else if (!dispatch1) begin
          pc <= pc + INST_BYTES;
        end else if (is_jal1 || (d_spec1 && pred1)) begin
          pc <= imem_addr1 + imm1;
        end else begin
          pc <= imem_addr1 + INST_BYTES;
        end
        if (d_ctrl_wait || (dispatch1 && d_ctrl_wait1)) fe_wait <= 1'b1;

        if (d_has_rd) begin
          spec_rat[rd_addr]  <= d_newp;
          preg_ready[d_newp] <= 1'b0;
        end
        fl_head <= fl_head + PW'(d_has_rd) + PW'(dispatch1 && d_has_rd1);

        rob_pc[rob_tail]       <= pc;
        rob_rd[rob_tail]       <= rd_addr;
        rob_has_rd[rob_tail]   <= d_has_rd;
        rob_newp[rob_tail]     <= d_newp;
        rob_oldp[rob_tail]     <= d_oldp;
        rob_complete[rob_tail] <= 1'b0;
        rob_sat[rob_tail]      <= 1'b0;
        rob_illegal[rob_tail]  <= dec_illegal;
        rob_load[rob_tail]     <= is_load;
        rob_tail               <= rob_tail + RW'(1) + RW'(dispatch1);

        iq_valid[iq_free_idx]   <= 1'b1;
        iq_fu[iq_free_idx]      <= d_fu;
        iq_rob[iq_free_idx]     <= rob_tail;
        iq_memq[iq_free_idx]    <= mq_tail;
        iq_pc[iq_free_idx]      <= pc;
        iq_imm[iq_free_idx]     <= imm;
        iq_src1[iq_free_idx]    <= d_src1;
        iq_src2[iq_free_idx]    <= d_src2;
        iq_src3[iq_free_idx]    <= d_src3;
        iq_dst[iq_free_idx]     <= d_newp;
        iq_has_rd[iq_free_idx]  <= d_has_rd;
        iq_alu_op[iq_free_idx]  <= alu_op;
        iq_a_pc[iq_free_idx]    <= alu_a_pc;
        iq_b_imm[iq_free_idx]   <= alu_b_imm;
        iq_link[iq_free_idx]    <= (wb_src == WB_PC4);
        iq_branch[iq_free_idx]  <= is_branch;
        iq_jalr[iq_free_idx]    <= is_jalr;
        iq_spec[iq_free_idx]    <= d_spec0;
        iq_pred[iq_free_idx]    <= pred0;
        iq_bf3[iq_free_idx]     <= branch_f3;
        iq_md_op[iq_free_idx]   <= md_op;
        iq_md_word[iq_free_idx] <= md_word;
        iq_xm_op[iq_free_idx]   <= xm_op;

        if (d_is_mem) begin
          mq_store[mq_tail]  <= is_store;
          mq_rob[mq_tail]    <= rob_tail;
          mq_ready[mq_tail]  <= 1'b0;
          mq_done[mq_tail]   <= 1'b0;
          mq_size[mq_tail]   <= mem_size;
          mq_uns[mq_tail]    <= load_unsigned;
          mq_dst[mq_tail]    <= d_newp;
          mq_has_rd[mq_tail] <= d_has_rd;
        end
        mq_tail <= mq_tail + MW'(d_is_mem) + MW'(dispatch1 && d_is_mem1);
      end

      // ---- Dispatch slot 1 (younger; its RAT write wins over slot 0's) ----
      if (dispatch1) begin
        if (d_has_rd1) begin
          spec_rat[rd_addr1]  <= d_newp1;
          preg_ready[d_newp1] <= 1'b0;
        end

        rob_pc[rob_tail1]       <= imem_addr1;
        rob_rd[rob_tail1]       <= rd_addr1;
        rob_has_rd[rob_tail1]   <= d_has_rd1;
        rob_newp[rob_tail1]     <= d_newp1;
        rob_oldp[rob_tail1]     <= d_oldp1;
        rob_complete[rob_tail1] <= 1'b0;
        rob_sat[rob_tail1]      <= 1'b0;
        rob_illegal[rob_tail1]  <= dec_illegal1;
        rob_load[rob_tail1]     <= is_load1;

        iq_valid[iq_free_idx2]   <= 1'b1;
        iq_fu[iq_free_idx2]      <= d_fu1;
        iq_rob[iq_free_idx2]     <= rob_tail1;
        iq_memq[iq_free_idx2]    <= mq_tail1;
        iq_pc[iq_free_idx2]      <= imem_addr1;
        iq_imm[iq_free_idx2]     <= imm1;
        iq_src1[iq_free_idx2]    <= d_src1_1;
        iq_src2[iq_free_idx2]    <= d_src2_1;
        iq_src3[iq_free_idx2]    <= d_src3_1;
        iq_dst[iq_free_idx2]     <= d_newp1;
        iq_has_rd[iq_free_idx2]  <= d_has_rd1;
        iq_alu_op[iq_free_idx2]  <= alu_op1;
        iq_a_pc[iq_free_idx2]    <= alu_a_pc1;
        iq_b_imm[iq_free_idx2]   <= alu_b_imm1;
        iq_link[iq_free_idx2]    <= (wb_src1 == WB_PC4);
        iq_branch[iq_free_idx2]  <= is_branch1;
        iq_jalr[iq_free_idx2]    <= is_jalr1;
        iq_spec[iq_free_idx2]    <= d_spec1;
        iq_pred[iq_free_idx2]    <= pred1;
        iq_bf3[iq_free_idx2]     <= branch_f3_1;
        iq_md_op[iq_free_idx2]   <= md_op1;
        iq_md_word[iq_free_idx2] <= md_word1;
        iq_xm_op[iq_free_idx2]   <= xm_op1;

        if (d_is_mem1) begin
          mq_store[mq_tail1]  <= is_store1;
          mq_rob[mq_tail1]    <= rob_tail1;
          mq_ready[mq_tail1]  <= 1'b0;
          mq_done[mq_tail1]   <= 1'b0;
          mq_size[mq_tail1]   <= mem_size1;
          mq_uns[mq_tail1]    <= load_unsigned1;
          mq_dst[mq_tail1]    <= d_newp1;
          mq_has_rd[mq_tail1] <= d_has_rd1;
        end
      end

      // ---- Issue / execute ----
      if (issue) begin
        iq_valid[s_idx] <= 1'b0;
      end
      if (sc_fire) begin
        if (iq_has_rd[s_idx]) begin
          prf[iq_dst[s_idx]]        <= sc_data;
          preg_ready[iq_dst[s_idx]] <= 1'b1;
        end
        rob_complete[iq_rob[s_idx]] <= 1'b1;
        rob_sat[iq_rob[s_idx]]      <= sc_sat;
      end
      if (redirect) begin
        // Fall-through pc + 4 was set at dispatch; only a taken branch or JALR moves it.
        if (iq_jalr[s_idx]) begin
          pc <= jalr_target;
        end else if (br_taken) begin
          pc <= br_target;
        end
        fe_wait <= 1'b0;
      end
      if (issue1) begin
        iq_valid[s1_idx] <= 1'b0;
        if (iq_has_rd[s1_idx]) begin
          prf[iq_dst[s1_idx]]        <= p1_res;
          preg_ready[iq_dst[s1_idx]] <= 1'b1;
        end
        rob_complete[iq_rob[s1_idx]] <= 1'b1;
        rob_sat[iq_rob[s1_idx]]      <= 1'b0;
      end
      if (redirect1) begin
        // At most one branch/JALR is ever in flight, so redirect and redirect1
        // never fire together.
        if (iq_jalr[s1_idx]) begin
          pc <= (p1_op1 + p1_imm) & ~XLEN'(1);
        end else if (p1_taken) begin
          pc <= p1_pc + p1_imm;
        end
        fe_wait <= 1'b0;
      end
      if (issue && (i_fu == FU_AGU)) begin
        mq_ready[iq_memq[s_idx]] <= 1'b1;
        mq_addr[iq_memq[s_idx]]  <= agu_addr;
        mq_data[iq_memq[s_idx]]  <= op2;
      end
      if (mul_start) begin
        mul_dst    <= iq_dst[s_idx];
        mul_has_rd <= iq_has_rd[s_idx];
        mul_rob    <= iq_rob[s_idx];
      end
      if (div_start) begin
        div_dst    <= iq_dst[s_idx];
        div_has_rd <= iq_has_rd[s_idx];
        div_rob    <= iq_rob[s_idx];
      end

      // ---- Writeback from the multi-cycle units and the load port ----
      if (mul_done && !mul_kill) begin
        if (mul_has_rd) begin
          prf[mul_dst]        <= mul_result;
          preg_ready[mul_dst] <= 1'b1;
        end
        rob_complete[mul_rob] <= 1'b1;
      end
      if (div_done && !div_kill) begin
        if (div_has_rd) begin
          prf[div_dst]        <= div_result;
          preg_ready[div_dst] <= 1'b1;
        end
        rob_complete[div_rob] <= 1'b1;
      end
      if (lx_fire) begin
        if (mq_has_rd[lx_idx]) begin
          prf[mq_dst[lx_idx]]        <= ld_data;
          preg_ready[mq_dst[lx_idx]] <= 1'b1;
        end
        rob_complete[mq_rob[lx_idx]] <= 1'b1;
        mq_done[lx_idx]              <= 1'b1;
        if (lx_hit) dbg_lsq_forwards <= dbg_lsq_forwards + DBG_CNT_W'(1);
        if (lx_idx != mq_head) dbg_lsq_early <= dbg_lsq_early + DBG_CNT_W'(1);
      end
      mq_head <= mq_head + MW'(mq_pop);

      // ---- Commit ----
      if (commit) begin
        rob_head <= rob_head + RW'(1) + RW'(commit1);
        if (rob_has_rd[rob_head]) begin
          arch_rat[rob_rd[rob_head]] <= rob_newp[rob_head];
          fl_q[fl_tail]              <= rob_oldp[rob_head];
        end
        fl_tail <= fl_tail + PW'(rob_has_rd[rob_head]) + PW'(commit1 && rob_has_rd[rob_head1]);
        dbg_retire_valid <= 1'b1;
        dbg_retire_pc    <= rob_pc[rob_head];
        dbg_wb_valid     <= rob_has_rd[rob_head];
        dbg_wb_rd        <= rob_rd[rob_head];
        dbg_wb_data      <= rob_has_rd[rob_head] ? commit_data : '0;
        if (rob_sat[rob_head]) dbg_mlstat_sat <= 1'b1;
        if (tohost_commit) halted <= 1'b1;
      end else begin
        dbg_retire_valid <= 1'b0;
        dbg_wb_valid     <= 1'b0;
      end
      if (commit1) begin
        if (rob_has_rd[rob_head1]) begin
          arch_rat[rob_rd[rob_head1]]                  <= rob_newp[rob_head1];
          fl_q[fl_tail + PW'(rob_has_rd[rob_head])] <= rob_oldp[rob_head1];
        end
        dbg1_retire_valid <= 1'b1;
        dbg1_retire_pc    <= rob_pc[rob_head1];
        dbg1_wb_valid     <= rob_has_rd[rob_head1];
        dbg1_wb_rd        <= rob_rd[rob_head1];
        dbg1_wb_data      <= rob_has_rd[rob_head1] ? commit_data1 : '0;
        if (rob_sat[rob_head1]) dbg_mlstat_sat <= 1'b1;
        dbg_dual_commits  <= dbg_dual_commits + DBG_CNT_W'(1);
      end else begin
        dbg1_retire_valid <= 1'b0;
        dbg1_wb_valid     <= 1'b0;
      end

      dbg_ooo_completions <= dbg_ooo_completions + DBG_CNT_W'(ooo_events);

      // ---- Occupancy counters (allocate and release may coincide) ----
      rob_count <= rob_count + (RW+1)'(dispatch) + (RW+1)'(dispatch1)
                             - (RW+1)'(commit) - (RW+1)'(commit1);
      mq_count  <= mq_count + (MW+1)'(dispatch && d_is_mem) + (MW+1)'(dispatch1 && d_is_mem1)
                            - (MW+1)'(mq_pop);
      fl_count  <= fl_count - (PW+1)'(dispatch && d_has_rd) - (PW+1)'(dispatch1 && d_has_rd1)
                            + (PW+1)'(commit && rob_has_rd[rob_head])
                            + (PW+1)'(commit1 && rob_has_rd[rob_head1]);

      // ---- Predicted branches: checkpoint at dispatch, train and resolve ----
      if (dispatch && d_spec0) begin
        spec_active  <= 1'b1;
        br_rob       <= rob_tail;
        ckpt_fl_head <= fl_head;
        for (int r = 0; r < NARCH; r++) ckpt_rat[r] <= spec_rat[r];
      end
      if (dispatch1 && d_spec1) begin
        spec_active  <= 1'b1;
        br_rob       <= rob_tail1;
        ckpt_fl_head <= fl_head + PW'(d_has_rd);
        for (int r = 0; r < NARCH; r++)
          ckpt_rat[r] <= (d_has_rd && (AW'(r) == rd_addr)) ? d_newp : spec_rat[r];
      end
      if (resolve) begin
        spec_active       <= 1'b0;
        dbg_spec_branches <= dbg_spec_branches + DBG_CNT_W'(1);
        if (res_taken && (bht[res_pc[BW+1:2]] != 2'b11))
          bht[res_pc[BW+1:2]] <= bht[res_pc[BW+1:2]] + 2'b01;
        if (!res_taken && (bht[res_pc[BW+1:2]] != 2'b00))
          bht[res_pc[BW+1:2]] <= bht[res_pc[BW+1:2]] - 2'b01;
      end

      // Ops running in the multiplier/divider that the squash makes dead
      if (mul_start) begin
        mul_kill <= squash && ((iq_rob[s_idx] - rob_head) > br_age);
      end else if (squash && mul_busy && ((mul_rob - rob_head) > br_age)) begin
        mul_kill <= 1'b1;
      end else if (mul_done) begin
        mul_kill <= 1'b0;
      end
      if (div_start) begin
        div_kill <= squash && ((iq_rob[s_idx] - rob_head) > br_age);
      end else if (squash && div_busy && ((div_rob - rob_head) > br_age)) begin
        div_kill <= 1'b1;
      end else if (div_done) begin
        div_kill <= 1'b0;
      end

      // ---- Mispredict: squash everything younger than the branch ----
      // Dispatch is held off this cycle, so nothing younger is being allocated.
      // Writes landing this cycle in squashed pregs are harmless: those pregs go
      // back to the free list and are re-marked not-ready when reallocated.
      if (squash) begin
        dbg_mispredicts <= dbg_mispredicts + DBG_CNT_W'(1);
        pc      <= squash_pc;
        fe_wait <= 1'b0;   // any JALR waiting now is younger than the branch
        for (int r = 0; r < NARCH; r++) spec_rat[r] <= ckpt_rat[r];
        fl_head   <= ckpt_fl_head;
        fl_count  <= fl_count + (fl_head - ckpt_fl_head)
                              + (PW+1)'(commit && rob_has_rd[rob_head])
                              + (PW+1)'(commit1 && rob_has_rd[rob_head1]);
        rob_tail  <= br_rob + RW'(1);
        rob_count <= (RW+1)'(br_age) + (RW+1)'(1) - (RW+1)'(commit) - (RW+1)'(commit1);
        for (int q = 0; q < IQ_DEPTH; q++) begin
          if ((iq_rob[q] - rob_head) > br_age) iq_valid[q] <= 1'b0;
        end
        // Older entries are a prefix. If the head is younger it may be a load
        // firing now; then the queue empties behind it.
        if (mq_keep == '0) begin
          mq_tail  <= mq_head + MW'(mq_pop);
          mq_count <= '0;
        end else begin
          mq_tail  <= mq_head + MW'(mq_keep);
          mq_count <= mq_keep - (MW+1)'(mq_pop);
        end
      end
    end
  end

  // ---------------------------------------------------------------------------
  // Simulation-only checks and notes (ignored by synthesis)
  // ---------------------------------------------------------------------------
  // verilator lint_off BLKSEQ
  always @(posedge clk) begin
    if (commit && rob_illegal[rob_head]) begin
      $display("[vortex_ooo_core] WARNING: illegal/unsupported instruction at pc=%016h",
               rob_pc[rob_head]);
    end
    if (commit1 && rob_illegal[rob_head1]) begin
      $display("[vortex_ooo_core] WARNING: illegal/unsupported instruction at pc=%016h",
               rob_pc[rob_head1]);
    end
    // With nothing in flight the speculative and committed maps must agree.
    if (rob_count == '0) begin
      for (int r = 0; r < NARCH; r++) begin
        if (spec_rat[r] != arch_rat[r])
          $fatal(1, "[vortex_ooo_core] RAT mismatch at idle: x%0d spec=p%0d arch=p%0d",
                 r, spec_rat[r], arch_rat[r]);
      end
    end
  end
  // verilator lint_on BLKSEQ

endmodule : vortex_ooo_core
`endif
