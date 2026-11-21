// vortex_core.sv — Vortex-V M1 in-order RV64I core (top level).
//
// Microarchitecture: single-cycle datapath with a *registered commit/retire*
// interface. One instruction is fetched, decoded, executed, and committed per
// clock; the debug-retire ports (dbg_*) are registered so the cocotb co-sim sees
// a clean one-pulse-per-retired-instruction stream to compare against the ISS.
// Correctness-first baseline before the OoO engine (see CLAUDE.md prime
// directive #3 and docs/M1-INTERFACE-v1.md).
//
// Scope: RV64IM + Xmlacc, Zicsr with the machine-mode CSRs, and ecall/ebreak/
// illegal-instruction traps and mret (ADR 0014). No C extension, no interrupts.
//
// Memory contract (per M1-INTERFACE-v1.md):
//   - imem: combinational read, 32-bit aligned instruction at imem_addr.
//   - dmem: single port. For a load, the TB returns `dmem_size` bytes starting at
//     dmem_addr, little-endian, right-justified into dmem_rdata[63:0]; the core
//     sign/zero-extends per the load's funct3. For a store, the core right-
//     justifies the data in dmem_wdata and the TB writes `dmem_size` bytes.
//   - A store to TOHOST_ADDR ends the run (TB watches it; core also latches halt).
`ifndef VORTEX_CORE_SV
`define VORTEX_CORE_SV
module vortex_core
  import vortex_pkg::*;
#(
  parameter int unsigned      XLEN       = 64,
  parameter logic [XLEN-1:0]  RESET_PC   = 64'h8000_0000,
  parameter logic [XLEN-1:0]  TOHOST_ADDR = 64'h8000_1000
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
  // Machine timer interrupt level from the SoC (mtime >= mtimecmp; ADR 0015)
  input  logic              irq_timer,

  // Debug / retire introspection (M1 co-sim oracle)
  output logic              dbg_retire_valid,
  output logic [XLEN-1:0]   dbg_retire_pc,
  output logic              dbg_wb_valid,
  output logic [4:0]        dbg_wb_rd,
  output logic [XLEN-1:0]   dbg_wb_data,

  // Xmlacc mlstat.SAT sticky (CSR 0x801 bit 0), so the co-sim can check
  // requantize saturation against the ISS at the end of a run.
  output logic              dbg_mlstat_sat,
  // One-cycle pulse: an interrupt was taken (before the instruction at mepc)
  output logic              dbg_irq
);

  // ---------------------------------------------------------------------------
  // Architectural state
  // ---------------------------------------------------------------------------
  logic [XLEN-1:0] pc;
  logic            halted;
  logic            running;
  // An interrupt is taken at an instruction boundary instead of executing the
  // instruction at pc; never in the middle of a multi-cycle muldiv (ADR 0015).
  logic            irq_pending, take_irq;
  assign running = !halted;

  // ---------------------------------------------------------------------------
  // Fetch
  // ---------------------------------------------------------------------------
  logic [31:0] inst;
  assign imem_addr = pc;
  assign inst      = imem_rdata;

  // ---------------------------------------------------------------------------
  // Decode
  // ---------------------------------------------------------------------------
  logic [4:0]       rs1_addr, rs2_addr, rs3_addr, rd_addr;
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
  logic             is_sys;
  sys_op_e          sys_op;
  logic             dec_illegal;
  // Source-use flags drive renaming in vortex_ooo_core; the in-order core reads
  // the register file unconditionally, so it leaves them unused.
  /* verilator lint_off UNUSEDSIGNAL */
  logic             use_rs1, use_rs2, use_rs3;
  /* verilator lint_on UNUSEDSIGNAL */

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
    .is_sys        (is_sys),
    .sys_op        (sys_op),
    .illegal       (dec_illegal)
  );

  // ---------------------------------------------------------------------------
  // Register file
  // ---------------------------------------------------------------------------
  logic [XLEN-1:0] rs1_data, rs2_data, rs3_data;
  logic [XLEN-1:0] wb_data;
  logic            rf_we;

  vortex_regfile #(.XLEN(XLEN), .NREGS(32)) u_regfile (
    .clk      (clk),
    .rst_n    (rst_n),
    .rs1_addr (rs1_addr),
    .rs1_data (rs1_data),
    .rs2_addr (rs2_addr),
    .rs2_data (rs2_data),
    .rs3_addr (rs3_addr),
    .rs3_data (rs3_data),
    .we       (rf_we),
    .rd_addr  (rd_addr),
    .rd_data  (wb_data)
  );

  // ---------------------------------------------------------------------------
  // Xmlacc custom ML execution unit (combinational, single-cycle). macc reads
  // the 3rd register port (rs3_data); the other ops ignore it.
  // ---------------------------------------------------------------------------
  logic [XLEN-1:0] xm_result;
  logic            xm_sat;

  vortex_xmlacc #(.XLEN(XLEN)) u_xmlacc (
    .op     (xm_op),
    .rs1    (rs1_data),
    .rs2    (rs2_data),
    .rs3    (rs3_data),
    .result (xm_result),
    .sat    (xm_sat)
  );

  // ---------------------------------------------------------------------------
  // ALU
  // ---------------------------------------------------------------------------
  logic [XLEN-1:0] alu_a, alu_b, alu_y;
  assign alu_a = alu_a_pc  ? pc  : rs1_data;
  assign alu_b = alu_b_imm ? imm : rs2_data;

  vortex_alu #(.XLEN(XLEN)) u_alu (
    .op (alu_op),
    .a  (alu_a),
    .b  (alu_b),
    .y  (alu_y)
  );

  // ---------------------------------------------------------------------------
  // RV64M multiply/divide unit (multi-cycle divider; stalls retire until done)
  // ---------------------------------------------------------------------------
  logic            md_start, md_busy, md_done;
  logic [XLEN-1:0] md_result;

  // Start a new op the first cycle a muldiv instruction is present and the unit
  // is free; while it runs (or on its done cycle) `start` stays low.
  assign take_irq = running && irq_pending && !(is_muldiv && (md_busy || md_done));
  assign md_start = running && is_muldiv && !md_busy && !md_done && !take_irq;

  // Stall the pipeline (hold pc, suppress retire/writeback) while a muldiv op is
  // in flight. The op retires on the single cycle `md_done` is asserted.
  logic md_stall;
  assign md_stall = running && is_muldiv && !md_done && !take_irq;

  vortex_muldiv #(.XLEN(XLEN)) u_muldiv (
    .clk     (clk),
    .rst_n   (rst_n),
    .start   (md_start),
    .op      (md_op),
    .is_word (md_word),
    .a       (rs1_data),
    .b       (rs2_data),
    .busy    (md_busy),
    .done    (md_done),
    .result  (md_result)
  );

  // ---------------------------------------------------------------------------
  // Branch comparator (shared with vortex_ooo_core)
  // ---------------------------------------------------------------------------
  logic branch_taken;
  vortex_branch_cmp #(.XLEN(XLEN)) u_branch_cmp (
    .f3    (branch_f3),
    .a     (rs1_data),
    .b     (rs2_data),
    .taken (branch_taken)
  );

  // ---------------------------------------------------------------------------
  // CSR file, traps and MRET (ADR 0014). A single-cycle core runs a SYSTEM or
  // illegal instruction like any other: every older one has retired.
  // ---------------------------------------------------------------------------
  logic            retiring, csr_exec, csr_redirect, csr_no_count, sat_set;
  logic [XLEN-1:0] csr_rdata, csr_target, trap_vector;
  assign retiring = running && !md_stall && !take_irq;

  // Misaligned faults (ADR 0016): a load/store address that is not naturally
  // aligned, or a taken jump/branch target that is not 4-byte aligned. The
  // instruction does nothing (no memory access, no register write, not counted)
  // and traps with mtval = the address or target.
  logic [XLEN-1:0] br_target, jalr_target, fault_cause, fault_tval;
  logic            mem_mis, jmp_mis, fault;
  assign br_target   = pc + imm;                  // JAL and taken branches
  assign jalr_target = (rs1_data + imm) & ~XLEN'(1);   // LSB cleared per spec
  assign mem_mis     = (is_load || is_store)
                    && ((alu_y[2:0] & ((3'd1 << mem_size) - 3'd1)) != 3'd0);
  assign jmp_mis     = ((is_jal || (is_branch && branch_taken)) && br_target[1])
                    || (is_jalr && jalr_target[1]);
  assign fault       = retiring && (mem_mis || jmp_mis);
  assign fault_cause = jmp_mis ? CAUSE_MISALIGNED_FETCH
                     : is_load ? CAUSE_MISALIGNED_LOAD : CAUSE_MISALIGNED_STORE;
  assign fault_tval  = jmp_mis ? (is_jalr ? jalr_target : br_target) : alu_y;
  assign csr_exec = retiring && (is_sys || dec_illegal);
  assign sat_set  = retiring && is_xmlacc && (xm_op == XM_RQS) && xm_sat;

  vortex_csr #(.XLEN(XLEN)) u_csr (
    .clk        (clk),
    .rst_n      (rst_n),
    .exec       (csr_exec),
    .illegal    (dec_illegal),
    .op         (sys_op),
    .inst       (inst),
    .rs1_val    (rs1_data),
    .pc         (pc),
    .rdata      (csr_rdata),
    .redirect   (csr_redirect),
    .target     (csr_target),
    .no_count   (csr_no_count),
    .retire_cnt (2'(retiring && !csr_no_count && !fault)),
    .sat_set    (sat_set),
    .mlstat_sat (dbg_mlstat_sat),
    .irq_timer  (irq_timer),
    .irq_pending(irq_pending),
    .irq_take   (take_irq),
    .irq_pc     (pc),
    .trap_vector(trap_vector),
    .fault      (fault),
    .fault_cause(fault_cause),
    .fault_tval (fault_tval),
    .fault_pc   (pc)
  );

  // ---------------------------------------------------------------------------
  // Next-PC selection
  // ---------------------------------------------------------------------------
  logic [XLEN-1:0] pc_plus4, next_pc;
  assign pc_plus4    = pc + 64'd4;

  always_comb begin
    if (take_irq || fault) begin
      next_pc = trap_vector;                      // interrupt or fault -> mtvec
    end else if (csr_redirect) begin
      next_pc = csr_target;                       // trap -> mtvec, mret -> mepc
    end else if (is_jal) begin
      next_pc = br_target;
    end else if (is_jalr) begin
      next_pc = jalr_target;
    end else if (is_branch && branch_taken) begin
      next_pc = br_target;
    end else begin
      next_pc = pc_plus4;
    end
  end

  // ---------------------------------------------------------------------------
  // Data memory interface (combinational outputs for this cycle)
  // ---------------------------------------------------------------------------
  assign dmem_req   = running && !take_irq && !mem_mis && (is_load || is_store);
  assign dmem_we    = running && !take_irq && !mem_mis && is_store;
  assign dmem_addr  = alu_y;             // rs1 + imm
  assign dmem_size  = mem_size;
  assign dmem_wdata = rs2_data;          // right-justified; TB writes mem_size bytes

  // Load data sign/zero-extension (shared with vortex_ooo_core)
  logic [XLEN-1:0] load_ext;
  vortex_load_ext #(.XLEN(XLEN)) u_load_ext (
    .size        (mem_size),
    .is_unsigned (load_unsigned),
    .raw         (dmem_rdata),
    .data        (load_ext)
  );

  // ---------------------------------------------------------------------------
  // Writeback mux
  // ---------------------------------------------------------------------------
  always_comb begin
    unique case (wb_src)
      WB_ALU   : wb_data = alu_y;
      WB_MEM   : wb_data = load_ext;
      WB_PC4   : wb_data = pc_plus4;
      WB_MULDIV: wb_data = md_result;
      WB_XMLACC: wb_data = xm_result;
      default  : wb_data = alu_y;
    endcase
    if (is_sys) wb_data = csr_rdata;            // CSR ops: the old CSR value
  end

  // Actual architectural register write enable (x0 handled in regfile).
  // Suppressed while a muldiv op is stalling (it fires on the op's done cycle)
  // and when an interrupt is taken instead.
  assign rf_we = retiring && reg_we && !fault;

  // tohost halt detection: a committed store to TOHOST_ADDR
  logic tohost_store;
  assign tohost_store = dmem_we && (dmem_addr == TOHOST_ADDR);

  // ---------------------------------------------------------------------------
  // Commit / retire (registered)
  // ---------------------------------------------------------------------------
  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      pc               <= RESET_PC;
      halted           <= 1'b0;
      dbg_retire_valid <= 1'b0;
      dbg_retire_pc    <= '0;
      dbg_wb_valid     <= 1'b0;
      dbg_wb_rd        <= '0;
      dbg_wb_data      <= '0;
      dbg_irq          <= 1'b0;
    end else if (halted) begin
      dbg_retire_valid <= 1'b0;
      dbg_wb_valid     <= 1'b0;
      dbg_irq          <= 1'b0;
    end else if (take_irq) begin
      // Interrupt: nothing retires; continue at mtvec (ADR 0015).
      pc               <= next_pc;
      dbg_retire_valid <= 1'b0;
      dbg_wb_valid     <= 1'b0;
      dbg_irq          <= 1'b1;
    end else if (md_stall) begin
      // A multi-cycle muldiv op is still running: hold pc, emit no retire pulse
      // this cycle. The op retires once on the cycle md_done is asserted (then
      // md_stall is low and the normal commit path below runs).
      dbg_retire_valid <= 1'b0;
      dbg_wb_valid     <= 1'b0;
      dbg_irq          <= 1'b0;
    end else begin
      // commit the instruction currently decoded at pc
      pc               <= next_pc;
      dbg_retire_valid <= 1'b1;
      dbg_retire_pc    <= pc;
      dbg_wb_valid     <= rf_we && (rd_addr != 5'd0);
      dbg_wb_rd        <= rd_addr;
      dbg_wb_data      <= (rf_we && (rd_addr != 5'd0)) ? wb_data : '0;
      dbg_irq          <= 1'b0;
      if (tohost_store) begin
        halted <= 1'b1;
      end
    end
  end

endmodule : vortex_core
`endif
