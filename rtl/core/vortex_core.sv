// vortex_core.sv — Vortex-V M1 in-order RV64I core (top level).
//
// Microarchitecture: single-cycle datapath with a *registered commit/retire*
// interface. One instruction is fetched, decoded, executed, and committed per
// clock; the debug-retire ports (dbg_*) are registered so the cocotb co-sim sees
// a clean one-pulse-per-retired-instruction stream to compare against the ISS.
// Correctness-first baseline before the OoO engine (see CLAUDE.md prime
// directive #3 and docs/M1-INTERFACE-v1.md).
//
// Scope: RV64I base integer only (incl. the RV64 *W ops). No M/C/CSR extensions,
// no traps/interrupts beyond the tohost-store halt.
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

  // Debug / retire introspection (M1 co-sim oracle)
  output logic              dbg_retire_valid,
  output logic [XLEN-1:0]   dbg_retire_pc,
  output logic              dbg_wb_valid,
  output logic [4:0]        dbg_wb_rd,
  output logic [XLEN-1:0]   dbg_wb_data,

  // Xmlacc mlstat.SAT sticky (CSR 0x801 bit0). Full Zicsr CSR-instruction
  // plumbing is deferred past M3 (see header); the sticky SAT state is exposed
  // here so the co-sim can check requantize saturation against the ISS.
  output logic              dbg_mlstat_sat
);

  // ---------------------------------------------------------------------------
  // Architectural state
  // ---------------------------------------------------------------------------
  logic [XLEN-1:0] pc;
  logic            halted;
  logic            running;
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
  logic             dec_illegal;

  vortex_decoder #(.XLEN(XLEN)) u_decoder (
    .inst          (inst),
    .rs1_addr      (rs1_addr),
    .rs2_addr      (rs2_addr),
    .rs3_addr      (rs3_addr),
    .rd_addr       (rd_addr),
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
  assign md_start = running && is_muldiv && !md_busy && !md_done;

  // Stall the pipeline (hold pc, suppress retire/writeback) while a muldiv op is
  // in flight. The op retires on the single cycle `md_done` is asserted.
  logic md_stall;
  assign md_stall = running && is_muldiv && !md_done;

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
  // Branch comparator
  // ---------------------------------------------------------------------------
  logic cmp_eq, cmp_lt, cmp_ltu;
  assign cmp_eq  = (rs1_data == rs2_data);
  assign cmp_lt  = ($signed(rs1_data) < $signed(rs2_data));
  assign cmp_ltu = (rs1_data < rs2_data);

  logic branch_taken;
  always_comb begin
    unique case (branch_f3)
      F3_BEQ : branch_taken = cmp_eq;
      F3_BNE : branch_taken = ~cmp_eq;
      F3_BLT : branch_taken = cmp_lt;
      F3_BGE : branch_taken = ~cmp_lt;
      F3_BLTU: branch_taken = cmp_ltu;
      F3_BGEU: branch_taken = ~cmp_ltu;
      default: branch_taken = 1'b0;
    endcase
  end

  // ---------------------------------------------------------------------------
  // Next-PC selection
  // ---------------------------------------------------------------------------
  logic [XLEN-1:0] pc_plus4, jalr_target, next_pc;
  assign pc_plus4    = pc + 64'd4;
  assign jalr_target = (rs1_data + imm) & ~64'd1; // LSB cleared per spec

  always_comb begin
    if (is_jal) begin
      next_pc = pc + imm;
    end else if (is_jalr) begin
      next_pc = jalr_target;
    end else if (is_branch && branch_taken) begin
      next_pc = pc + imm;
    end else begin
      next_pc = pc_plus4;
    end
  end

  // ---------------------------------------------------------------------------
  // Data memory interface (combinational outputs for this cycle)
  // ---------------------------------------------------------------------------
  assign dmem_req   = running && (is_load || is_store);
  assign dmem_we    = running && is_store;
  assign dmem_addr  = alu_y;             // rs1 + imm
  assign dmem_size  = mem_size;
  assign dmem_wdata = rs2_data;          // right-justified; TB writes mem_size bytes

  // Load data sign/zero-extension
  logic [XLEN-1:0] load_ext;
  always_comb begin
    unique case (mem_size)
      SZ_BYTE  : load_ext = load_unsigned ? {{(XLEN-8){1'b0}},            dmem_rdata[7:0]}
                                          : {{(XLEN-8){dmem_rdata[7]}},   dmem_rdata[7:0]};
      SZ_HALF  : load_ext = load_unsigned ? {{(XLEN-16){1'b0}},          dmem_rdata[15:0]}
                                          : {{(XLEN-16){dmem_rdata[15]}},dmem_rdata[15:0]};
      SZ_WORD  : load_ext = load_unsigned ? {{(XLEN-32){1'b0}},          dmem_rdata[31:0]}
                                          : {{(XLEN-32){dmem_rdata[31]}},dmem_rdata[31:0]};
      SZ_DOUBLE: load_ext = dmem_rdata;
      default  : load_ext = dmem_rdata;
    endcase
  end

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
  end

  // Actual architectural register write enable (x0 handled in regfile).
  // Suppressed while a muldiv op is stalling; it fires on the op's done cycle.
  assign rf_we = running && reg_we && !md_stall;

  // tohost halt detection: a committed store to TOHOST_ADDR
  logic tohost_store;
  assign tohost_store = running && is_store && (dmem_addr == TOHOST_ADDR);

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
      dbg_mlstat_sat   <= 1'b0;
    end else if (halted) begin
      dbg_retire_valid <= 1'b0;
      dbg_wb_valid     <= 1'b0;
    end else if (md_stall) begin
      // A multi-cycle muldiv op is still running: hold pc, emit no retire pulse
      // this cycle. The op retires once on the cycle md_done is asserted (then
      // md_stall is low and the normal commit path below runs).
      dbg_retire_valid <= 1'b0;
      dbg_wb_valid     <= 1'b0;
    end else begin
      // commit the instruction currently decoded at pc
      pc               <= next_pc;
      dbg_retire_valid <= 1'b1;
      dbg_retire_pc    <= pc;
      dbg_wb_valid     <= reg_we && (rd_addr != 5'd0);
      dbg_wb_rd        <= rd_addr;
      dbg_wb_data      <= (reg_we && (rd_addr != 5'd0)) ? wb_data : '0;
      // mlstat.SAT sticky: set (never cleared here — W1C is a deferred CSR op)
      // when a retiring mlacc.rqs saturates.
      if (is_xmlacc && (xm_op == XM_RQS) && xm_sat) begin
        dbg_mlstat_sat <= 1'b1;
      end
      if (tohost_store) begin
        halted <= 1'b1;
      end
    end
  end

  // ---------------------------------------------------------------------------
  // Simulation-only sanity note (keeps dec_illegal "used"; ignored by synthesis)
  // ---------------------------------------------------------------------------
  // verilator lint_off BLKSEQ
  always @(posedge clk) begin
    if (running && dec_illegal) begin
      // RV64I scope only; illegal/unsupported encodings are not expected in M1.
      $display("[vortex_core] WARNING: illegal/unsupported instruction %08h at pc=%016h",
               inst, pc);
    end
  end
  // verilator lint_on BLKSEQ

endmodule : vortex_core
`endif
