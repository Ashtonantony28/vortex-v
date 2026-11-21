# Vortex-V SoC start-up: set the stack, clear .bss, call main, report to tohost.
# main's return value 0 means pass (tohost = 1); anything else fails with that code.
# Any trap (ecall, ebreak, illegal instruction; ADR 0014) lands in trap_entry, which
# lights all four LEDs and fails with code 0x100 + mcause. A timer interrupt
# (ADR 0015) instead calls irq_handler() and returns.
    .section .text.init
    .globl _start
_start:
    la    t0, trap_entry
    csrw  mtvec, t0
    la    sp, _stack_top
    la    t0, _bss_start
    la    t1, _bss_end
1:  bgeu  t0, t1, 2f
    sd    zero, 0(t0)
    addi  t0, t0, 8
    j     1b
2:  call  main
    slli  a0, a0, 1
    ori   a0, a0, 1            # tohost: 1 = pass, (code << 1) | 1 = fail
    li    t0, 0x80001000
    sw    a0, 0(t0)
3:  j     3b

    .align 2
# A machine timer interrupt (mcause bit 63; ADR 0015) saves the caller-saved
# registers, calls irq_handler() and returns with mret. Any other trap fails.
trap_entry:
    addi  sp, sp, -128
    sd    ra, 0(sp)
    sd    t0, 8(sp)
    sd    t1, 16(sp)
    sd    t2, 24(sp)
    sd    a0, 32(sp)
    sd    a1, 40(sp)
    sd    a2, 48(sp)
    sd    a3, 56(sp)
    sd    a4, 64(sp)
    sd    a5, 72(sp)
    sd    a6, 80(sp)
    sd    a7, 88(sp)
    sd    t3, 96(sp)
    sd    t4, 104(sp)
    sd    t5, 112(sp)
    sd    t6, 120(sp)
    csrr  t0, mcause
    bgez  t0, trap_fail
    call  irq_handler
    ld    ra, 0(sp)
    ld    t0, 8(sp)
    ld    t1, 16(sp)
    ld    t2, 24(sp)
    ld    a0, 32(sp)
    ld    a1, 40(sp)
    ld    a2, 48(sp)
    ld    a3, 56(sp)
    ld    a4, 64(sp)
    ld    a5, 72(sp)
    ld    a6, 80(sp)
    ld    a7, 88(sp)
    ld    t3, 96(sp)
    ld    t4, 104(sp)
    ld    t5, 112(sp)
    ld    t6, 120(sp)
    addi  sp, sp, 128
    mret

trap_fail:
    li    t0, 0x10000010       # LEDS
    li    t1, 0xf
    sd    t1, 0(t0)
    csrr  a0, mcause
    addi  a0, a0, 0x100
    slli  a0, a0, 1
    ori   a0, a0, 1
    li    t0, 0x80001000
    sw    a0, 0(t0)
4:  j     4b

# Default handler for a program that enables the timer but defines none:
# disarm the timer (MTIMECMP = all ones) so the interrupt stops.
    .weak irq_handler
irq_handler:
    li    t0, 0x10000020
    li    t1, -1
    sd    t1, 0(t0)
    ret
