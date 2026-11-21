# 0016. Misaligned-access traps, with a flush at the ROB head

- **Status:** Accepted
- **Date:** 2026-11-21

## Context

A misaligned load or store neither worked nor trapped (KNOWN-ISSUES I21): the core put the
address on the bus and the memory did whatever it does with it, so a bug in C code silently
read or wrote the wrong bytes. A jump to a target that is not 4-byte aligned did not trap
either. The privileged spec allows misaligned loads and stores to be done in hardware or to
trap; with no C extension, a misaligned jump target must trap.

Doing misaligned accesses in hardware means splitting an access that crosses a doubleword into
two bus cycles, in both cores, the LSQ forwarding unit and the SoC RAM. Trapping is much
smaller, and software that wants misaligned access can emulate it in the handler.

Unlike every trap in ADR 0014, these are found at execute (an address or a jump target from a
register), not at decode. In the out-of-order core younger instructions are already in flight
by then, so taking the trap needs what ADR 0014 avoided: a flush of everything younger. The
options:

- **Kill at detection.** Squash everything younger than the faulting op as soon as the AGU
  sees the address. But an older branch may still mispredict, or an older op fault, so the
  fault may not be real, and the trap state (mepc, mcause) would need undoing.
- **Mark and flush at the head.** Record the fault in the ROB and let the op wait. When it is
  the oldest instruction, the fault is certain: flush the whole pipeline back to the committed
  state and trap. The committed state is already kept (`arch_rat`), and the branch squash
  already has kill paths for the multiplier, divider, Xmlacc pipelines and the SYNC_MEM load
  stage. This is how out-of-order cores usually take precise exceptions.

## Decision

1. **Traps.** A load or store whose address is not a multiple of its size traps with mcause 4
   (load) or 6 (store) and mtval = the address. A taken jump or branch whose target is not
   4-byte aligned (after JALR clears bit 0) traps with mcause 0 and mtval = the target. mepc is
   the faulting instruction. It does nothing else: no memory access, no register write (not
   even a JAL/JALR link), no `minstret` count. MPIE/MIE and the jump to `mtvec` are as in
   ADR 0014. A not-taken branch with a misaligned target does not trap.
2. **CSR file.** `vortex_csr` gets a `fault` input with cause, mtval and pc from the core.
3. **In-order core.** It detects both cases in the instruction's single cycle and traps
   instead of executing it.
4. **Out-of-order core.**
   - A misaligned memory op is marked in the ROB (`rob_exc`) by the AGU. It never executes:
     the memory queue does not fire a misaligned load (in order, or from the LSQ) or store.
   - A JALR, or a branch that is not predicted, that would jump to a misaligned target is
     marked instead of redirecting. Dispatch stays stopped behind it. A branch or JAL whose
     offset has bit 1 set is known at decode to have a misaligned target. Such a branch is not
     predicted and waits like a non-speculative branch. Such a JAL is marked at dispatch. These
     never go in dispatch slot 1. Only one such jump is in flight, so one register holds its
     target for mtval.
   - When a marked op is the ROB head, nothing commits that cycle, and the core flushes:
     - the speculative RAT is restored from `arch_rat`;
     - the in-flight pregs return to the free list (head rolled back by the number allocated,
       count reset to its idle value);
     - the ROB, issue queue and memory queue are emptied;
     - a running multiply or divide is killed, and the Xmlacc pipelines and SYNC_MEM load stage
       are dropped;
     - fetch goes to `mtvec`.
   - A faulting memory op at the ROB head is also the memory-queue head, so its address gives
     mtval.
   - The fault is reported on `dbg_retire_*` with no register write, like an ADR 0014 trap.
5. **Golden model.** The ISS traps the same way when `traps=True`. With `traps=False`, its own
   unit tests still allow misaligned accesses.
6. **Software.** `crt0` already fails the run on any synchronous trap with code 0x100 + mcause.

**Verification.**
- `verif/core/test_misaligned.py` on all eight configurations, under lockstep co-sim with the
  data region compared, with expected values computed in the test:
  - **access:** 12 misaligned loads and stores of every size, with aligned and byte accesses
    between them;
  - **jumps:** JAL forward and backward, a taken and a not-taken branch, JALR at +2 and +3,
    JALR at +1 landing correctly;
  - **flush:** a misaligned load waiting on a divide with loads, stores, multiplies, Xmlacc ops
    and branches in flight behind it;
  - **wrong path:** misaligned loads, stores and JALRs only on mispredicted paths, which must
    not trap.
- `verif/crv` `test_random_cosim_traps`: random programs with misaligned loads/stores (also in
  branch shadows and loops) and JALRs to misaligned targets, plus timer interrupts, on all eight
  configurations. The handler counts faults and skips the instruction.
- ISS unit tests for each cause, aligned and byte accesses, a not-taken branch, and JALR bit-0
  clearing.
- Mutation checks (listed in the PR).

## Consequences

- **Easier:** a bad pointer in C traps instead of silently corrupting data. The core has a
  general precise-exception flush, so later execute-time faults (access faults, page faults)
  can reuse it.
- **Accepted:** the riscv-tests `ma_data` test stays excluded, because it expects misaligned
  accesses to succeed (hardware or trap-and-emulate). A misaligned op waits until it is the
  oldest instruction and then costs a full flush.
- **Follow-on:** misaligned access emulation in `crt0` if software needs it; the riscv-tests
  `rv64mi` suite (`ma_addr`, `ma_fetch`) once it is vendored.
