// vortex_muldiv.sv — RV64M integer multiply/divide execution unit.
//
// M1.1 part A. Provides MUL/MULH/MULHSU/MULHU/DIV/DIVU/REM/REMU (64-bit) and
// the *W variants (MULW/DIVW/DIVUW/REMW/REMUW) that operate on the low 32 bits
// and sign-extend the 32-bit result to XLEN.
//
// Microarchitecture:
//   * Multiply is combinational (the tools infer a `*`); the result is latched
//     and surfaced with a single-cycle latency so it shares the handshake below.
//   * Divide is a radix-2 (restoring) sequential divider: XLEN iterations, one
//     quotient bit per clock, on the unsigned *magnitudes* of the operands, with
//     the result sign applied at the end. A divide-by-zero finishes in one cycle
//     (no iteration). This keeps the critical path short and the datapath small
//     and synthesizable (no `/` or `%` operators), which is the realistic choice
//     for a core heading toward OoO.
//
// Handshake (one op at a time; the in-order core stalls retire until `done`):
//   * `start` : 1-cycle request. Accepted only when `!busy`.
//   * `busy`  : high from the cycle after an accepted `start` until the result
//               cycle (inclusive-exclusive — see below).
//   * `done`  : 1-cycle pulse asserted together with a valid `result`; `busy`
//               is simultaneously deasserted so the consumer sees {busy=0,done=1}
//               on exactly the result cycle.
//
// RISC-V spec corner cases (no traps):
//   * divide by zero : DIV*/DIVU* -> all-ones quotient ; REM*/REMU* -> dividend.
//   * signed overflow (most-negative / -1) : DIV* -> dividend ; REM* -> 0.
//     Handled naturally by the magnitude datapath (|MIN| fits unsigned and the
//     two's-complement result equals the dividend), no special case required.
`ifndef VORTEX_MULDIV_SV
`define VORTEX_MULDIV_SV
module vortex_muldiv
  import vortex_pkg::*;
#(
  parameter int unsigned XLEN = 64
) (
  input  logic               clk,
  input  logic               rst_n,

  input  logic               start,
  input  vortex_pkg::md_op_e  op,
  input  logic               is_word,
  input  logic [XLEN-1:0]     a,       // rs1
  input  logic [XLEN-1:0]     b,       // rs2

  output logic               busy,
  output logic               done,
  output logic [XLEN-1:0]     result
);

  localparam int unsigned CNTW = $clog2(XLEN) + 1; // counts 0..XLEN inclusive

  // =========================================================================
  // Combinational operand interpretation (on the LIVE inputs, sampled at start)
  // =========================================================================
  // Operation class decode.
  logic op_is_mul;    // any multiply
  logic op_div_quot;  // produces a quotient (DIV/DIVU) rather than a remainder
  logic op_signed;    // signed division family (DIV/REM)
  always_comb begin
    op_is_mul   = (op == MD_MUL) || (op == MD_MULH)
               || (op == MD_MULHSU) || (op == MD_MULHU);
    op_div_quot = (op == MD_DIV) || (op == MD_DIVU);
    op_signed   = (op == MD_DIV) || (op == MD_REM);
  end

  // ---- Multiply (combinational, full 128-bit products) --------------------
  logic signed [2*XLEN-1:0] a_se, b_se;   // sign-extended to 128
  logic        [2*XLEN-1:0] a_ze, b_ze;   // zero-extended to 128
  assign a_se = {{XLEN{a[XLEN-1]}}, a};
  assign b_se = {{XLEN{b[XLEN-1]}}, b};
  assign a_ze = {{XLEN{1'b0}}, a};
  assign b_ze = {{XLEN{1'b0}}, b};

  // verilator lint_off UNUSED
  logic [2*XLEN-1:0] prod_ss;  // signed   x signed   (only high half used)
  logic [2*XLEN-1:0] prod_uu;  // unsigned x unsigned
  logic [2*XLEN-1:0] prod_su;  // signed   x unsigned (only high half used)
  // verilator lint_on UNUSED
  assign prod_ss = a_se * b_se;
  assign prod_uu = a_ze * b_ze;                 // both operands unsigned
  assign prod_su = a_se * $signed(b_ze);        // b_ze is positive (top half 0)

  logic [31:0] mulw_lo;
  assign mulw_lo = a[31:0] * b[31:0];           // low 32 bits of 32x32 product

  logic [XLEN-1:0] mul_result;
  always_comb begin
    if (is_word) begin
      // MULW: low 32 of product, sign-extended.
      mul_result = {{(XLEN-32){mulw_lo[31]}}, mulw_lo};
    end else begin
      unique case (op)
        MD_MUL   : mul_result = prod_uu[XLEN-1:0];     // low XLEN (sign-agnostic)
        MD_MULH  : mul_result = prod_ss[2*XLEN-1:XLEN];
        MD_MULHSU: mul_result = prod_su[2*XLEN-1:XLEN];
        MD_MULHU : mul_result = prod_uu[2*XLEN-1:XLEN];
        default  : mul_result = prod_uu[XLEN-1:0];
      endcase
    end
  end

  // ---- Divide operand interpretation --------------------------------------
  // Effective (width- and sign-honoring) dividend/divisor bit patterns.
  logic [XLEN-1:0] dvd_eff, dvs_eff;
  always_comb begin
    if (is_word) begin
      if (op_signed) begin
        dvd_eff = {{(XLEN-32){a[31]}}, a[31:0]};
        dvs_eff = {{(XLEN-32){b[31]}}, b[31:0]};
      end else begin
        dvd_eff = {{(XLEN-32){1'b0}}, a[31:0]};
        dvs_eff = {{(XLEN-32){1'b0}}, b[31:0]};
      end
    end else begin
      dvd_eff = a;
      dvs_eff = b;
    end
  end

  logic a_neg, b_neg;
  assign a_neg = op_signed & dvd_eff[XLEN-1];
  assign b_neg = op_signed & dvs_eff[XLEN-1];

  logic [XLEN-1:0] a_mag, b_mag;
  assign a_mag = a_neg ? (-dvd_eff) : dvd_eff;
  assign b_mag = b_neg ? (-dvs_eff) : dvs_eff;

  logic div_zero;
  assign div_zero = (dvs_eff == '0);

  // Div-by-zero result, already word-formatted (sign-extended for *W).
  logic [XLEN-1:0] divzero_raw;   // all-ones quotient, or dividend remainder
  logic [XLEN-1:0] divzero_result;
  assign divzero_raw = op_div_quot ? {XLEN{1'b1}} : dvd_eff;
  assign divzero_result = is_word
       ? {{(XLEN-32){divzero_raw[31]}}, divzero_raw[31:0]}
       : divzero_raw;

  // The 1-cycle "fast" result (multiply or divide-by-zero).
  logic            fast_valid;
  logic [XLEN-1:0] fast_result;
  assign fast_valid  = op_is_mul || div_zero;
  assign fast_result = op_is_mul ? mul_result : divzero_result;

  // =========================================================================
  // Sequential core
  // =========================================================================
  logic [2*XLEN-1:0] shreg;      // {remainder, quotient}
  logic [XLEN-1:0]   divisor_q;
  logic [CNTW-1:0]   cnt;
  logic              fast_q;     // this op finishes in one cycle
  logic [XLEN-1:0]   fast_res_q; // latched fast result
  logic              word_q;
  logic              div_quot_q; // produces quotient
  logic              qsign_q;    // quotient negative
  logic              rsign_q;    // remainder negative (sign of dividend)

  // One restoring-division step on the combined {rem,quot} register.
  logic [2*XLEN-1:0] sh;
  logic [XLEN-1:0]   rem_part;
  logic              ge;
  logic [2*XLEN-1:0] step_next;
  assign sh       = shreg << 1;
  assign rem_part = sh[2*XLEN-1:XLEN];
  assign ge       = (rem_part >= divisor_q);
  assign step_next = ge ? {(rem_part - divisor_q), sh[XLEN-1:1], 1'b1} : sh;

  // Finalized divide result from the current shreg.
  logic [XLEN-1:0] quot_mag, rem_mag, quot_fin, rem_fin, div_raw, div_result;
  assign quot_mag = shreg[XLEN-1:0];
  assign rem_mag  = shreg[2*XLEN-1:XLEN];
  assign quot_fin = qsign_q ? (-quot_mag) : quot_mag;
  assign rem_fin  = rsign_q ? (-rem_mag)  : rem_mag;
  assign div_raw  = div_quot_q ? quot_fin : rem_fin;
  assign div_result = word_q
       ? {{(XLEN-32){div_raw[31]}}, div_raw[31:0]}
       : div_raw;

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      busy       <= 1'b0;
      done       <= 1'b0;
      result     <= '0;
      shreg      <= '0;
      divisor_q  <= '0;
      cnt        <= '0;
      fast_q     <= 1'b0;
      fast_res_q <= '0;
      word_q     <= 1'b0;
      div_quot_q <= 1'b0;
      qsign_q    <= 1'b0;
      rsign_q    <= 1'b0;
    end else begin
      done <= 1'b0;   // default: no pulse

      if (!busy) begin
        if (start) begin
          busy       <= 1'b1;
          word_q     <= is_word;
          div_quot_q <= op_div_quot;
          qsign_q    <= a_neg ^ b_neg;
          rsign_q    <= a_neg;
          if (fast_valid) begin
            fast_q     <= 1'b1;
            fast_res_q <= fast_result;
          end else begin
            fast_q     <= 1'b0;
            shreg      <= {{XLEN{1'b0}}, a_mag};
            divisor_q  <= b_mag;
            cnt        <= CNTW'(XLEN);
          end
        end
      end else begin
        if (fast_q) begin
          // multiply / divide-by-zero: result ready one cycle after start
          result <= fast_res_q;
          done   <= 1'b1;
          busy   <= 1'b0;
        end else if (cnt == '0) begin
          result <= div_result;
          done   <= 1'b1;
          busy   <= 1'b0;
        end else begin
          shreg <= step_next;
          cnt   <= cnt - CNTW'(1);
        end
      end
    end
  end

endmodule : vortex_muldiv
`endif
