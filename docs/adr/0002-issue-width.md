# 0002. Issue width: 2-wide superscalar, not 4-wide

- **Status:** Accepted
- **Date:** 2026-11-06

## Context

The core is out-of-order superscalar. The superscalar width (how many instructions can be
renamed, issued, and retired per cycle) is a first-order decision: it shapes the rename logic,
the wakeup/select loops in the issue queue, the bypass network, the register file port count,
the ROB width, and ultimately whether the design closes timing on the target FPGA.

The target is a Xilinx Artix-7 (Arty A7-100T, see ADR 0003), a mid-range FPGA with modest LUT
delay and limited routing compared to an ASIC. The hardest timing path in any OoO core is the
**wakeup/select** loop — a destination tag broadcast must wake dependent instructions and the
select logic must pick ready instructions, all inside a single cycle — and the **bypass
network** that forwards results to in-flight instructions. The cost of both grows
super-linearly with issue width:

- Wakeup comparators scale with (issue width × issue-queue entries × operands).
- The select tree depth and the bypass mux fan-in grow with width.
- Register file read/write ports roughly double going from 2-wide to 4-wide.

On Artix-7 these structures are exactly where a 4-wide design fails to meet timing: the
wakeup/select loop becomes a critical path that cannot be pipelined without breaking
back-to-back dependent issue, and the bypass mux delay balloons. A 4-wide OoO core is a
well-known timing trap on this class of FPGA.

## Decision

We will build a **2-wide** superscalar core: 2 instructions renamed, issued (subject to
functional-unit availability), and retired per cycle. The microarchitecture (issue queue,
rename, ROB, register file ports, bypass network) is sized and laid out for 2-wide. Width is a
parameter where practical, but 2 is the design point we commit to closing timing on; 4-wide is
explicitly out of scope for v1.

## Consequences

- **Easier:** Realistic FPGA timing closure on Artix-7. The wakeup/select loop and bypass
  network stay within a single-cycle budget at achievable clock rates, which keeps the OoO study
  meaningful (a core that does not meet timing teaches the wrong lessons).
- **Easier:** Smaller register file (fewer ports), smaller issue queue, simpler select trees —
  less LUT and routing pressure, leaving headroom for the ML functional units and caches.
- **Harder / accepted:** Peak IPC is capped at 2. For TinyML inner loops this is rarely the
  bottleneck (they are memory- and MAC-bound), but it does cap scalar throughput.
- **Harder / accepted:** Some interesting 4-wide scheduling phenomena (wider wakeup contention,
  deeper select) will not be studied in this core.
- **Follow-on work:** If a later target is a larger FPGA or ASIC, revisit width in a new ADR.
  Keeping width parameterized reduces (but does not eliminate) the cost of that change, since the
  wakeup/select and bypass structures would need re-timing regardless.
