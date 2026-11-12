# 0007. M5 two-wide: dual fetch, rename, ALU issue and commit, with an additive interface

- **Status:** Accepted
- **Date:** 2026-11-12

## Context

ADR 0002 fixes the width at 2. M5 widens the ADR 0006 core: 2 instructions renamed, issued and
retired per cycle. Three things limit a 1-wide core to at most 1 instruction per cycle, and
all three must widen. The fetch port returns 1 instruction. Commit drives one set of `dbg_*`
retire ports. The issue queue picks one entry.

The M1 interface (docs/M1-INTERFACE-v1.md) is shared by every suite. Replacing its ports
would break the in-order core and the harness at once. Adding ports does not.

Issue options:

- (a) Two symmetric ports, each able to reach every unit. This needs two copies of the
  multiplier, divider and Xmlacc unit, or arbitration between the ports.
- (b) One full port plus one ALU-only port. ALU ops are most of the dynamic mix (in the
  constrained-random mix, about half), so a second ALU captures most of the gain. Branches can
  go on either port, because at most one branch is ever in flight (ADR 0006 has no speculation).

## Decision

1. **Width is a parameter.** `vortex_ooo_core` gets `WIDTH` (1 or 2). It defaults to 1
   (CLAUDE.md: features default off), and every suite runs on both widths. The `WIDTH=1`
   behaviour is exactly ADR 0006.
2. **Additive interface (M1-INTERFACE v1.1).** New ports, present on `vortex_ooo_core` at
   either width:
   - `imem_addr1` / `imem_rdata1`: a second combinational fetch port, always `pc + 4`.
   - `dbg1_retire_valid`, `dbg1_retire_pc`, `dbg1_wb_valid`, `dbg1_wb_rd`, `dbg1_wb_data`:
     a second retire slot. Within a cycle, slot 0 is older than slot 1. Slot 1 is valid only
     when slot 0 is valid.

   `vortex_core` keeps the v1 ports. The harness serves and reads the new ports only when
   the toplevel has them.
3. **Dispatch up to 2 per cycle.** Slot 1 dispatches only when slot 0 does and slot 0 is not a
   branch, JAL or JALR. A JAL in slot 0 redirects. A branch or JALR stops dispatch until it
   resolves. Slot 1 also needs room in the ROB, the issue queue, the memory queue and the free
   list for both instructions together.
4. **Rename within the group.** Slot 1 reads slot 0's new physical register when its source
   is slot 0's destination. Its old physical register is slot 0's new one when both write the
   same `rd`. Slot 1 wins the RAT write. The free list pops up to 2 per cycle.
5. **Issue up to 2 per cycle.** Port 1 is ALU/branch only and takes the oldest ready ALU entry.
   Port 0 takes the oldest ready entry of any unit that port 1 did not take. Each port has its
   own PRF read ports and writeback/wakeup port.
6. **Commit up to 2 per cycle.** Slot 1 commits the next ROB entry when slot 0 commits and that
   entry is complete. A store only ever commits in slot 0, because it executes only as ROB
   head. Nothing commits in slot 1 after a `tohost` store in slot 0. The free list takes up to
   2 old physical registers back. `mlstat.SAT` is set from either slot.
7. **Unchanged from ADR 0006:** one data-memory port with an in-order memory queue, no
   speculation, and one multiplier, one divider and one Xmlacc unit.

**Verification.** Every suite (directed, ISS co-sim, riscv-tests, constrained-random) runs on
the in-order core, the OoO core at `WIDTH=1` and the OoO core at `WIDTH=2`. The co-sim checks
slot 0 and slot 1 retires in order against the ISS. A proof test shows more than one instruction
per cycle on independent ALU code, using a debug count of dual-commit cycles and a cycle count
against the in-order core. Lint runs on both widths.

## Consequences

- **Easier:** the in-order core and all v1 harness code are untouched. The 1-wide OoO core
  stays available as a reference while 2-wide bugs are chased.
- **Harder:** intra-group rename is a new class of bug: a same-cycle dependency, a same-`rd`
  pair, or two free-list pops. The constrained-random generator already produces many
  back-to-back dependencies, and mutation checks must show the suites catch these bugs.
- **Accepted:** memory operations stay 1 per cycle and multiplies, divides and Xmlacc ops stay
  on one port. Loops with a branch every few instructions gain little until M6 speculation.
  M7 replaces the memory queue.
- **Follow-on:** the PRF now has 5 read ports and 5 write ports, plus a debug read for each
  commit slot. That is more pressure for M9 timing (see ADR 0006 consequences).
