// Vortex-V SoC bare-metal helpers (ADR 0011). Register map: rtl/soc/vortex_soc.sv.
#ifndef VORTEX_H
#define VORTEX_H
#include <stdint.h>

#define MMIO_BASE    0x10000000UL
#define UART_TX      (*(volatile uint64_t *)(MMIO_BASE + 0x00))
#define UART_STATUS  (*(volatile uint64_t *)(MMIO_BASE + 0x08))
#define LEDS         (*(volatile uint64_t *)(MMIO_BASE + 0x10))
#define CYCLE        (*(volatile uint64_t *)(MMIO_BASE + 0x18))   // also mtime
#define MTIMECMP     (*(volatile uint64_t *)(MMIO_BASE + 0x20))   // ADR 0015

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

// mlacc.rqs: requantize int32 -> int8 with round/shift/clamp; sets mlstat.SAT on a clamp
static inline int64_t xm_rqs(int64_t x, uint64_t shift) {
    int64_t r;
    __asm__ volatile (".insn r 0x0b, 5, 0, %0, %1, %2" : "=r"(r) : "r"(x), "r"(shift));
    return r;
}

static inline int64_t xm_macc(uint64_t a, uint64_t b, int64_t acc) {
    int64_t r;
    __asm__ volatile (".insn r4 0x0b, 4, 0, %0, %1, %2, %3" : "=r"(r) : "r"(a), "r"(b), "r"(acc));
    return r;
}

// Zicsr (ADR 0014). CSR numbers are written as literals so any assembler accepts them.
#define CSR_READ(csr) ({ uint64_t v_; __asm__ volatile ("csrr %0, " #csr : "=r"(v_)); v_; })
#define CSR_WRITE(csr, v) __asm__ volatile ("csrw " #csr ", %0" :: "r"((uint64_t)(v)))

static inline uint64_t rdcycle(void)   { return CSR_READ(0xc00); }
static inline uint64_t rdinstret(void) { return CSR_READ(0xc02); }
static inline uint64_t mlstat_read(void) { return CSR_READ(0x801); }
static inline void mlstat_clear(void)  { CSR_WRITE(0x801, 1); }   // SAT is write-1-to-clear

// Machine timer interrupt (ADR 0015). The interrupt is pending while
// CYCLE >= MTIMECMP; crt0 calls irq_handler(), which must re-arm or disarm
// the timer (write MTIMECMP) before it returns, or it is taken again at once.
#define CSR_SET(csr, v)   __asm__ volatile ("csrs " #csr ", %0" :: "r"((uint64_t)(v)))
#define CSR_CLEAR(csr, v) __asm__ volatile ("csrc " #csr ", %0" :: "r"((uint64_t)(v)))
#define MIE_MTIE      (1u << 7)
#define MSTATUS_MIE   (1u << 3)
void irq_handler(void);
static inline void timer_arm(uint64_t delta) { MTIMECMP = CYCLE + delta; }
static inline void timer_disarm(void)       { MTIMECMP = ~0ULL; }
static inline void irq_enable(void)  { CSR_SET(0x304, MIE_MTIE); CSR_SET(0x300, MSTATUS_MIE); }
static inline void irq_disable(void) { CSR_CLEAR(0x300, MSTATUS_MIE); }
#endif
