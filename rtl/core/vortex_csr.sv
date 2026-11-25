// vortex_csr.sv — machine-mode CSR file, traps and MRET (ADR 0014).
//
// Shared by vortex_core and vortex_ooo_core. The core asserts `exec` for one
// cycle when a SYSTEM, FENCE.I or illegal instruction executes; the core
// guarantees that every older instruction has retired and no younger one has
// executed (the out-of-order core runs it only at the ROB head). In that cycle
// this module returns the old CSR value (`rdata`, the rd result), applies the
// write, takes a trap or MRET, and gives the next pc for those (`redirect`,
// `target`).
//
// Counters: mcycle counts every cycle; minstret counts `retire_cnt`, which the
// core gives without the trapping or minstret-writing instruction. A write to
// mcycle/minstret replaces that cycle's increment.
//
// mlstat (Xmlacc, 0x801): bit 0 is the SAT sticky. `sat_set` sets it when a
// saturating mlacc.rqs retires; CSRRW and CSRRS clear the bits set in the
// source operand, CSRRC does nothing (XMLACC-SPEC-v1).
//
// Timer interrupt (ADR 0015): `irq_timer` is the SoC's level (mtime >= mtimecmp),
// read back as mip.MTIP. `irq_pending` = MTIP && mie.MTIE && mstatus.MIE. The
// core decides when to take it (`irq_take`, at an instruction boundary with no
// instruction executing) and gives the pc of the next instruction (`irq_pc`);
// it then fetches from `trap_vector`.
//
// Faults (ADR 0016): a misaligned jump target, load or store is found by the
// core, not decoded here. The core asserts `fault` for one cycle with the cause,
// mtval and the faulting pc, and fetches from `trap_vector` next.
//
// External interrupt (ADR 0017): `irq_ext` is read back as mip.MEIP and enabled
// by mie.MEIE; when both are pending it is taken before the timer.
//
// Reset is asynchronous, like the cores that instantiate it.
`ifndef VORTEX_CSR_SV
`define VORTEX_CSR_SV

module vortex_csr
  import vortex_pkg::*;
#(
  parameter int unsigned XLEN = 64
) (
  input  logic            clk,
  input  logic            rst_n,
  // The executing SYSTEM / FENCE.I / illegal instruction
  input  logic            exec,
  input  logic            illegal,
  input  sys_op_e         op,
  input  logic [31:0]     inst,        // CSR address, funct3, zimm, and mtval for illegal
  input  logic [XLEN-1:0] rs1_val,
  input  logic [XLEN-1:0] pc,
  output logic [XLEN-1:0] rdata,       // old value of the CSR (CSR ops)
  output logic            redirect,    // a trap or MRET: next pc is `target`
  output logic [XLEN-1:0] target,
  output logic            no_count,    // this instruction must not count in minstret
  // Retirement and Xmlacc status
  input  logic [1:0]      retire_cnt,
  input  logic            sat_set,
  output logic            mlstat_sat,
  // Timer interrupt (ADR 0015)
  input  logic            irq_timer,
  input  logic            irq_ext,
  output logic            irq_pending,
  input  logic            irq_take,
  input  logic [XLEN-1:0] irq_pc,
  output logic [XLEN-1:0] trap_vector,
  // Synchronous fault found by the core (ADR 0016)
  input  logic            fault,
  input  logic [XLEN-1:0] fault_cause,
  input  logic [XLEN-1:0] fault_tval,
  input  logic [XLEN-1:0] fault_pc
);
  localparam logic [XLEN-1:0] ALIGN_MASK = ~XLEN'(3);   // mtvec/mepc: IALIGN = 32
  localparam logic [XLEN-1:0] MSTATUS_WMASK = (XLEN'(1) << MSTATUS_MIE_BIT)
                                            | (XLEN'(1) << MSTATUS_MPIE_BIT);

  logic [XLEN-1:0] mstatus_r, mtvec_r, mscratch_r, mepc_r, mcause_r, mtval_r;
  logic [XLEN-1:0] mcycle_r, minstret_r;
  logic            mlstat_r;
  logic            mtie_r, meie_r;
  logic            ext_en;
  assign ext_en = irq_ext && meie_r;

  // ---- Decode the access ----
  logic [11:0]     csr;
  logic [1:0]      csr_op;
  logic [XLEN-1:0] src;
  logic            is_csr, writes, trap, mret;
  assign csr    = inst[31:20];
  assign csr_op = inst[13:12];
  assign src    = inst[14] ? XLEN'(inst[19:15]) : rs1_val;   // zimm or rs1
  assign is_csr = exec && !illegal && (op == SYS_CSR);
  assign writes = is_csr && ((csr_op == CSR_OP_RW) || (inst[19:15] != 5'd0));
  assign trap   = exec && (illegal || (op == SYS_ECALL) || (op == SYS_EBREAK));
  assign mret   = exec && !illegal && (op == SYS_MRET);

  // ---- Read ----
  always_comb begin
    unique case (csr)
      CSR_MSTATUS                 : rdata = mstatus_r;
      CSR_MISA                    : rdata = XLEN'(MISA_VALUE);
      CSR_MTVEC                   : rdata = mtvec_r;
      CSR_MSCRATCH                : rdata = mscratch_r;
      CSR_MEPC                    : rdata = mepc_r;
      CSR_MCAUSE                  : rdata = mcause_r;
      CSR_MTVAL                   : rdata = mtval_r;
      CSR_MCYCLE, CSR_CYCLE       : rdata = mcycle_r;
      CSR_MINSTRET, CSR_INSTRET   : rdata = minstret_r;
      CSR_MLSTAT                  : rdata = XLEN'(mlstat_r);
      CSR_MIE                     : rdata = (XLEN'(mtie_r)    << IRQ_M_TIMER)
                                          | (XLEN'(meie_r)    << IRQ_M_EXT);
      CSR_MIP                     : rdata = (XLEN'(irq_timer) << IRQ_M_TIMER)
                                          | (XLEN'(irq_ext)   << IRQ_M_EXT);
      default                     : rdata = '0;   // mlcfg, ID registers
    endcase
  end

  // ---- Write value (before the per-CSR field masks) ----
  logic [XLEN-1:0] wval;
  always_comb begin
    unique case (csr_op)
      CSR_OP_RS: wval = rdata | src;
      CSR_OP_RC: wval = rdata & ~src;
      default  : wval = src;                     // CSR_OP_RW
    endcase
  end

  logic wr_mcycle, wr_minstret;
  assign wr_mcycle   = writes && (csr == CSR_MCYCLE);
  assign wr_minstret = writes && (csr == CSR_MINSTRET);

  // ---- Redirect ----
  assign redirect = trap || mret;
  assign target   = trap ? mtvec_r : mepc_r;
  assign no_count = trap || wr_minstret;

  assign irq_pending = ((irq_timer && mtie_r) || ext_en) && mstatus_r[MSTATUS_MIE_BIT];
  assign trap_vector = mtvec_r;

  // ---- State ----
  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      mstatus_r  <= XLEN'(MSTATUS_MPP_M);
      mtvec_r    <= '0;
      mscratch_r <= '0;
      mepc_r     <= '0;
      mcause_r   <= '0;
      mtval_r    <= '0;
      mcycle_r   <= '0;
      minstret_r <= '0;
      mlstat_r   <= 1'b0;
      mtie_r     <= 1'b0;
      meie_r     <= 1'b0;
    end else begin
      mcycle_r   <= wr_mcycle   ? wval : (mcycle_r + XLEN'(1));
      minstret_r <= wr_minstret ? wval : (minstret_r + XLEN'(retire_cnt));

      if (writes) begin
        unique case (csr)
          CSR_MSTATUS : mstatus_r  <= (wval & MSTATUS_WMASK) | XLEN'(MSTATUS_MPP_M);
          CSR_MTVEC   : mtvec_r    <= wval & ALIGN_MASK;
          CSR_MSCRATCH: mscratch_r <= wval;
          CSR_MEPC    : mepc_r     <= wval & ALIGN_MASK;
          CSR_MCAUSE  : mcause_r   <= wval;
          CSR_MTVAL   : mtval_r    <= wval;
          CSR_MIE     : begin
            mtie_r <= wval[IRQ_M_TIMER];
            meie_r <= wval[IRQ_M_EXT];
          end
          default     : ;                     // misa, mip, mlcfg, counters, mlstat
        endcase
      end

      // mlstat: W1C on CSRRW/CSRRS, then a retiring saturating rqs sets it.
      if (writes && (csr == CSR_MLSTAT) && (csr_op != CSR_OP_RC))
        mlstat_r <= (mlstat_r && !src[0]) || sat_set;
      else if (sat_set)
        mlstat_r <= 1'b1;

      if (trap) begin
        mepc_r    <= pc & ALIGN_MASK;
        mcause_r  <= illegal ? XLEN'(CAUSE_ILLEGAL)
                   : (op == SYS_EBREAK) ? XLEN'(CAUSE_BREAKPOINT) : XLEN'(CAUSE_ECALL_M);
        mtval_r   <= illegal ? XLEN'(inst) : '0;
        mstatus_r <= XLEN'(MSTATUS_MPP_M)
                   | (XLEN'(mstatus_r[MSTATUS_MIE_BIT]) << MSTATUS_MPIE_BIT);
      end
      // A fault replaces the faulting instruction; nothing else executes with it.
      if (fault) begin
        mepc_r    <= fault_pc & ALIGN_MASK;
        mcause_r  <= fault_cause;
        mtval_r   <= fault_tval;
        mstatus_r <= XLEN'(MSTATUS_MPP_M)
                   | (XLEN'(mstatus_r[MSTATUS_MIE_BIT]) << MSTATUS_MPIE_BIT);
      end
      // The core never takes an interrupt in a cycle where an instruction executes.
      if (irq_take) begin
        mepc_r    <= irq_pc & ALIGN_MASK;
        mcause_r  <= ext_en ? XLEN'(CAUSE_M_EXT_IRQ) : XLEN'(CAUSE_M_TIMER_IRQ);
        mtval_r   <= '0;
        mstatus_r <= XLEN'(MSTATUS_MPP_M)
                   | (XLEN'(mstatus_r[MSTATUS_MIE_BIT]) << MSTATUS_MPIE_BIT);
      end
      if (mret) begin
        mstatus_r <= XLEN'(MSTATUS_MPP_M) | (XLEN'(1) << MSTATUS_MPIE_BIT)
                   | (XLEN'(mstatus_r[MSTATUS_MPIE_BIT]) << MSTATUS_MIE_BIT);
      end
    end
  end

  assign mlstat_sat = mlstat_r;
endmodule : vortex_csr

`endif
