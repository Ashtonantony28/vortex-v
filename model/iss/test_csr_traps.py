"""ISS Zicsr and machine-mode traps (ADR 0014). Run: python3 -m pytest model/ -q"""
import pytest

from model.iss.rv64i import (CAUSE_BREAKPOINT, CAUSE_ECALL_M, CAUSE_ILLEGAL, MISA_VALUE,
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
              csr(1, 0, 0x304, 1), csr(2, 6, 0x304, 0)])    # mie: reads 0
    run(h, 11)
    assert h.x[2] == MSTATUS_MPP_M | MSTATUS_MPIE | MSTATUS_MIE
    assert h.x[3] == h.x[4] == (1 << 64) - 4
    assert h.x[5] == MISA_VALUE and h.x[6] == 0


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
    h.cycle_override = 1234
    h.step()
    assert h.x[1] == 1234
