# Vortex-V SoC start-up: set the stack, clear .bss, call main, report to tohost.
# main's return value 0 means pass (tohost = 1); anything else fails with that code.
    .section .text.init
    .globl _start
_start:
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
