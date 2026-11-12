// Vortex-V SoC bare-metal helpers (ADR 0011). Register map: rtl/soc/vortex_soc.sv.
#ifndef VORTEX_H
#define VORTEX_H
#include <stdint.h>

#define MMIO_BASE    0x10000000UL
#define UART_TX      (*(volatile uint64_t *)(MMIO_BASE + 0x00))
#define UART_STATUS  (*(volatile uint64_t *)(MMIO_BASE + 0x08))
#define LEDS         (*(volatile uint64_t *)(MMIO_BASE + 0x10))
#define CYCLE        (*(volatile uint64_t *)(MMIO_BASE + 0x18))

static inline void uart_putc(char c) {
    while (UART_STATUS & 1) { }
    UART_TX = (uint8_t)c;
}

static inline void uart_puts(const char *s) {
    while (*s) uart_putc(*s++);
}

static inline void uart_puthex(uint64_t v) {
    for (int i = 60; i >= 0; i -= 4) uart_putc("0123456789abcdef"[(v >> i) & 0xf]);
}

// Xmlacc Tier-1 (docs/XMLACC-SPEC-v1.md), CUSTOM-0 opcode, emitted with .insn
static inline int64_t xm_dot8(uint64_t a, uint64_t b) {
    int64_t r;
    __asm__ volatile (".insn r 0x0b, 0, 0, %0, %1, %2" : "=r"(r) : "r"(a), "r"(b));
    return r;
}

static inline int64_t xm_macc(uint64_t a, uint64_t b, int64_t acc) {
    int64_t r;
    __asm__ volatile (".insn r4 0x0b, 4, 0, %0, %1, %2, %3" : "=r"(r) : "r"(a), "r"(b), "r"(acc));
    return r;
}
#endif
