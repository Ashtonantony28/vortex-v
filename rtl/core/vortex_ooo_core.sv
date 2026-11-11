// vortex_ooo_core.sv — Vortex-V M4 out-of-order RV64IM + Xmlacc core (top level).
//
// Implements ADR 0006. Same ports and retire contract as vortex_core
// (docs/M1-INTERFACE-v1.md), so every suite that checks the in-order core checks
// this one unchanged.
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
// No speculation: after a conditional branch or JALR, dispatch stops until it
// executes and redirects fetch. JAL redirects at decode. Nothing is ever squashed.
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
  parameter int unsigned      MEMQ_DEPTH  = 8    // in-order memory queue (power of 2)
) (
  input  logic              clk,
  input  logic              rst_n,

  // Instruction fetch
  output logic [XLEN-1:0]   imem_addr,
  input  logic [31:0]       imem_rdata,

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
  output logic              dbg_mlstat_sat
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

  // Parameter checks (ADR 0006 item 8). Evaluated at time zero in simulation and
  // by lint; a bad configuration stops the run before any instruction executes.
  initial begin
    if (NUM_PREGS < NARCH + ROB_DEPTH)
      $fatal(1, "vortex_ooo_core: NUM_PREGS (%0d) must be >= 32 + ROB_DEPTH (%0d)",
             NUM_PREGS, ROB_DEPTH);
    if ((1 << PW) != NUM_PREGS || (1 << RW) != ROB_DEPTH || (1 << MW) != MEMQ_DEPTH)
      $fatal(1, "vortex_ooo_core: NUM_PREGS, ROB_DEPTH and MEMQ_DEPTH must be powers of 2");
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
  logic [MW-1:0]   mq_head, mq_tail;
  logic [MW:0]     mq_count;

  // Debug: completions (single-cycle and multiplier ports) while an older ROB
  // entry was still incomplete. Nonzero proves out-of-order completion (ADR 0006 verification).
  logic [DBG_CNT_W-1:0] dbg_ooo_completions;

  // ---------------------------------------------------------------------------
  // Fetch + decode
  // ---------------------------------------------------------------------------
  logic [31:0] inst;
  assign imem_addr = pc;
  assign inst      = imem_rdata;

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

  // Functional-unit class
  fu_e d_fu;
  always_comb begin
    if (is_load || is_store) begin
      d_fu = FU_AGU;
    end else if (is_muldiv) begin
      if ((md_op == MD_MUL) || (md_op == MD_MULH) || (md_op == MD_MULHSU)
          || (md_op == MD_MULHU)) begin
        d_fu = FU_MUL;
      end else begin
        d_fu = FU_DIV;
      end
    end else if (is_xmlacc) begin
      d_fu = FU_XM;
    end else begin
      d_fu = FU_ALU;
    end
  end

  logic d_has_rd, d_is_mem, d_ctrl_wait;
  assign d_has_rd    = reg_we && (rd_addr != '0);
  assign d_is_mem    = is_load || is_store;
  assign d_ctrl_wait = is_branch || is_jalr;   // stop dispatch until it resolves

  // ---------------------------------------------------------------------------
  // Rename
  // ---------------------------------------------------------------------------
  logic [PW-1:0] d_src1, d_src2, d_src3, d_newp, d_oldp;
  assign d_src1 = use_rs1 ? spec_rat[rs1_addr] : '0;
  assign d_src2 = use_rs2 ? spec_rat[rs2_addr] : '0;
  assign d_src3 = use_rs3 ? spec_rat[rs3_addr] : '0;
  assign d_newp = d_has_rd ? fl_q[fl_head] : '0;
  assign d_oldp = spec_rat[rd_addr];

  // First free issue-queue slot
  logic          iq_has_free;
  logic [QW-1:0] iq_free_idx;
  always_comb begin
    iq_has_free = 1'b0;
    iq_free_idx = '0;
    for (int i = IQ_DEPTH - 1; i >= 0; i--) begin
      if (!iq_valid[i]) begin
        iq_has_free = 1'b1;
        iq_free_idx = QW'(i);
      end
    end
  end

  logic dispatch;
  assign dispatch = !halted && !fe_wait
                 && (rob_count != (RW+1)'(ROB_DEPTH))
                 && iq_has_free
                 && (!d_is_mem || (mq_count != (MW+1)'(MEMQ_DEPTH)))
                 && (!d_has_rd || (fl_count != '0));

  // ---------------------------------------------------------------------------
  // Issue select: oldest (by ROB age) valid entry whose sources are ready and
  // whose functional unit can accept it.
  // ---------------------------------------------------------------------------
  logic mul_busy, div_busy;

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
      if (iq_valid[i] && fu_ok && preg_ready[iq_src1[i]] && preg_ready[iq_src2[i]]
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
  assign redirect = issue && (i_fu == FU_ALU) && (iq_branch[s_idx] || iq_jalr[s_idx]);

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

  assign dmem_req   = ld_fire || st_fire;
  assign dmem_we    = st_fire;
  assign dmem_addr  = mq_addr[mq_head];
  assign dmem_size  = mq_size[mq_head];
  assign dmem_wdata = mq_data[mq_head];

  logic [XLEN-1:0] ld_data;
  vortex_load_ext #(.XLEN(XLEN)) u_load_ext (
    .size        (mq_size[mq_head]),
    .is_unsigned (mq_uns[mq_head]),
    .raw         (dmem_rdata),
    .data        (ld_data)
  );

  // ---------------------------------------------------------------------------
  // Commit
  // ---------------------------------------------------------------------------
  logic commit;
  assign commit = !halted && (rob_count != '0) && (rob_complete[rob_head] || st_fire);

  logic [XLEN-1:0] commit_data;
  assign commit_data = prf[rob_newp[rob_head]];

  // Is any ROB entry older than the op completing now on the single-cycle port
  // (sc) or the multiplier port (mul) still incomplete? Feeds the debug counter.
  logic older_incomplete_sc, older_incomplete_mul;
  always_comb begin
    logic [RW-1:0] age_j, age_sc, age_mul;
    older_incomplete_sc  = 1'b0;
    older_incomplete_mul = 1'b0;
    age_sc  = iq_rob[s_idx] - rob_head;
    age_mul = mul_rob - rob_head;
    for (int j = 0; j < ROB_DEPTH; j++) begin
      age_j = RW'(j) - rob_head;
      if ((age_j < age_sc)  && !rob_complete[j]) older_incomplete_sc  = 1'b1;
      if ((age_j < age_mul) && !rob_complete[j]) older_incomplete_mul = 1'b1;
    end
  end

  logic [1:0] ooo_events;
  assign ooo_events = 2'(sc_fire && older_incomplete_sc) + 2'(mul_done && older_incomplete_mul);

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
    end else begin
      // ---- Dispatch: rename, allocate ROB / IQ / memory-queue entries ----
      if (dispatch) begin
        if (is_jal) begin
          pc <= pc + imm;
        end else begin
          pc <= pc + INST_BYTES;
        end
        if (d_ctrl_wait) fe_wait <= 1'b1;

        if (d_has_rd) begin
          spec_rat[rd_addr]  <= d_newp;
          preg_ready[d_newp] <= 1'b0;
          fl_head            <= fl_head + PW'(1);
        end

        rob_pc[rob_tail]       <= pc;
        rob_rd[rob_tail]       <= rd_addr;
        rob_has_rd[rob_tail]   <= d_has_rd;
        rob_newp[rob_tail]     <= d_newp;
        rob_oldp[rob_tail]     <= d_oldp;
        rob_complete[rob_tail] <= 1'b0;
        rob_sat[rob_tail]      <= 1'b0;
        rob_illegal[rob_tail]  <= dec_illegal;
        rob_tail               <= rob_tail + RW'(1);

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
        iq_bf3[iq_free_idx]     <= branch_f3;
        iq_md_op[iq_free_idx]   <= md_op;
        iq_md_word[iq_free_idx] <= md_word;
        iq_xm_op[iq_free_idx]   <= xm_op;

        if (d_is_mem) begin
          mq_store[mq_tail]  <= is_store;
          mq_rob[mq_tail]    <= rob_tail;
          mq_ready[mq_tail]  <= 1'b0;
          mq_size[mq_tail]   <= mem_size;
          mq_uns[mq_tail]    <= load_unsigned;
          mq_dst[mq_tail]    <= d_newp;
          mq_has_rd[mq_tail] <= d_has_rd;
          mq_tail            <= mq_tail + MW'(1);
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
      if (mul_done) begin
        if (mul_has_rd) begin
          prf[mul_dst]        <= mul_result;
          preg_ready[mul_dst] <= 1'b1;
        end
        rob_complete[mul_rob] <= 1'b1;
      end
      if (div_done) begin
        if (div_has_rd) begin
          prf[div_dst]        <= div_result;
          preg_ready[div_dst] <= 1'b1;
        end
        rob_complete[div_rob] <= 1'b1;
      end
      if (ld_fire) begin
        if (mq_has_rd[mq_head]) begin
          prf[mq_dst[mq_head]]        <= ld_data;
          preg_ready[mq_dst[mq_head]] <= 1'b1;
        end
        rob_complete[mq_rob[mq_head]] <= 1'b1;
      end
      if (ld_fire || st_fire) begin
        mq_head <= mq_head + MW'(1);
      end

      // ---- Commit ----
      if (commit) begin
        rob_head <= rob_head + RW'(1);
        if (rob_has_rd[rob_head]) begin
          arch_rat[rob_rd[rob_head]] <= rob_newp[rob_head];
          fl_q[fl_tail]              <= rob_oldp[rob_head];
          fl_tail                    <= fl_tail + PW'(1);
        end
        dbg_retire_valid <= 1'b1;
        dbg_retire_pc    <= rob_pc[rob_head];
        dbg_wb_valid     <= rob_has_rd[rob_head];
        dbg_wb_rd        <= rob_rd[rob_head];
        dbg_wb_data      <= rob_has_rd[rob_head] ? commit_data : '0;
        if (rob_sat[rob_head]) dbg_mlstat_sat <= 1'b1;
        if (st_fire && (mq_addr[mq_head] == TOHOST_ADDR)) halted <= 1'b1;
      end else begin
        dbg_retire_valid <= 1'b0;
        dbg_wb_valid     <= 1'b0;
      end

      dbg_ooo_completions <= dbg_ooo_completions + DBG_CNT_W'(ooo_events);

      // ---- Occupancy counters (allocate and release may coincide) ----
      rob_count <= rob_count + (RW+1)'(dispatch) - (RW+1)'(commit);
      mq_count  <= mq_count + (MW+1)'(dispatch && d_is_mem) - (MW+1)'(ld_fire || st_fire);
      fl_count  <= fl_count - (PW+1)'(dispatch && d_has_rd)
                            + (PW+1)'(commit && rob_has_rd[rob_head]);
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
