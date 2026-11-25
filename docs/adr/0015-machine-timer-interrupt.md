# 0015. Machine timer interrupt, taken with the pipeline drained

- **Status:** Accepted (decisions 1 and 3 amended by 0017: the machine external interrupt)
- **Date:** 2026-11-21

## Context

ADR 0014 gave both cores Zicsr and synchronous traps but left interrupts out: `mie` and `mip`
read as 0, and the SoC has a cycle counter but no way to raise anything. Software on the board
can only poll. A periodic tick is what any RTOS, a sampling loop or a watchdog needs, and it is
the one interrupt the SoC can source today (there is no external interrupt controller and no
UART receiver).

How the out-of-order core takes an interrupt is the real decision:

- **Flush and take at once.** Mark the ROB head as interrupted, squash everything younger and
  redirect. Low latency, but it needs a full ROB flush path (squash of non-branch state, LSQ,
  the Xmlacc pipelines), which no trap needs today (every trap in ADR 0014 is known at decode).
- **Drain and take.** Stop dispatch while an interrupt is pending, let everything already in
  the ROB finish and commit, and take the interrupt when the ROB is empty. The precise state is
  then the committed state, the next pc is simply `pc`, and nothing needs undoing. Latency is
  one pipeline drain (tens of cycles worst case, a divide plus a full ROB), which is fine for a
  timer tick. It is the same "serialize at the head" choice ADR 0014 made for CSR ops.

## Decision

1. **Scope: the machine timer interrupt only** (`mip.MTIP`, cause 7). No software or external
   interrupts, no vectored `mtvec`, no `mtime` CSR (RISC-V puts `mtime` in memory).
2. **SoC.** `mtime` is the existing CYCLE register (MMIO + 0x18). A new RW register
   `MTIMECMP` at MMIO + 0x20 resets to all ones. The SoC drives `irq_timer` to the core,
   registered: `CYCLE >= MTIMECMP`. The level stays high until software writes `MTIMECMP`.
3. **CSRs** (amending the `mie`/`mip` row of ADR 0014): `mie.MTIE` (bit 7) is writable, every
   other `mie` bit reads 0. `mip` is read-only and reads `MTIP` (bit 7) = `irq_timer`. The
   interrupt is pending when `MTIP && mie.MTIE && mstatus.MIE`.
4. **Taking it** (both cores, in `vortex_csr`): `mepc` = the pc of the next instruction in
   program order (not yet executed), `mcause` = 2^63 + 7, `mtval` = 0, MPIE = MIE, MIE = 0,
   continue at `mtvec`. Nothing retires that cycle and `minstret` does not count it.
   - **In-order core:** taken instead of executing the instruction at `pc`, except while a
     multi-cycle mul/div is in progress (it finishes first).
   - **Out-of-order core:** a pending interrupt stops dispatch; it is taken in the first cycle
     the ROB is empty. If the drained instructions clear MIE or MTIE (or software disarms the
     timer), it is not taken.
5. **New core ports** (M1-INTERFACE v1.2): in `irq_timer`, out `dbg_irq` (a one-cycle pulse
   when an interrupt is taken).
6. **Golden model and co-sim.** The ISS does not model when interrupts arrive. It gets `mie`,
   `mip` reads from the RTL (like `mcycle`), and `take_interrupt()`. The core harness models
   `MTIMECMP` at `MTIMECMP_ADDR` (0x8000_1008, next to tohost) and drives `irq_timer` like the
   SoC. For each `dbg_irq` the co-sim takes the interrupt in the ISS between the same two
   retires, after checking the ISS has it enabled (MIE and MTIE); every later retire must
   match.
7. **Software.** `crt0` saves the caller-saved registers on a timer interrupt, calls
   `irq_handler()` (weak default: disarm the timer) and returns with `mret`; other traps still
   fail the run. `vortex.h` gets `MTIMECMP`, `timer_arm`, `timer_disarm`, `irq_enable`,
   `irq_disable`. `wfi` stays a no-op (allowed by the spec).

**Verification.**
- `verif/core/test_irq.py` on all eight configurations, under lockstep co-sim, with explicit
  values: `mip.MTIP` follows the timer; nothing is taken with MIE or MTIE clear; then `mcause`,
  `mepc` (the next instruction), `mtval` = 0, MPIE/MIE in the handler, MIE restored by `mret`;
  and a race that arms the timer -4..107 cycles from an mcycle read before a `csrci` that clears MIE, behind a
  store, a divide, a load, a branch and a loop, so the interrupt arrives at every point around
  them and the disable (it may come before, never after; a preempted store must not have
  happened; the handler's multiply must not get the preempted divide's result).
- `verif/crv` `test_random_cosim_irq`: the random programs with a handler that re-arms the timer
  48-111 cycles ahead, on all eight configurations (about 150-280 interrupts per 25 seeds,
  landing inside branch shadows, loads, divides and Xmlacc ops); the handler's count must equal
  the RTL's `dbg_irq` count.
- The SoC program takes three ticks from C and prints ` irq ok`.
- ISS unit tests for `mie`/`mip` and `take_interrupt`.
- Mutation checks (listed in the PR).

## Consequences

- **Easier:** periodic work and timeouts on the board without polling; the riscv-tests `rv64mi`
  suite gets closer (it still needs misaligned-access traps).
- **Accepted:** out-of-order interrupt latency is a pipeline drain: whatever was already
  dispatched when the interrupt arrived runs first. A handler that returns without writing
  `MTIMECMP` is re-entered at once.
- **Follow-on:** software and external interrupts (with a PLIC or a UART receiver), `wfi` that
  waits for an interrupt, misaligned-access traps.
