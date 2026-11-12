// vortex_soc.sv — simulation-ready SoC around a Vortex-V core (ADR 0011).
//
// One core (the in-order vortex_core or vortex_ooo_core), one unified RAM, and a
// small MMIO block: a UART transmitter, four LEDs and a cycle counter.
//
// Address map
//   RAM_BASE .. +RAM_BYTES   code, data and stack (default 0x8000_0000, 16 KiB)
//   TOHOST_ADDR              in RAM; a store ends the program: 1 = pass (M1 contract)
//   MMIO_BASE + 0x00  W      UART_TX: send the low byte (ignored while busy)
//   MMIO_BASE + 0x08  R      UART_STATUS: bit 0 = transmitter busy
//   MMIO_BASE + 0x10  RW     LEDS: bits 3:0
//   MMIO_BASE + 0x18  R      CYCLE: cycles since reset
// Other addresses read 0 and ignore writes.
//
// MMIO reads have no side effects, so speculative loads may read them safely
// (KNOWN-ISSUES I12). Stores reach MMIO only at commit.
`ifndef VORTEX_SOC_SV
`define VORTEX_SOC_SV
module vortex_soc
  import vortex_pkg::*;
#(
  parameter int unsigned      XLEN          = 64,
  parameter bit               CORE_OOO      = 1'b1,   // 0 = in-order vortex_core
  parameter int unsigned      WIDTH         = 1,      // vortex_ooo_core parameters
  parameter int unsigned      SPEC_BRANCHES = 0,
  parameter int unsigned      LSQ           = 0,
  parameter int unsigned      XM_LATENCY    = 1,
  parameter int unsigned      SYNC_MEM      = 0,      // 1 = block-RAM-style memory (ADR 0012)
  parameter int unsigned      XM_PORTS      = 1,      // 2 = second Xmlacc unit, WIDTH=2 only (ADR 0013)
  parameter int unsigned      RAM_BYTES     = 16384,
  parameter logic [XLEN-1:0]  RAM_BASE      = 64'h8000_0000,
  parameter logic [XLEN-1:0]  TOHOST_ADDR   = 64'h8000_1000,
  parameter logic [XLEN-1:0]  MMIO_BASE     = 64'h1000_0000,
  parameter int unsigned      CLKS_PER_BIT  = 868,    // UART: 100 MHz / 115200 baud
  parameter                   INIT_FILE     = ""      // RAM image (hex, 64-bit words)
) (
  input  logic       clk,
  input  logic       rst_n,
  output logic       uart_tx,
  output logic [3:0] leds,
  output logic       done,       // the program stored to tohost
  output logic       pass        // ... and the value was 1
);
  localparam int unsigned RAMW       = $clog2(RAM_BYTES);
  localparam int unsigned MMIO_SPAN  = 32;
  localparam int unsigned MMW        = $clog2(MMIO_SPAN);
  localparam logic [MMW-1:0] REG_UART_TX     = MMW'(5'h00);
  localparam logic [MMW-1:0] REG_UART_STATUS = MMW'(5'h08);
  localparam logic [MMW-1:0] REG_LEDS        = MMW'(5'h10);
  localparam logic [MMW-1:0] REG_CYCLE       = MMW'(5'h18);
  localparam logic [XLEN-1:0] TOHOST_PASS    = XLEN'(1);

  initial begin
    if (SYNC_MEM != 0 && !CORE_OOO)
      $fatal(1, "vortex_soc: SYNC_MEM needs the out-of-order core (ADR 0012)");
    if (XM_PORTS != 1 && !CORE_OOO)
      $fatal(1, "vortex_soc: XM_PORTS needs the out-of-order core (ADR 0013)");
  end

  // ---- Core ----
  logic [XLEN-1:0] imem_addr, imem_addr1;
  logic [31:0]     imem_rdata, imem_rdata1;
  logic            dmem_req, dmem_we;
  logic [XLEN-1:0] dmem_addr, dmem_wdata, dmem_rdata;
  logic [1:0]      dmem_size;

  // The debug/retire ports are for the co-sim harness; the SoC leaves them open.
  /* verilator lint_off PINCONNECTEMPTY */
  if (CORE_OOO) begin : g_ooo
    vortex_ooo_core #(
      .XLEN          (XLEN),
      .RESET_PC      (RAM_BASE),
      .TOHOST_ADDR   (TOHOST_ADDR),
      .WIDTH         (WIDTH),
      .SPEC_BRANCHES (SPEC_BRANCHES),
      .LSQ           (LSQ),
      .XM_LATENCY    (XM_LATENCY),
      .SYNC_MEM      (SYNC_MEM),
      .XM_PORTS      (XM_PORTS)
    ) u_core (
      .clk               (clk),
      .rst_n             (rst_n),
      .imem_addr         (imem_addr),
      .imem_rdata        (imem_rdata),
      .imem_addr1        (imem_addr1),
      .imem_rdata1       (imem_rdata1),
      .dmem_req          (dmem_req),
      .dmem_we           (dmem_we),
      .dmem_addr         (dmem_addr),
      .dmem_size         (dmem_size),
      .dmem_wdata        (dmem_wdata),
      .dmem_rdata        (dmem_rdata),
      .dbg_retire_valid  (),
      .dbg_retire_pc     (),
      .dbg_wb_valid      (),
      .dbg_wb_rd         (),
      .dbg_wb_data       (),
      .dbg_mlstat_sat    (),
      .dbg1_retire_valid (),
      .dbg1_retire_pc    (),
      .dbg1_wb_valid     (),
      .dbg1_wb_rd        (),
      .dbg1_wb_data      ()
    );
  end else begin : g_inorder
    vortex_core #(
      .XLEN        (XLEN),
      .RESET_PC    (RAM_BASE),
      .TOHOST_ADDR (TOHOST_ADDR)
    ) u_core (
      .clk              (clk),
      .rst_n            (rst_n),
      .imem_addr        (imem_addr),
      .imem_rdata       (imem_rdata),
      .dmem_req         (dmem_req),
      .dmem_we          (dmem_we),
      .dmem_addr        (dmem_addr),
      .dmem_size        (dmem_size),
      .dmem_wdata       (dmem_wdata),
      .dmem_rdata       (dmem_rdata),
      .dbg_retire_valid (),
      .dbg_retire_pc    (),
      .dbg_wb_valid     (),
      .dbg_wb_rd        (),
      .dbg_wb_data      (),
      .dbg_mlstat_sat   ()
    );
    assign imem_addr1 = imem_addr + XLEN'(4);
    /* verilator lint_off UNUSEDSIGNAL */   // the in-order core has one fetch port
    logic unused_rdata1;
    assign unused_rdata1 = ^imem_rdata1;
    /* verilator lint_on UNUSEDSIGNAL */
  end
  /* verilator lint_on PINCONNECTEMPTY */

  // ---- Address decode ----
  logic sel_ram, sel_mmio;
  assign sel_ram  = (dmem_addr[XLEN-1:RAMW] == RAM_BASE[XLEN-1:RAMW]);
  assign sel_mmio = (dmem_addr[XLEN-1:MMW]  == MMIO_BASE[XLEN-1:MMW]);

  // ---- RAM ----
  logic [XLEN-1:0] ram_rdata;
  vortex_ram #(
    .XLEN      (XLEN),
    .BYTES     (RAM_BYTES),
    .BASE      (RAM_BASE),
    .INIT_FILE (INIT_FILE),
    .SYNC      (SYNC_MEM != 0)
  ) u_ram (
    .clk    (clk),
    .iaddr0 (imem_addr),
    .idata0 (imem_rdata),
    .iaddr1 (imem_addr1),
    .idata1 (imem_rdata1),
    .daddr  (dmem_addr),
    .drdata (ram_rdata),
    .dwe    (dmem_req && dmem_we && sel_ram),
    .dsize  (dmem_size),
    .dwdata (dmem_wdata)
  );

  // ---- MMIO ----
  logic            tx_busy, tx_start;
  logic [XLEN-1:0] cycle;
  logic [3:0]      led_q;
  logic [MMW-1:0]  reg_off;
  assign reg_off  = dmem_addr[MMW-1:0];
  assign tx_start = dmem_req && dmem_we && sel_mmio && (reg_off == REG_UART_TX);

  vortex_uart_tx #(.CLKS_PER_BIT(CLKS_PER_BIT)) u_uart_tx (
    .clk   (clk),
    .rst_n (rst_n),
    .valid (tx_start),
    .data  (dmem_wdata[7:0]),
    .busy  (tx_busy),
    .tx    (uart_tx)
  );

  logic [XLEN-1:0] mmio_rdata;
  always_comb begin
    unique case (reg_off)
      REG_UART_STATUS: mmio_rdata = XLEN'(tx_busy);
      REG_LEDS:        mmio_rdata = XLEN'(led_q);
      REG_CYCLE:       mmio_rdata = cycle;
      default:         mmio_rdata = '0;
    endcase
  end
  // With SYNC_MEM the RAM answers a cycle late, so the MMIO data and the
  // select are registered to line up with it.
  logic            sel_ram_q, sel_mmio_q;
  logic [XLEN-1:0] mmio_rdata_q;
  always_ff @(posedge clk) begin
    sel_ram_q    <= sel_ram;
    sel_mmio_q   <= sel_mmio;
    mmio_rdata_q <= mmio_rdata;
  end
  assign dmem_rdata = (SYNC_MEM != 0)
                    ? (sel_ram_q ? ram_rdata : sel_mmio_q ? mmio_rdata_q : '0)
                    : (sel_ram   ? ram_rdata : sel_mmio   ? mmio_rdata   : '0);

  // Asynchronous active-low reset, like the core (the FPGA top synchronises its release).
  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      cycle <= '0;
      led_q <= '0;
      done  <= 1'b0;
      pass  <= 1'b0;
    end else begin
      cycle <= cycle + XLEN'(1);
      if (dmem_req && dmem_we && sel_mmio && (reg_off == REG_LEDS)) led_q <= dmem_wdata[3:0];
      if (dmem_req && dmem_we && (dmem_addr == TOHOST_ADDR) && !done) begin
        done <= 1'b1;
        pass <= (dmem_wdata == TOHOST_PASS);
      end
    end
  end
  assign leds = led_q;
endmodule : vortex_soc
`endif
