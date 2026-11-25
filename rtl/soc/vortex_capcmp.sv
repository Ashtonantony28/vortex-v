// vortex_capcmp.sv — capture/compare timer for the Vortex-V SoC (ADR 0017).
//
// A free-running TW-bit timer that ticks every PRESC+1 clocks (1 MHz at 50 MHz
// by default). N_CAP input-capture channels timestamp edges into a CAP_DEPTH
// FIFO; N_CMP one-shot compare slots drive N_OUT outputs at an absolute tick.
// Built for engine control (Redline): crank/cam edges in, injector and coil
// outputs out, with no software latency between the tick and the output.
//
// Registers (8 bytes apart; reads have no side effects, KNOWN-ISSUES I20):
//   0x00 TIME       R   timer value
//   0x08 PRESC      RW  timer tick = PRESC+1 clocks (16 bits); a write restarts the divider
//   0x10 CTRL       RW  bits 2c+1:2c edge select of capture channel c (0 off, 1 rising,
//                       2 falling, 3 both; resets to 1); bit 16 CAP_IE (external interrupt
//                       while the FIFO is not empty; resets to 0)
//   0x18 CAP_WR     R   captures written, free-running TW bits
//   0x20 CAP_RD     RW  captures read, free-running; write CAP_RD+n to pop n entries
//                       (a value past CAP_WR pops all)
//   0x28 CAP_DATA   R   entry CAP_RD mod CAP_DEPTH: bit 63 = valid (FIFO not empty),
//                       bits 39:32 = kind (channel + 1), bits TW-1:0 = timer at the edge;
//                       reads 0 while the FIFO is empty
//   0x30 CAP_OVF    RW  edges dropped on a full FIFO (saturating); a write clears it
//   0x38 CMP_ARG    RW  pulse length in ticks for the next CMP_PUSH
//   0x40 CMP_PUSH   W   queue a compare: bits TW-1:0 tick, bits 39:32 output, bits 42:40
//                       action (1 pulse for CMP_ARG ticks, 2 set, 3 clear). Dropped if
//                       every slot is busy or the output does not exist (counted in
//                       CMP_STATUS)
//   0x48 CMP_STATUS R   bits 7:0 pending compares, bits 47:16 pushes dropped; a write
//                       clears the drop count
//   0x50 OUT        RW  output levels; a write sets them and cancels running pulses
//   0x58 CMP_FLUSH  W   cancel every pending compare
//
// Compare rules: a slot fires in the first clock where (TIME - tick), as a signed
// TW-bit value, is >= 0, so a tick already in the past fires at once. Slots that
// fire together on one output combine as: clear wins, then pulse (the highest
// slot's length), then set. A pulse ends PULSE ticks after it fired, unless a
// later action on that output replaces it. Outputs are registered.
//
// Capture rules: inputs are synchronised (2 flops). An edge latches the timer
// in that channel's holding register; one held edge per clock enters the FIFO,
// lowest channel first. An edge on a full FIFO, or on a channel still holding
// one, is dropped and counted in CAP_OVF.
//
// Reset is asynchronous, like the rest of the SoC.
`ifndef VORTEX_CAPCMP_SV
`define VORTEX_CAPCMP_SV
module vortex_capcmp #(
  parameter int unsigned XLEN      = 64,
  parameter int unsigned TW        = 32,    // timer width
  parameter int unsigned N_CAP     = 2,     // capture channels
  parameter int unsigned CAP_DEPTH = 32,    // capture FIFO entries (power of 2)
  parameter int unsigned N_OUT     = 8,     // compare outputs
  parameter int unsigned N_CMP     = 8,     // pending compare slots
  parameter int unsigned PRESC_RST = 49     // reset prescaler: 50 MHz -> 1 MHz
) (
  input  logic             clk,
  input  logic             rst_n,
  // Register port (offsets within the block; 8-byte registers)
  input  logic             we,
  input  logic [6:0]       addr,
  input  logic [XLEN-1:0]  wdata,
  output logic [XLEN-1:0]  rdata,
  // Pins
  input  logic [N_CAP-1:0] cap_in,
  output logic [N_OUT-1:0] cmp_out,
  output logic             irq
);
  localparam int unsigned CW = $clog2(CAP_DEPTH);
  localparam int unsigned OW = (N_OUT > 1) ? $clog2(N_OUT) : 1;
  localparam int unsigned SW = $clog2(N_CMP + 1);          // slot count
  localparam int unsigned KW = (N_CMP > 1) ? $clog2(N_CMP) : 1;   // slot index
  localparam int unsigned HW = (N_CAP > 1) ? $clog2(N_CAP) : 1;   // capture channel index
  localparam int unsigned PW = 16;               // prescaler width
  localparam int unsigned DCW = 32;              // drop counters

  localparam logic [6:0] R_TIME = 7'h00, R_PRESC = 7'h08, R_CTRL = 7'h10, R_CAP_WR = 7'h18,
                         R_CAP_RD = 7'h20, R_CAP_DATA = 7'h28, R_CAP_OVF = 7'h30,
                         R_CMP_ARG = 7'h38, R_CMP_PUSH = 7'h40, R_CMP_STATUS = 7'h48,
                         R_OUT = 7'h50, R_CMP_FLUSH = 7'h58;
  localparam int unsigned CTRL_IE_BIT = 16;
  localparam logic [2:0] ACT_PULSE = 3'd1, ACT_SET = 3'd2, ACT_CLEAR = 3'd3;
  localparam int unsigned KIND_LSB = 32, VALID_BIT = 63, ACT_LSB = 40;

  initial begin
    if ((1 << CW) != CAP_DEPTH)
      $fatal(1, "vortex_capcmp: CAP_DEPTH must be a power of 2");
    if (TW > 32 || N_CAP < 1 || N_CAP > 8 || N_OUT < 1 || N_OUT > 32 || N_CMP < 1 || N_CMP > 255)
      $fatal(1, "vortex_capcmp: need TW <= 32, 1..8 captures, 1..32 outputs, 1..255 slots");
  end

  // ---- Timer ----
  logic [TW-1:0] time_q;
  logic [PW-1:0] presc_q, div_q;
  logic          tick;
  assign tick = (div_q == presc_q);

  // ---- Registers ----
  logic [2*N_CAP-1:0] edge_mode;
  logic               cap_ie;
  logic [TW-1:0]      arg_q;

  // ---- Capture ----
  logic [N_CAP-1:0] sync1, sync2, prev;
  logic [N_CAP-1:0] held;                    // an edge waiting to enter the FIFO
  logic [TW-1:0]    held_ts [N_CAP];
  logic [TW-1:0]    fifo_ts [CAP_DEPTH];
  logic [7:0]       fifo_kind [CAP_DEPTH];
  logic [TW-1:0]    cap_wr, cap_rd;
  logic [DCW-1:0]   cap_ovf;
  logic [TW-1:0]    cap_count;
  assign cap_count = cap_wr - cap_rd;

  logic [N_CAP-1:0] edge_now;
  always_comb begin
    for (int c = 0; c < N_CAP; c++) begin
      edge_now[c] = (edge_mode[2*c]   &&  sync2[c] && !prev[c])     // rising
                 || (edge_mode[2*c+1] && !sync2[c] &&  prev[c]);    // falling
    end
  end

  // Lowest held channel enters the FIFO this clock
  logic              push_cap;
  logic [HW-1:0]     push_ch;
  always_comb begin
    push_cap = 1'b0;
    push_ch  = '0;
    for (int c = N_CAP - 1; c >= 0; c--) begin
      if (held[c]) begin
        push_cap = 1'b1;
        push_ch  = HW'(c);
      end
    end
  end
  logic cap_full;
  assign cap_full = (cap_count == TW'(CAP_DEPTH));

  // ---- Compare slots ----
  logic             slot_v    [N_CMP];
  logic [TW-1:0]    slot_tick [N_CMP];
  logic [TW-1:0]    slot_arg  [N_CMP];
  logic [OW-1:0]    slot_ch   [N_CMP];
  logic [2:0]       slot_act  [N_CMP];
  logic [N_CMP-1:0] fire;
  logic [DCW-1:0]   cmp_drop;
  logic [SW-1:0]    pending;

  logic [N_OUT-1:0] out_q, pulse_on;
  logic [TW-1:0]    pulse_end [N_OUT];

  // Due when TIME - tick is >= 0 as a signed TW-bit value (wrap-safe)
  logic [TW-1:0] slot_diff [N_CMP];
  logic [TW-1:0] pulse_diff [N_OUT];
  always_comb begin
    pending = '0;
    for (int k = 0; k < N_CMP; k++) begin
      slot_diff[k] = time_q - slot_tick[k];
      fire[k]      = slot_v[k] && !slot_diff[k][TW-1];
      pending      = pending + SW'(slot_v[k]);
    end
    for (int o = 0; o < N_OUT; o++) pulse_diff[o] = time_q - pulse_end[o];
  end

  // Lowest free slot for a push
  logic          has_free;
  logic [KW-1:0] free_idx;
  always_comb begin
    has_free = 1'b0;
    free_idx = '0;
    for (int k = N_CMP - 1; k >= 0; k--) begin
      if (!slot_v[k]) begin
        has_free = 1'b1;
        free_idx = KW'(k);
      end
    end
  end

  // Per-output result of this clock's firings
  logic [N_OUT-1:0] f_clear, f_pulse, f_set;
  logic [TW-1:0]    f_len [N_OUT];
  always_comb begin
    f_clear = '0;
    f_pulse = '0;
    f_set   = '0;
    for (int o = 0; o < N_OUT; o++) f_len[o] = '0;
    for (int k = 0; k < N_CMP; k++) begin
      if (fire[k]) begin
        unique case (slot_act[k])
          ACT_CLEAR: f_clear[slot_ch[k]] = 1'b1;
          ACT_PULSE: begin
            f_pulse[slot_ch[k]] = 1'b1;
            f_len[slot_ch[k]]   = slot_arg[k];
          end
          ACT_SET:   f_set[slot_ch[k]] = 1'b1;
          default:   ;                       // no action: the slot just frees
        endcase
      end
    end
  end

  // ---- Register writes ----
  logic wr_presc, wr_ctrl, wr_rd, wr_ovf, wr_arg, wr_push, wr_status, wr_out, wr_flush;
  assign wr_presc  = we && (addr == R_PRESC);
  assign wr_ctrl   = we && (addr == R_CTRL);
  assign wr_rd     = we && (addr == R_CAP_RD);
  assign wr_ovf    = we && (addr == R_CAP_OVF);
  assign wr_arg    = we && (addr == R_CMP_ARG);
  assign wr_push   = we && (addr == R_CMP_PUSH);
  assign wr_status = we && (addr == R_CMP_STATUS);
  assign wr_out    = we && (addr == R_OUT);
  assign wr_flush  = we && (addr == R_CMP_FLUSH);

  logic [2:0] push_act;
  assign push_act = wdata[ACT_LSB +: 3];

  // ---- Reads ----
  logic [CW-1:0] rd_idx;
  assign rd_idx = cap_rd[CW-1:0];
  always_comb begin
    unique case (addr)
      R_TIME      : rdata = XLEN'(time_q);
      R_PRESC     : rdata = XLEN'(presc_q);
      R_CTRL      : rdata = (XLEN'(cap_ie) << CTRL_IE_BIT) | XLEN'(edge_mode);
      R_CAP_WR    : rdata = XLEN'(cap_wr);
      R_CAP_RD    : rdata = XLEN'(cap_rd);
      R_CAP_DATA  : rdata = (cap_count == '0) ? '0
                          : (XLEN'(1) << VALID_BIT) | (XLEN'(fifo_kind[rd_idx]) << KIND_LSB)
                          | XLEN'(fifo_ts[rd_idx]);
      R_CAP_OVF   : rdata = XLEN'(cap_ovf);
      R_CMP_ARG   : rdata = XLEN'(arg_q);
      R_CMP_STATUS: rdata = (XLEN'(cmp_drop) << 16) | XLEN'(pending);
      R_OUT       : rdata = XLEN'(out_q);
      default     : rdata = '0;
    endcase
  end

  assign cmp_out = out_q;
  assign irq     = cap_ie && (cap_count != '0);

  // ---- State ----
  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      time_q    <= '0;
      div_q     <= '0;
      presc_q   <= PW'(PRESC_RST);
      for (int c = 0; c < N_CAP; c++) edge_mode[2*c +: 2] <= 2'b01;
      cap_ie    <= 1'b0;
      arg_q     <= '0;
      sync1     <= '0;
      sync2     <= '0;
      prev      <= '0;
      held      <= '0;
      cap_wr    <= '0;
      cap_rd    <= '0;
      cap_ovf   <= '0;
      cmp_drop  <= '0;
      out_q     <= '0;
      pulse_on  <= '0;
      for (int k = 0; k < N_CMP; k++) slot_v[k] <= 1'b0;
    end else begin
      // Timer
      if (wr_presc) begin
        presc_q <= wdata[PW-1:0];
        div_q   <= '0;
      end else if (tick) begin
        div_q  <= '0;
        time_q <= time_q + TW'(1);
      end else begin
        div_q <= div_q + PW'(1);
      end

      if (wr_ctrl) begin
        edge_mode <= wdata[2*N_CAP-1:0];
        cap_ie    <= wdata[CTRL_IE_BIT];
      end
      if (wr_arg) arg_q <= wdata[TW-1:0];

      // Capture: synchronise, detect, hold, then one held edge per clock into the FIFO
      sync1 <= cap_in;
      sync2 <= sync1;
      prev  <= sync2;
      if (push_cap) begin
        if (!cap_full) begin
          fifo_ts[cap_wr[CW-1:0]]   <= held_ts[push_ch];
          fifo_kind[cap_wr[CW-1:0]] <= 8'(push_ch) + 8'd1;
          cap_wr                    <= cap_wr + TW'(1);
        end
      end
      for (int c = 0; c < N_CAP; c++) begin
        if (push_cap && (push_ch == HW'(c))) held[c] <= 1'b0;
        if (edge_now[c] && (!held[c] || (push_cap && (push_ch == HW'(c))))) begin
          held[c]    <= 1'b1;
          held_ts[c] <= time_q;
        end
      end
      // Drops: an edge on a channel still holding one, or a held edge on a full FIFO
      begin
        logic [3:0] drops;
        drops = 4'(push_cap && cap_full);
        for (int c = 0; c < N_CAP; c++) begin
          if (edge_now[c] && held[c] && !(push_cap && (push_ch == HW'(c))))
            drops = drops + 4'd1;
        end
        if (wr_ovf)
          cap_ovf <= '0;
        else if ((DCW+1)'(cap_ovf) + (DCW+1)'(drops) > (DCW+1)'({DCW{1'b1}}))
          cap_ovf <= '1;
        else
          cap_ovf <= cap_ovf + DCW'(drops);
      end
      if (wr_rd) begin
        if ((wdata[TW-1:0] - cap_rd) > cap_count) cap_rd <= cap_wr;
        else                                      cap_rd <= wdata[TW-1:0];
      end

      // Compare: fired slots free; pulses end; outputs take this clock's actions
      for (int k = 0; k < N_CMP; k++) if (fire[k]) slot_v[k] <= 1'b0;
      if (wr_push) begin
        if (has_free && (wdata[KIND_LSB +: 8] < 8'(N_OUT))) begin
          slot_v[free_idx]    <= 1'b1;
          slot_tick[free_idx] <= wdata[TW-1:0];
          slot_arg[free_idx]  <= arg_q;
          slot_ch[free_idx]   <= wdata[KIND_LSB +: OW];
          slot_act[free_idx]  <= push_act;
        end else begin
          cmp_drop <= (cmp_drop == '1) ? cmp_drop : cmp_drop + DCW'(1);
        end
      end
      if (wr_status) cmp_drop <= '0;
      if (wr_flush) for (int k = 0; k < N_CMP; k++) slot_v[k] <= 1'b0;

      for (int o = 0; o < N_OUT; o++) begin
        if (f_clear[o]) begin
          out_q[o]    <= 1'b0;
          pulse_on[o] <= 1'b0;
        end else if (f_pulse[o]) begin
          out_q[o]     <= 1'b1;
          pulse_on[o]  <= 1'b1;
          pulse_end[o] <= time_q + f_len[o];
        end else if (f_set[o]) begin
          out_q[o]    <= 1'b1;
          pulse_on[o] <= 1'b0;
        end else if (pulse_on[o] && !pulse_diff[o][TW-1]) begin
          out_q[o]    <= 1'b0;
          pulse_on[o] <= 1'b0;
        end
      end
      if (wr_out) begin
        out_q    <= wdata[N_OUT-1:0];
        pulse_on <= '0;
      end
    end
  end
endmodule : vortex_capcmp
`endif
