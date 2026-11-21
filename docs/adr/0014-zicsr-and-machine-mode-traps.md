# 0014. Zicsr and machine-mode traps, serialized at the ROB head

- **Status:** Accepted (the `mie`/`mip` row and "no interrupts" superseded by 0015)
- **Date:** 2026-11-19

## Context

Both cores treat every SYSTEM-opcode instruction (CSR access, `ecall`, `ebreak`, `mret`, `wfi`)
and every illegal encoding as a warning and a no-op. `FENCE` is decoded as illegal too and only
works because "illegal" does nothing. So there is no `mlstat` CSR on the RTL (the SAT sticky is a
debug port only), software cannot read a cycle counter, and a bad instruction runs on silently.
The C runtime in `sw/` and anything like CoreMark or the riscv-tests `p` environment needs Zicsr,
`mcycle`/`minstret` and a trap vector.

Options weighed for the out-of-order core:

- **Rename CSRs and run CSR ops speculatively.** What fast cores do, but it is a large change
  (CSR checkpoints, squash of CSR state) and CSR ops are rare in the target workloads.
- **Serialize.** A SYSTEM instruction (and an illegal one) stops dispatch behind it, the way a
  JALR does, and executes only once it is the oldest instruction in the ROB. Then nothing older
  can still fault or mispredict, nothing younger exists, and CSR side effects never need undoing.
  It costs a pipeline drain per CSR op, which is fine at the rate software uses them.

Every trap cause in scope is known at decode (`ecall`, `ebreak`, an illegal encoding, a CSR
address that does not exist or a write to a read-only CSR), so no trap needs a ROB flush.

## Decision

1. **Scope: machine mode only, no interrupts.** The implemented CSRs are:

   | Address | CSR | Behaviour |
   |---|---|---|
   | 0x300 | `mstatus` | MIE (bit 3) and MPIE (bit 7) writable; MPP (12:11) reads 3; rest 0 |
   | 0x301 | `misa` | read-only: MXL=2, I, M and X (non-standard extension) bits |
   | 0x304, 0x344 | `mie`, `mip` | read as 0, writes ignored (no interrupts yet) |
   | 0x305 | `mtvec` | direct mode only: bits 1:0 read 0 |
   | 0x340 | `mscratch` | read/write |
   | 0x341 | `mepc` | read/write, bits 1:0 read 0 |
   | 0x342 | `mcause` | read/write |
   | 0x343 | `mtval` | read/write |
   | 0xB00, 0xB02 | `mcycle`, `minstret` | read/write counters |
   | 0xC00, 0xC02 | `cycle`, `instret` | read-only shadows of the two counters |
   | 0xF11-0xF14 | `mvendorid`, `marchid`, `mimpid`, `mhartid` | read-only 0 |
   | 0x800 | `mlcfg` | read as 0, writes ignored (XMLACC-SPEC-v1) |
   | 0x801 | `mlstat` | bit 0 = SAT sticky, set by a committing saturating `mlacc.rqs`; a write clears the bits set in the source operand (CSRRW and CSRRS both clear, CSRRC does nothing), as in XMLACC-SPEC-v1 and the ISS |

   Any other address, or a write (CSRRW, or CSRRS/CSRRC with a non-zero source) to a read-only
   address (bits 11:10 = 3), is an illegal instruction. The decoder decides this, so the check is
   a shared table in `vortex_pkg`.
2. **Traps.** `ecall` (cause 11), `ebreak` (3) and an illegal instruction (2) trap: `mepc` gets
   the pc, `mcause` the cause, `mtval` the instruction bits for an illegal instruction and 0
   otherwise, MPIE gets MIE, MIE is cleared, and execution continues at `mtvec`. `mret` sets MIE
   from MPIE, sets MPIE, and continues at `mepc`. `wfi`, `fence` and `fence.i` are no-ops
   (`fence.i` is enough because neither core caches instructions). A trapping instruction writes
   no register and does not count in `minstret`. Misaligned accesses and interrupts are not in
   scope (KNOWN-ISSUES).
3. **Counters.** `mcycle` counts every clock after reset; `minstret` counts retired instructions.
   An instruction that writes `minstret` is not itself counted, so the next instruction reads the
   written value. A CSR read sees every older instruction retired and none younger.
4. **One CSR file, `vortex_csr`,** shared by both cores. It holds the CSRs, does the
   read-modify-write, takes traps and `mret`, gives the redirect target, and counts retirements
   and the `mlstat` SAT sets it is told about. `dbg_mlstat_sat` now comes from the real `mlstat`.
5. **In-order core.** A SYSTEM or illegal instruction executes in its one cycle like any other;
   the next pc is `mtvec` or `mepc` for a trap or `mret`.
6. **Out-of-order core.** SYSTEM and illegal instructions get a new functional-unit class,
   `FU_SYS`. At dispatch they stop dispatch behind them (as a JALR does) and never pair in a
   dispatch group. `FU_SYS` issues only on port 0 and only when its ROB entry is the head. At
   issue it reads and writes the CSR file, writes `rd` through the single-cycle port, completes,
   and redirects fetch: to pc + 4 for a CSR op, `wfi` or `fence`, to `mtvec` for a trap, to
   `mepc` for `mret`. Being the head, it is never on a wrong path and never squashed.
7. **Golden model.** The ISS gets the same CSR file and traps (`traps=True`; the old
   `ecall_halts` behaviour stays the default for its own unit tests). For co-sim the harness gives
   the ISS the RTL's value for reads of `mcycle`/`cycle`, which the ISS cannot predict.

8. **Strict decode.** Because an illegal instruction now traps instead of doing nothing, the
   decoder and the ISS must agree on every encoding. Both now reject OP/OP-32 with funct7
   `0100000` other than SUB/SRA (SUBW/SRAW); before, the decoder accepted it with any funct3 and
   the ISS ignored funct7. FENCE (funct3 000) and FENCE.I (001) are decoded; other MISC-MEM
   encodings are illegal.
9. **Software.** `sw/common/crt0.S` points `mtvec` at a handler that lights all LEDs and fails
   the run with code 0x100 + mcause, so a trap on the board is visible. `vortex.h` gets
   `rdcycle`, `rdinstret`, `mlstat_read` and `mlstat_clear`; `sw/` and the riscv-tests build use
   `-march=rv64im_zicsr_zifencei`.

**Verification.**
- `verif/core/test_csr.py`, under lockstep co-sim on all eight core configurations, with explicit
  expected values on top: every CSR's read/write behaviour and field masks; `minstret`
  exactness, including a write to it; `mcycle`; `ecall`, `ebreak` and eight illegal encodings
  trapping with the right `mcause`/`mepc`/`mtval` and `mstatus` MIE/MPIE, and `mret`; `mlstat`
  set by `rqs`, cleared by CSRRW/CSRRS and not by CSRRC; illegal words and `ecall`s behind an
  untrained always-taken branch, a jump, a JALR and a never-taken branch that shares a predictor
  counter with a taken one (none may trap); and self-modifying code with `fence.i`.
- The random co-sim mix adds CSR reads (`mscratch`, `minstret`, `instret`, `mlstat`, `mcycle`,
  `cycle`, `misa`, `mhartid`), `mscratch` read-modify-writes, `mlstat` clears and `fence`.
- riscv-tests `rv64ui-fence_i` runs now (66 tests, ISS and every core).
- The SoC program checks `mlstat` and `instret` from C and prints ` csr ok`.
- Mutation checks, each caught: a SYSTEM op issuing before it is the ROB head; dispatch not
  stopping behind one; one dispatching in slot 1; a trap or `minstret` write still counted;
  no redirect on a trap; `mtval` not set; MPIE not saved; CSRRC clearing `mlstat`; a `minstret`
  write off by one; an illegal instruction still writing `rd`.

## Consequences

- **Easier:** C code can time itself with `rdcycle`, read and clear `mlstat`, and catch bad
  instructions; the riscv-tests machine-mode environment becomes possible.
- **Accepted:** each CSR op or trap drains the out-of-order pipeline (tens of cycles in the worst
  case). `mcycle` reads are not checked bit-exact against the ISS, only taken from the RTL.
- **Follow-on:** timer interrupts (`mtime`/`mtimecmp` in the SoC, `mie`/`mip`), misaligned access
  traps, and the riscv-tests `rv64mi` suite once it is vendored.
