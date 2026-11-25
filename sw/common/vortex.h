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

// Interrupts. crt0 calls irq_handler(mcause) for every interrupt (before ADR 0017
// it was irq_handler(void): add the argument when porting older firmware); mcause is
// MCAUSE_TIMER or MCAUSE_EXT. Both are level-triggered, so the handler must remove
// the cause before it returns, or it is taken again at once.
// Machine timer (ADR 0015): pending while CYCLE >= MTIMECMP; re-arm or disarm it.
#define CSR_SET(csr, v)   __asm__ volatile ("csrs " #csr ", %0" :: "r"((uint64_t)(v)))
#define CSR_CLEAR(csr, v) __asm__ volatile ("csrc " #csr ", %0" :: "r"((uint64_t)(v)))
#define MIE_MTIE      (1u << 7)
#define MIE_MEIE      (1u << 11)
#define MSTATUS_MIE   (1u << 3)
#define MCAUSE_TIMER  ((1ULL << 63) | 7)
#define MCAUSE_EXT    ((1ULL << 63) | 11)
void irq_handler(uint64_t mcause);
static inline void timer_arm(uint64_t delta) { MTIMECMP = CYCLE + delta; }
static inline void timer_disarm(void)       { MTIMECMP = ~0ULL; }
static inline void irq_enable(void)  { CSR_SET(0x304, MIE_MTIE); CSR_SET(0x300, MSTATUS_MIE); }
static inline void irq_disable(void) { CSR_CLEAR(0x300, MSTATUS_MIE); }
// Machine external interrupt (ADR 0017): the capture/compare timer's capture interrupt
static inline void ext_irq_enable(void)  { CSR_SET(0x304, MIE_MEIE); CSR_SET(0x300, MSTATUS_MIE); }
static inline void ext_irq_disable(void) { CSR_CLEAR(0x304, MIE_MEIE); }

// Capture/compare timer (ADR 0017; SoC built with CAPCMP=1). Register map and
// rules: rtl/soc/vortex_capcmp.sv. Use 64-bit accesses only. Its capture
// interrupt is the machine external interrupt: pending while CC_CTRL.CAP_IE is
// set and the capture FIFO is not empty; pop entries to clear it.
#define CC_BASE       (MMIO_BASE + 0x80)
#define CC_REG(off)   (*(volatile uint64_t *)(CC_BASE + (off)))
#define CC_TIME       CC_REG(0x00)   // R   timer, 1 tick = CC_PRESC+1 clocks
#define CC_PRESC      CC_REG(0x08)   // RW
#define CC_CTRL       CC_REG(0x10)   // RW  2 bits per capture channel, bit 16 CAP_IE
#define CC_CAP_WR     CC_REG(0x18)   // R
#define CC_CAP_RD     CC_REG(0x20)   // RW  write CC_CAP_RD + n to pop n entries
#define CC_CAP_DATA   CC_REG(0x28)   // R   bit 63 valid, 39:32 kind, 31:0 tick
#define CC_CAP_OVF    CC_REG(0x30)   // RW  dropped edges; write to clear
#define CC_CMP_ARG    CC_REG(0x38)   // RW  pulse length for the next push
#define CC_CMP_PUSH   CC_REG(0x40)   // W   tick | out << 32 | action << 40
#define CC_CMP_STATUS CC_REG(0x48)   // R   7:0 pending, 47:16 dropped pushes; write to clear
#define CC_OUT        CC_REG(0x50)   // RW  output levels; a write cancels pulses
#define CC_CMP_FLUSH  CC_REG(0x58)   // W   cancel all pending compares
#define CC_EDGE_OFF     0u
#define CC_EDGE_RISE    1u
#define CC_EDGE_FALL    2u
#define CC_EDGE_BOTH    3u
#define CC_CTRL_CAP_IE  (1u << 16)
#define CC_ACT_PULSE    1u
#define CC_ACT_SET      2u
#define CC_ACT_CLEAR    3u
#define CC_CAP_VALID(e) ((e) >> 63)
#define CC_CAP_KIND(e)  ((uint32_t)((e) >> 32) & 0xff)   // capture channel + 1
#define CC_CAP_TICK(e)  ((uint32_t)(e))
static inline void cc_push(uint32_t tick, unsigned out, unsigned action) {
    CC_CMP_PUSH = (uint64_t)tick | ((uint64_t)out << 32) | ((uint64_t)action << 40);
}
static inline void cc_pulse(uint32_t tick, unsigned out, uint32_t len) {
    CC_CMP_ARG = len;
    cc_push(tick, out, CC_ACT_PULSE);
}
// Ticks are wrap-safe: compare them as signed differences
static inline int32_t cc_diff(uint32_t a, uint32_t b) { return (int32_t)(a - b); }
#endif
