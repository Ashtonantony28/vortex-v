// Minimal CSR-free riscv-tests environment for Vortex-V (machine mode, no traps).
// Pass/fail via a store to `tohost` (1 = pass, (TESTNUM<<1)|1 = fail), matching
// the core's halt convention (docs/M1-INTERFACE-v1.md).
#ifndef VORTEX_RISCV_TEST_H
#define VORTEX_RISCV_TEST_H

#define TESTNUM gp

#define RVTEST_RV64U
#define RVTEST_RV64UM

#define RVTEST_CODE_BEGIN                                               \
        .section .text.init;                                            \
        .globl _start;                                                  \
_start: j reset_vector;                                                 \
        .text;                                                          \
reset_vector:                                                           \
        li TESTNUM, 0;

#define RVTEST_CODE_END                                                 \
        unimp

#define RVTEST_PASS                                                     \
        fence;                                                          \
        li TESTNUM, 1;                                                  \
        la t5, tohost;                                                  \
        sw TESTNUM, 0(t5);                                              \
1:      j 1b;

#define RVTEST_FAIL                                                     \
        fence;                                                          \
1:      beqz TESTNUM, 1b;                                               \
        sll TESTNUM, TESTNUM, 1;                                        \
        or TESTNUM, TESTNUM, 1;                                         \
        la t5, tohost;                                                  \
        sw TESTNUM, 0(t5);                                              \
2:      j 2b;

#define EXTRA_DATA

#define RVTEST_DATA_BEGIN                                               \
        EXTRA_DATA                                                      \
        .pushsection .tohost,"aw",@progbits;                            \
        .align 3; .global tohost; tohost: .dword 0;                     \
        .align 3; .global fromhost; fromhost: .dword 0;                 \
        .popsection;                                                    \
        .align 4; .global begin_signature; begin_signature:

#define RVTEST_DATA_END .align 4; .global end_signature; end_signature:

#endif
