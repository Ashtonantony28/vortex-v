"""ISS Zicsr and machine-mode traps (ADR 0014). Run: python3 -m pytest model/ -q"""
import pytest

from rv64i import (CAUSE_BREAKPOINT, CAUSE_ECALL_M, CAUSE_ILLEGAL, MISA_VALUE,
                             MSTATUS_MIE, MSTATUS_MPIE, MSTATUS_MPP_M, Hart,
                             IllegalInstruction, Memory, RESET_PC)


def csr(f3, rd, addr, rs1):
    return ((addr & 0xFFF) << 20) | (rs1 << 15) | (f3 << 12) | (rd << 7) | 0x73


def addi(rd, rs1, imm):
    return ((imm & 0xFFF) << 20) | (rs1 << 15) | (rd << 7) | 0x13


ECALL, EBREAK, MRET, WFI = 0x73, 0x0010_0073, 0x3020_0073, 0x1050_0073


def hart(words, traps=True):
    mem = Memory(base=RESET_PC)
    mem.load_words(words, base=RESET_PC)
    return Hart(mem=mem, traps=traps)


def run(h, n):
    return [h.step() for _ in range(n)]


def test_field_masks_and_ignored_csrs():
    h = hart([addi(1, 0, -1),
              csr(1, 0, 0x300, 1), csr(2, 2, 0x300, 0),     # mstatus
              csr(1, 0, 0x305, 1), csr(2, 3, 0x305, 0),     # mtvec
              csr(1, 0, 0x341, 1), csr(2, 4, 0x341, 0),     # mepc
              csr(1, 0, 0x301, 0), csr(2, 5, 0x301, 0),     # misa: write ignored
              csr(1, 0, 0x304, 1), csr(2, 6, 0x304, 0)])    # mie: MTIE and MEIE only
    run(h, 11)
    assert h.x[2] == MSTATUS_MPP_M | MSTATUS_MPIE | MSTATUS_MIE
    assert h.x[3] == h.x[4] == (1 << 64) - 4
    assert h.x[5] == MISA_VALUE and h.x[6] == (1 << 7) | (1 << 11)   # mie (ADR 0015, 0017)


def test_minstret_counts_retired_and_write_is_not_counted():
    h = hart([csr(2, 1, 0xB02, 0), addi(0, 0, 0), csr(2, 2, 0xC02, 0),
              addi(3, 0, 50), csr(1, 0, 0xB02, 3), csr(2, 4, 0xB02, 0)])
    run(h, 6)
    assert h.x[2] - h.x[1] == 2
    assert h.x[4] == 50


@pytest.mark.parametrize("word,cause,tval", [
    (ECALL, CAUSE_ECALL_M, 0), (EBREAK, CAUSE_BREAKPOINT, 0),
    (0, CAUSE_ILLEGAL, 0), (csr(1, 0, 0xC00, 1), CAUSE_ILLEGAL, csr(1, 0, 0xC00, 1)),
    (csr(2, 5, 0x7C0, 0), CAUSE_ILLEGAL, csr(2, 5, 0x7C0, 0)),
    (0x4000_1033, CAUSE_ILLEGAL, 0x4000_1033),    # sll with funct7 0100000
])
def test_trap_and_mret(word, cause, tval):
    handler = RESET_PC + 0x40
    words = [0x0000_0097,                   # auipc x1, 0
             addi(1, 1, 0x40),
             csr(1, 0, 0x305, 1),           # mtvec = handler
             csr(6, 0, 0x300, 8),           # MIE = 1
             word]
    words += [addi(0, 0, 0)] * (16 - len(words))
    words += [csr(2, 10, 0x341, 0), addi(10, 10, 4), csr(1, 0, 0x341, 10), MRET]
    h = hart(words)
    recs = run(h, 5)
    assert recs[-1].get("trap") == cause and recs[-1]["rd"] is None
    assert h.pc == handler and h.mcause == cause and h.mtval == tval
    assert h.mepc == RESET_PC + 16
    assert h.mstatus == MSTATUS_MPP_M | MSTATUS_MPIE
    run(h, 4)
    assert h.pc == RESET_PC + 20
    assert h.mstatus == MSTATUS_MPP_M | MSTATUS_MPIE | MSTATUS_MIE


def test_trap_is_not_counted_in_minstret():
    h = hart([csr(2, 1, 0xB02, 0), ECALL])
    h.mtvec = RESET_PC + 8
    h.mem.load_words([csr(2, 2, 0xB02, 0)], base=RESET_PC + 8)
    run(h, 3)
    assert h.x[2] - h.x[1] == 1


def test_mlstat_is_write_1_to_clear():
    h = hart([addi(1, 0, 1), csr(3, 0, 0x801, 1), csr(2, 2, 0x801, 0),
              csr(2, 0, 0x801, 1), csr(2, 3, 0x801, 0)])
    h.mlstat = 1
    run(h, 5)
    assert h.x[2] == 1 and h.x[3] == 0


def test_wfi_is_a_nop_and_without_traps_bad_encodings_raise():
    h = hart([WFI, addi(1, 0, 7)])
    run(h, 2)
    assert h.x[1] == 7
    with pytest.raises(IllegalInstruction):
        hart([0], traps=False).step()


def test_mcycle_read_uses_the_cosim_override():
    h = hart([csr(2, 1, 0xC00, 0)])
    h.read_override = 1234
    h.step()
    assert h.x[1] == 1234


def test_timer_interrupt_and_mie():
    from rv64i import CAUSE_INTERRUPT, MIE_MEIE, MIE_MTIE
    h = hart([addi(1, 0, -1), csr(1, 0, 0x304, 1), csr(2, 2, 0x304, 0), addi(3, 0, 3)])
    h.mtvec = RESET_PC + 0x100
    h.mstatus |= MSTATUS_MIE
    run(h, 3)
    assert h.x[2] == MIE_MTIE | MIE_MEIE      # only MTIE and MEIE are writable
    rec = h.take_interrupt()
    assert rec["irq"] == CAUSE_INTERRUPT | 7 and rec["pc"] == RESET_PC + 12
    assert h.mepc == RESET_PC + 12 and h.mcause == CAUSE_INTERRUPT | 7 and h.mtval == 0
    assert h.pc == RESET_PC + 0x100 and h.mstatus == MSTATUS_MPP_M | MSTATUS_MPIE


def test_mip_read_uses_the_cosim_override():
    h = hart([csr(2, 1, 0x344, 0)])
    h.read_override = 0xFFFF
    h.step()
    assert h.x[1] == (1 << 7) | (1 << 11)


def test_pending_interrupt_priority_and_masks():
    """ADR 0017: external before timer; each needs its mie bit and mstatus.MIE."""
    from rv64i import IRQ_M_EXT, IRQ_M_TIMER, MIE_MEIE, MIE_MTIE
    h = hart([addi(1, 0, 0)])
    both = MIE_MTIE | MIE_MEIE              # mip uses the same bit positions
    h.mie = both
    assert h.pending_interrupt(both) is None          # mstatus.MIE clear
    h.mstatus |= MSTATUS_MIE
    assert h.pending_interrupt(both) == IRQ_M_EXT
    assert h.pending_interrupt(MIE_MTIE) == IRQ_M_TIMER
    assert h.pending_interrupt(0) is None
    h.mie = MIE_MTIE
    assert h.pending_interrupt(MIE_MEIE) is None
    assert h.pending_interrupt(both) == IRQ_M_TIMER
    rec = h.take_interrupt(IRQ_M_EXT)
    assert h.mcause == (1 << 63) | 11 and rec["irq"] == h.mcause


def _jal(rd, off):
    return (((off >> 20) & 1) << 31) | (((off >> 1) & 0x3FF) << 21) | (((off >> 11) & 1) << 20) \
        | (((off >> 12) & 0xFF) << 12) | (rd << 7) | 0x6F


@pytest.mark.parametrize("words,cause,tval_off", [
    ([addi(1, 0, 0), 0x0010_9183], 4, 1),           # lh x3, 1(x1)      (x1 = data base)
    ([addi(1, 0, 0), 0x0020_A183], 4, 2),           # lw x3, 2(x1)
    ([addi(1, 0, 0), 0x0040_B183], 4, 4),           # ld x3, 4(x1)
    ([addi(1, 0, 0), 0x0030_91A3], 6, 3),           # sh x3, 3(x1)
    ([addi(1, 0, 0), 0x0020_B323], 6, 6),           # sd x2, 6(x1)
])
def test_misaligned_load_store_traps(words, cause, tval_off):
    from rv64i import CAUSE_MISALIGNED_LOAD, CAUSE_MISALIGNED_STORE
    assert (cause, cause) in ((CAUSE_MISALIGNED_LOAD, 4), (CAUSE_MISALIGNED_STORE, 6))
    base = RESET_PC + 0x800
    h = hart([0x0000_0097, addi(1, 1, 0x7FC), addi(1, 1, 4)] + words[1:])
    h.mtvec = RESET_PC + 0x100
    run(h, 3)
    assert h.x[1] == base
    h.x[3] = 0x77
    h.mem.store(base, 8, 0x1122334455667788)
    rec = h.step()
    assert rec["trap"] == cause and rec["rd"] is None and rec["mem_write"] is None
    assert h.mcause == cause and h.mtval == base + tval_off and h.mepc == RESET_PC + 12
    assert h.pc == RESET_PC + 0x100 and h.x[3] == 0x77
    assert h.mem.load(base, 8) == 0x1122334455667788


def test_aligned_and_byte_accesses_do_not_trap():
    h = hart([0x0000_0097, addi(1, 1, 0x101),       # x1 = RESET_PC + 0x101 (odd)
              0x0000_8183,                          # lb x3, 0(x1)
              0x0030_8023,                          # sb x3, 0(x1)
              0xFFF0_C203])                         # lbu x4, -1(x1)
    run(h, 5)
    assert h.mcause == 0 and h.pc == RESET_PC + 20


@pytest.mark.parametrize("taken", [True, False])
def test_misaligned_branch_target_traps_only_if_taken(taken):
    from rv64i import CAUSE_MISALIGNED_FETCH
    beq_6 = (0 << 25) | (0 << 20) | (0 << 15) | (0 << 12) | (0b0011 << 8) | 0x63   # beq x0,x0,+6
    bne_6 = beq_6 | (1 << 12)                                                     # bne x0,x0,+6
    h = hart([beq_6 if taken else bne_6, addi(1, 0, 1)])
    h.mtvec = RESET_PC + 0x100
    rec = h.step()
    if taken:
        assert rec["trap"] == CAUSE_MISALIGNED_FETCH and h.mtval == RESET_PC + 6
        assert h.mepc == RESET_PC and h.pc == RESET_PC + 0x100
    else:
        assert "trap" not in rec and h.pc == RESET_PC + 4


def test_misaligned_jal_and_jalr_targets_trap_without_writing_rd():
    from rv64i import CAUSE_MISALIGNED_FETCH
    h = hart([_jal(5, 6)])
    h.mtvec = RESET_PC + 0x100
    rec = h.step()
    assert rec["trap"] == CAUSE_MISALIGNED_FETCH and rec["rd"] is None and h.x[5] == 0
    assert h.mtval == RESET_PC + 6 and h.mepc == RESET_PC
    # jalr clears bit 0 first: target +3 -> +2 is still misaligned, +1 -> +0 is fine
    h = hart([0x0000_0097, 0x0030_82E7])            # auipc x1,0; jalr x5, 3(x1)
    h.mtvec = RESET_PC + 0x100
    run(h, 2)
    assert h.mcause == CAUSE_MISALIGNED_FETCH and h.mtval == RESET_PC + 2 and h.x[5] == 0
    h = hart([0x0000_0097, 0x0090_82E7])            # jalr x5, 9(x1) -> RESET_PC + 8
    run(h, 2)
    assert h.pc == RESET_PC + 8 and h.x[5] == RESET_PC + 8


def test_without_traps_misaligned_accesses_still_run():
    h = hart([0x0000_0097, addi(1, 1, 0x102), 0x0000_A183], traps=False)   # lw x3, 0(x1)
    run(h, 3)
    assert h.pc == RESET_PC + 12
