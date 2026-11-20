"""Zicsr and machine-mode traps (ADR 0014), on every core configuration.

Each program runs under lockstep co-sim against the ISS (every retired
instruction's pc and register write must match), and the tests add explicit
checks of the values the privileged spec and ADR 0014 fix, so a misreading
shared by the RTL and the ISS still fails.

Programs:
  * csr_rw     - every implemented CSR: read/write/set/clear, the field masks,
                 read-only and ignored CSRs, minstret exactness, mcycle.
  * traps      - ecall, ebreak and eight illegal encodings trap to mtvec with the
                 right mcause/mepc/mtval and mstatus.MIE/MPIE; mret returns.
  * mlstat     - the SAT sticky set by mlacc.rqs, read and cleared through the CSR.
  * wrong_path - illegal words and ecalls on mispredicted or jumped-over paths
                 must never trap (mtvec is 0, so a wrong trap derails the run).
  * fence_i    - self-modifying code: a store into the instruction stream, then
                 fence.i, then the new instruction runs.

Run (from the repo root):  python3 -m pytest verif/core/test_csr.py -q
"""
from pathlib import Path

import pytest

try:
    import cocotb
except ImportError:  # pragma: no cover - cocotb is a hard dependency of verif/
    cocotb = None

from cores import CORES, build_and_test
from rvasm import Asm, DATA_BASE, DATA_SIZE, MASK64, TOHOST_ADDR

CSR_MSTATUS, CSR_MISA, CSR_MIE, CSR_MTVEC = 0x300, 0x301, 0x304, 0x305
CSR_MSCRATCH, CSR_MEPC, CSR_MCAUSE, CSR_MTVAL, CSR_MIP = 0x340, 0x341, 0x342, 0x343, 0x344
CSR_MLCFG, CSR_MLSTAT = 0x800, 0x801
CSR_MCYCLE, CSR_MINSTRET, CSR_CYCLE, CSR_INSTRET = 0xB00, 0xB02, 0xC00, 0xC02
CSR_MVENDORID, CSR_MARCHID, CSR_MIMPID, CSR_MHARTID = 0xF11, 0xF12, 0xF13, 0xF14

MISA_VALUE = (2 << 62) | (1 << 8) | (1 << 12) | (1 << 23)
MSTATUS_MPP_M = 0x1800
MIE, MPIE = 1 << 3, 1 << 7
CAUSE_ILLEGAL, CAUSE_BREAKPOINT, CAUSE_ECALL_M = 2, 3, 11

# Illegal encodings the traps program executes, in order (mtval must equal each).
ILLEGAL_WORDS = [
    0x0000_0000,   # all zeros
    0xFFFF_FFFF,   # all ones
    0xC000_9073,   # csrrw x0, cycle, x1    (write to a read-only CSR)
    0x7C00_22F3,   # csrrs x5, 0x7c0, x0    (CSR that does not exist)
    0x0000_4073,   # SYSTEM funct3 = 100    (reserved)
    0x0000_200F,   # MISC-MEM funct3 = 010  (not FENCE / FENCE.I)
    0x0400_0033,   # OP with funct7 = 0000010
    0x3020_8073,   # mret with rs1 = x1     (not MRET)
]
TRAP_RECORD = 32               # bytes the handler stores per trap
WRONG_PATH_LOOPS = 12
BHT_ENTRIES = 64               # vortex_ooo_core default: branches this many words apart alias


def _end(a):
    a.la(31, TOHOST_ADDR)
    a.addi(29, 0, 1)
    a.sw(29, 31, 0)


def csr_rw_program():
    a = Asm()
    a.la(30, DATA_BASE)
    a.addi(1, 0, -1)                          # x1 = all ones
    a.csrrw(2, CSR_MSCRATCH, 1)               # x2 = 0 (reset value)
    a.csrr(3, CSR_MSCRATCH)                   # x3 = ~0
    a.csrrc(4, CSR_MSCRATCH, 1)               # x4 = ~0, mscratch = 0
    a.csrrsi(5, CSR_MSCRATCH, 21)             # x5 = 0,  mscratch = 21
    a.csrrci(6, CSR_MSCRATCH, 5)              # x6 = 21, mscratch = 16
    a.csrr(7, CSR_MSCRATCH)                   # x7 = 16
    a.csrw(CSR_MTVEC, 1); a.csrr(8, CSR_MTVEC)        # ~3: direct mode only
    a.csrw(CSR_MEPC, 1); a.csrr(9, CSR_MEPC)          # ~3: IALIGN = 32
    a.csrw(CSR_MCAUSE, 1); a.csrr(10, CSR_MCAUSE)     # ~0
    a.csrw(CSR_MTVAL, 1); a.csrr(11, CSR_MTVAL)       # ~0
    a.csrw(CSR_MSTATUS, 1); a.csrr(12, CSR_MSTATUS)   # MPP | MPIE | MIE
    a.csrw(CSR_MISA, 0); a.csrr(13, CSR_MISA)         # write ignored
    a.csrw(CSR_MIE, 1); a.csrr(14, CSR_MIE)           # 0: no interrupts
    a.csrw(CSR_MIP, 1); a.csrr(15, CSR_MIP)           # 0
    a.csrr(16, CSR_MHARTID)
    a.csrr(17, CSR_MVENDORID)
    a.csrr(18, CSR_MARCHID)
    a.csrr(19, CSR_MIMPID)
    a.csrw(CSR_MLCFG, 1); a.csrr(20, CSR_MLCFG)       # 0: reserved
    a.csrr(21, CSR_MINSTRET)
    a.nop(); a.nop()
    a.csrr(22, CSR_INSTRET)                   # x22 - x21 = 3
    a.addi(23, 0, 100)
    a.csrw(CSR_MINSTRET, 23)                  # not counted itself
    a.csrr(24, CSR_MINSTRET)                  # 100
    a.csrr(25, CSR_INSTRET)                   # 101
    a.csrr(26, CSR_MCYCLE)
    a.csrr(27, CSR_CYCLE)                     # later than x26
    a.csrrs(28, CSR_MHARTID, 0)               # read-only CSR, no write: legal
    a.wfi(); a.fence(); a.fence_i()
    for i, r in enumerate((2, 3, 4, 5, 6, 7, 8, 9, 12, 13, 14, 16, 20, 24, 25)):
        a.sd(r, 30, 8 * i)
    _end(a)
    return a.assemble()


def traps_program():
    a = Asm()
    a.la_label(1, "handler")
    a.csrw(CSR_MTVEC, 1)
    a.la(30, DATA_BASE)
    a.csrrsi(0, CSR_MSTATUS, 8)               # MIE = 1
    a.ecall()
    a.csrr(10, CSR_MSTATUS)                   # mret restored MIE: MPP | MPIE | MIE
    a.sd(10, 30, 0)
    a.addi(30, 30, 8)
    a.ebreak()
    for w in ILLEGAL_WORDS:
        a.word(w)
    a.jal(0, "done")
    a.label("handler")                        # record the trap, skip the instruction
    a.csrr(20, CSR_MCAUSE); a.sd(20, 30, 0)
    a.csrr(21, CSR_MEPC);   a.sd(21, 30, 8)
    a.csrr(22, CSR_MTVAL);  a.sd(22, 30, 16)
    a.csrr(23, CSR_MSTATUS); a.sd(23, 30, 24)
    a.addi(30, 30, TRAP_RECORD)
    a.addi(21, 21, 4)
    a.csrw(CSR_MEPC, 21)
    a.mret()
    a.label("done")
    _end(a)
    return a.assemble()


def mlstat_program():
    a = Asm()
    a.la(30, DATA_BASE)
    a.lui(1, 0x10)                            # x1 = 65536: saturates rqs at shift 0
    a.addi(2, 0, 1)
    a.csrr(5, CSR_MLSTAT)                     # 0
    a.rqs(3, 1, 0)                            # +127, SAT set
    a.csrr(6, CSR_MLSTAT)                     # 1
    a.csrrc(0, CSR_MLSTAT, 2)                 # CSRRC is a no-op on a W1C bit
    a.csrr(7, CSR_MLSTAT)                     # 1
    a.csrrs(0, CSR_MLSTAT, 2)                 # CSRRS clears (W1C)
    a.csrr(8, CSR_MLSTAT)                     # 0
    a.rqs(3, 1, 0)
    a.csrrwi(9, CSR_MLSTAT, 1)                # x9 = 1, cleared
    a.csrr(10, CSR_MLSTAT)                    # 0
    a.rqs(3, 1, 0)                            # ends with SAT set
    for i, r in enumerate((5, 6, 7, 8, 9, 10)):
        a.sd(r, 30, 8 * i)
    _end(a)
    return a.assemble()


def _n_ops(a):
    return sum(1 for kind, _ in a.items if kind == "op")


def wrong_path_program():
    """mtvec stays 0: any trap jumps out of the program and the run fails.

    Wrong paths with illegal words and ecalls behind them: an always-taken branch
    (predicted not-taken until its counter trains), a jump, a JALR, and a never-
    taken branch placed BHT_ENTRIES instructions after the always-taken one so the
    two share a counter and the never-taken one is predicted taken."""
    a = Asm()
    a.la(30, DATA_BASE)
    a.addi(1, 0, WRONG_PATH_LOOPS)
    a.addi(2, 0, 0)
    a.label("loop")
    alias_at = _n_ops(a) + BHT_ENTRIES
    a.beq(0, 0, "a")                          # always taken
    a.word(0x0000_0000)
    a.label("a")
    a.andi(3, 1, 1)
    a.beq(3, 0, "b")                          # alternates: both paths are real
    a.addi(2, 2, 1)
    a.label("b")
    a.addi(1, 1, -1)
    a.bne(1, 0, "loop")
    a.jal(0, "over")
    a.ecall()
    a.word(0xFFFF_FFFF)
    a.label("over")
    a.la_label(4, "after")
    a.jalr(0, 4, 0)
    a.ecall()
    a.ebreak()
    a.word(0x0000_0000)
    a.label("after")
    while _n_ops(a) < alias_at:
        a.nop()
    a.bne(0, 0, "bad")                        # never taken, predicted taken
    a.jal(0, "fin")
    a.label("bad")
    a.word(0x0000_0000)
    a.ecall()
    a.label("fin")
    a.sd(2, 30, 0)
    _end(a)
    return a.assemble()


def fence_i_program():
    a = Asm()
    a.la(30, DATA_BASE)
    a.la_label(1, "patch")
    a.lui(3, 0x02A00)                         # x3 = 0x02A00293 = addi x5, x0, 42
    a.addi(3, 3, 0x293)
    a.sw(3, 1, 0)
    a.fence_i()
    a.label("patch")
    a.addi(5, 0, 1)                           # overwritten before it runs
    a.sd(5, 30, 0)
    _end(a)
    return a.assemble()


if cocotb is not None:
    from harness import cosim_lockstep, final_regs

    async def _run(dut, name, words):
        retires, state, iss = await cosim_lockstep(dut, name, words, max_cycles=4000,
                                                   mem_check=(DATA_BASE, DATA_SIZE))
        return final_regs(retires), iss

    def _mem64(iss, addr):
        return iss.mem.load(addr, 8) & MASK64

    @cocotb.test()
    async def csr_rw(dut):
        regs, iss = await _run(dut, "csr_rw", csr_rw_program())
        ones = MASK64
        expect = {2: 0, 3: ones, 4: ones, 5: 0, 6: 21, 7: 16, 8: ones & ~3, 9: ones & ~3,
                  10: ones, 11: ones, 12: MSTATUS_MPP_M | MPIE | MIE, 13: MISA_VALUE,
                  14: 0, 15: 0, 16: 0, 17: 0, 18: 0, 19: 0, 20: 0, 24: 100, 25: 101}
        for r, v in expect.items():
            assert regs[r] == v, f"x{r} = {regs[r]:#x}, expected {v:#x}"
        assert regs[22] - regs[21] == 3, f"instret advanced {regs[22] - regs[21]}, expected 3"
        assert regs[27] > regs[26] > 0, f"cycle {regs[27]} after mcycle {regs[26]}"

    @cocotb.test()
    async def traps(dut):
        _, iss = await _run(dut, "traps", traps_program())
        assert _mem64(iss, DATA_BASE + TRAP_RECORD) == MSTATUS_MPP_M | MPIE | MIE, \
            "mret did not restore mstatus.MIE"
        causes = [CAUSE_ECALL_M, CAUSE_BREAKPOINT] + [CAUSE_ILLEGAL] * len(ILLEGAL_WORDS)
        tvals = [0, 0] + ILLEGAL_WORDS
        recs = [DATA_BASE] + [DATA_BASE + 8 + TRAP_RECORD * k
                              for k in range(1, len(causes))]
        epcs = []
        for k, (base, cause, tval) in enumerate(zip(recs, causes, tvals)):
            assert _mem64(iss, base) == cause, f"trap {k}: mcause {_mem64(iss, base)}"
            assert _mem64(iss, base + 16) == tval, f"trap {k}: mtval {_mem64(iss, base + 16):#x}"
            status = _mem64(iss, base + 24)
            assert status == MSTATUS_MPP_M | MPIE, f"trap {k}: mstatus {status:#x}"
            epcs.append(_mem64(iss, base + 8))
        # ebreak and the illegal words are consecutive instructions
        assert all(b - a == 4 for a, b in zip(epcs[1:], epcs[2:])), [hex(e) for e in epcs]

    @cocotb.test()
    async def mlstat(dut):
        _, iss = await _run(dut, "mlstat", mlstat_program())
        got = [_mem64(iss, DATA_BASE + 8 * i) for i in range(6)]
        assert got == [0, 1, 1, 0, 1, 0], got
        assert iss.mlstat == 1

    @cocotb.test()
    async def wrong_path(dut):
        _, iss = await _run(dut, "wrong_path", wrong_path_program())
        assert iss.mcause == 0 and iss.mepc == 0, "a wrong-path instruction trapped"
        assert _mem64(iss, DATA_BASE) == WRONG_PATH_LOOPS // 2

    @cocotb.test()
    async def fence_i(dut):
        _, iss = await _run(dut, "fence_i", fence_i_program())
        assert _mem64(iss, DATA_BASE) == 42, "the patched instruction did not run"


@pytest.mark.parametrize("toplevel", CORES)
def test_csr_and_traps(toplevel):
    build_and_test(toplevel, "test_csr", Path(__file__).parent)
