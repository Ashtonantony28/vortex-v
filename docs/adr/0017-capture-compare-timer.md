# 0017. Capture/compare timer and the machine external interrupt

- **Status:** Accepted
- **Date:** 2026-11-25

## Context

The Redline engine-control firmware (crank/cam decoding, fuel and spark scheduling) runs on
Vortex-V in co-simulation. Today it timestamps crank teeth and fires injector and coil events in
software, from a behavioural RAM struct its Verilator wrapper pokes. At 50 MHz the software path
puts spark up to 0.76 crank degrees late at 7000 rpm, against a 0.5 degree budget. Redline wrote
down what it needs (`interfaces/redline-capture-compare.md` in the project files):

- a free-running 32-bit timer at 1 MHz, compared wrap-safe (signed differences);
- input capture of crank (kind 1) and cam (kind 2) edges into a 32-entry FIFO with free-running
  write and read indices, popped by a write; a full FIFO drops the edge, with a sticky count;
- at least 8 pending one-shot output compares on 4 injector and 4 coil outputs, with pulse
  (injector: on at the tick, off `arg` ticks later), set (dwell) and clear (spark) actions; a
  compare already in the past fires at once;
- a maskable capture interrupt, level-triggered while the FIFO is not empty, as a machine
  external interrupt. Knock sensing stays out (it is an ADC/knock-IC path).

The options were a generic timer block that software drives (still software latency at the
edge), a PLIC plus a separate timer (far more than one interrupt source needs), or one
self-contained peripheral that does the time-critical work in hardware and raises one level
interrupt. Only the last removes the latency Redline is fighting, and it is small.

## Decision

1. **Peripheral `vortex_capcmp`** (`rtl/soc/vortex_capcmp.sv`), parameters `TW` (32), `N_CAP`
   (2), `CAP_DEPTH` (32), `N_OUT` (8), `N_CMP` (8), `PRESC_RST` (49). Twelve 64-bit registers;
   the module header is the register reference:

   | Offset | Name | Access | Meaning |
   |---|---|---|---|
   | 0x00 | TIME | R | timer, ticks every PRESC+1 clocks |
   | 0x08 | PRESC | RW | 16 bits; a write restarts the divider |
   | 0x10 | CTRL | RW | 2 bits per capture channel (0 off, 1 rising, 2 falling, 3 both; reset 1), bit 16 CAP_IE |
   | 0x18 | CAP_WR | R | captures written (free-running) |
   | 0x20 | CAP_RD | RW | captures read; write CAP_RD+n to pop n, a value past CAP_WR pops all |
   | 0x28 | CAP_DATA | R | entry CAP_RD mod depth: bit 63 valid, 39:32 kind (channel+1), TW-1:0 tick; 0 when empty |
   | 0x30 | CAP_OVF | RW | dropped edges, saturating; a write clears |
   | 0x38 | CMP_ARG | RW | pulse length for the next push |
   | 0x40 | CMP_PUSH | W | tick, output (39:32), action (42:40: 1 pulse, 2 set, 3 clear) |
   | 0x48 | CMP_STATUS | R | 7:0 pending, 47:16 pushes dropped (no free slot or no such output); a write clears the count |
   | 0x50 | OUT | RW | output levels; a write sets them and cancels running pulses |
   | 0x58 | CMP_FLUSH | W | cancel every pending compare |

   - **Reads never have side effects** (KNOWN-ISSUES I12/I20: speculative loads may read
     devices). Popping, clearing and arming are all writes.
   - **Compare:** a slot fires in the first clock where `TIME - tick`, as a signed TW-bit value,
     is >= 0, so a past tick fires at once. Slots firing together on one output combine as clear,
     then pulse (the highest slot's length), then set. A pulse ends `arg` ticks after it fired,
     unless a later action on that output replaces it. Outputs are registered.
   - **Capture:** pins pass a 2-flop synchroniser. An edge latches TIME into the channel's
     holding register; one held edge per clock enters the FIFO, lowest channel first. An edge on
     a full FIFO, or on a channel still holding one, is dropped and counted.
   - `irq = CAP_IE && FIFO not empty`.
2. **SoC.** `vortex_soc` gets `CAPCMP` (default 0, off), `CAP_PRESC` (49: 1 MHz at the board's
   50 MHz core clock), `N_CAP`, `N_OUT`, and ports `cap_in`, `cmp_out` (0 when off). The block
   sits at MMIO + 0x80..0xFF (the MMIO window grows from 64 to 256 bytes; the new space reads 0
   without the block). 64-bit accesses only. The Arty top ties `cap_in` to 0 for now; pins come
   with the board bring-up.
3. **Machine external interrupt** (amending decisions 1 and 3 of ADR 0015). New core input
   `irq_ext` (M1-INTERFACE v1.3), the SoC's capture interrupt. `mie.MEIE` (bit 11) is writable
   next to MTIE; `mip.MEIP` reads `irq_ext`. It is pending when `MEIP && MEIE && mstatus.MIE`,
   is taken exactly like the timer interrupt (same drain on the out-of-order core) with
   `mcause` = 2^63 + 11, and has priority over the timer, as the privileged spec orders them.
4. **Golden models.** `verif/soc/capcmp_model.py` is a cycle-accurate Python model written from
   the register description. The ISS gains `pending_interrupt(mip)`, which picks the interrupt
   the spec says is taken; the co-sim passes it the RTL's interrupt levels at the point the RTL
   took one. The core harness drives `irq_ext` from bit 0 of the last store to `EXTIRQ_ADDR`
   (0x8000_1010).
5. **Software.** `crt0` passes `mcause` to `irq_handler(uint64_t mcause)`. `vortex.h` gets the
   `CC_*` registers, `cc_push`, `cc_pulse`, `cc_diff`, `MIE_MEIE`, `MCAUSE_TIMER`, `MCAUSE_EXT`.

**Verification.**
- `verif/soc/test_capcmp.py`: the RTL against the model on every cycle (register read, outputs,
  irq) under 60,000 cycles of random traffic in three builds: the default, a 12-bit timer with a
  4-entry FIFO, 3 capture channels, 5 outputs and 3 slots (timer wrap, full FIFO, busy slots),
  and a minimal 8-bit, 1-channel, 1-output, 1-slot build. Directed checks against hand-worked
  ticks: a past compare fires at once, a pulse lasts exactly its length, a full FIFO drops and
  counts, a CAP_RD write past CAP_WR pops all.
- `verif/core/test_irq.py` `ext`, on all eight core configurations under lockstep co-sim:
  `mip.MEIP` follows `irq_ext`, nothing is taken with MEIE clear, `mie` keeps only MTIE and MEIE,
  and with both pending the external interrupt is taken first (`mcause`, `mepc`), then the timer.
- `verif/soc/test_soc.py` `sw/capcmp` on three CAPCMP=1 SoCs (in-order, out-of-order, and the
  board configuration): the bench drives four crank edges and a cam edge; the firmware drains the
  FIFO in the external-interrupt handler and schedules dwell, injector pulse and spark for each
  crank edge; the bench checks every capture's kind and tick and every output edge to the exact
  tick, and that a compare armed 5 ticks in the past fires in the clock after the push.
- ISS unit tests for the `mie` mask, `mip` and `pending_interrupt`.
- Mutation checks (listed in the PR).

## Consequences

- **Easier:** Redline's HAL writes a compare per event and reads the FIFO under an interrupt;
  output timing no longer depends on software latency (it is exact to the 1 µs tick), and the
  firmware can drop its predict-and-spin loop.
- **Accepted:** at most one held edge per channel between FIFO writes, so two edges on one
  channel in successive clocks drop the second (crank teeth are thousands of clocks apart). The
  out-of-order core's interrupt latency is still a pipeline drain (I25); the hardware compares
  make that harmless for output timing. One external interrupt source, no PLIC.
- **Follow-on:** board pins for `cap_in`/`cmp_out` (Pmod) at bring-up; a PLIC if a second source
  (UART receive) arrives; random external interrupts in the `verif/crv` co-sim (today the random
  arrival is exercised with the timer, which shares the take path).
