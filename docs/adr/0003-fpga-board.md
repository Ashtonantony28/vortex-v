# 0003. FPGA target: Digilent Arty A7-100T with free Vivado, RTL kept vendor-neutral

- **Status:** Accepted
- **Date:** 2026-11-06

## Context

The core must run on real hardware, not only in simulation, to make the out-of-order and TinyML
claims credible and to expose the timing and resource realities that simulation hides. Choosing
a board commits us to a vendor toolchain and a resource/timing budget.

Constraints and options considered:

- **Cost and availability.** The board should be affordable and widely available to a solo
  developer, with a good reference design ecosystem.
- **Toolchain cost.** The synthesis toolchain must be free for the chosen device.
- **Capacity.** The device must fit a 2-wide OoO RV64 core *plus* caches *plus* the Xmlacc
  functional units (and later the systolic engine), with headroom.
- **Lock-in risk.** We do not want to be permanently tied to one proprietary toolchain.

The **Arty A7-100T** carries a Xilinx Artix-7 XC7A100T (~101k logic cells, ~240 DSP slices,
~4.8 Mb block RAM). It is inexpensive, ubiquitous in the hobbyist/academic RISC-V community,
and — critically — the A7-100T part is supported by **Vivado's free WebPACK/Standard edition**,
so synthesis, place-and-route, and bitstream generation cost nothing. The DSP slices are useful
for the int8 MAC datapath and later the systolic engine. The smaller A7-35T was considered and
rejected as too tight once caches and ML units are added.

Relying on Vivado is a lock-in risk. The open-source flow (yosys + nextpnr) does not target
Artix-7 well, but it does target Lattice ECP5. We want the option to retarget later without a
rewrite.

## Decision

- The v1 hardware target is the **Digilent Arty A7-100T** (Artix-7 XC7A100T), synthesized with
  **free Vivado**.
- The **core RTL is kept vendor-neutral.** No Xilinx-specific primitives in the core logic.
  All device-specific constructs — block RAM / FIFO instantiation, clocking (MMCM/PLL), I/O —
  are confined to thin wrappers under `rtl/fpga/` with a generic behavioral fallback, so the
  core can later be retargeted to **Lattice ECP5 via the open yosys/nextpnr flow** by swapping
  only those wrappers.

## Consequences

- **Easier:** Zero-cost, well-trodden path to a working bitstream on affordable hardware with
  enough capacity and DSPs for the ML datapath.
- **Easier:** The vendor-neutral discipline keeps an escape hatch to a fully open-source flow
  (ECP5) and makes the RTL cleaner to simulate and reason about.
- **Harder / accepted:** Maintaining the wrapper boundary is ongoing discipline; it is tempting
  to reach for a Xilinx IP core for a FIFO or a BRAM. We accept the small overhead of wrapping.
- **Harder / accepted:** We depend on proprietary Vivado for the primary target, including its
  specific timing-closure behavior. The ECP5 path mitigates but does not remove this.
- **Follow-on work:** The actual ECP5 retarget (if pursued) warrants its own ADR covering the
  chosen board, resource re-budgeting, and any width/frequency changes it forces.
