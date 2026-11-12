// Vortex-V SoC smoke test (ADR 0011): UART output, an int8 dot product on the
// Xmlacc unit checked against plain C, the LEDs, and the cycle counter.
#include "vortex.h"

#define N 32                              // int8 elements per vector

static const int8_t a[N] = {
    127, -128, 1, -1, 2, 3, -2, 127, 5, -7, 9, -11, 13, -15, 17, -19,
    -100, 100, -50, 50, -25, 25, -12, 12, 64, -64, 32, -32, 16, -16, 8, -8};
static const int8_t b[N] = {
    1, 2, 3, 4, 5, 6, 7, 8, -1, -2, -3, -4, -5, -6, -7, -8,
    127, 127, -128, -128, 1, 1, 1, 1, 3, 5, 7, 9, 11, 13, 15, 17};

static uint64_t pack8(const int8_t *p) {
    uint64_t v = 0;
    for (int i = 7; i >= 0; i--) v = (v << 8) | (uint8_t)p[i];
    return v;
}

// A device read must see every older device write: here the UART must already
// be busy with the byte just written, even though that store waits to commit
// behind a slow divide (ADR 0011: device loads run only from the queue head).
static int mmio_ordered(void) {
    while (UART_STATUS & 1) { }
    int64_t q, n = 100000, d = 7;
    __asm__ volatile ("div %0, %1, %2" : "=r"(q) : "r"(n), "r"(d));
    UART_TX = '.';
    uint64_t busy = UART_STATUS & 1;
    return busy && q == 14285;
}

int main(void) {
    uart_puts("Vortex-V SoC: hello\n");
    int order_ok = mmio_ordered();
    uart_puts(order_ok ? " mmio order ok\n" : " mmio order BROKEN\n");

    int64_t ref = 0;
    for (int i = 0; i < N; i++) ref += (int64_t)a[i] * b[i];

    int64_t acc = 0;
    for (int i = 0; i < N; i += 8) acc = xm_macc(pack8(a + i), pack8(b + i), acc);
    int64_t first = xm_dot8(pack8(a), pack8(b));
    int64_t first_ref = 0;
    for (int i = 0; i < 8; i++) first_ref += (int64_t)a[i] * b[i];

    int ok = (acc == ref) && (first == first_ref);
    uart_puts("dot32 = ");
    uart_puthex((uint64_t)acc);
    uart_puts(ok ? " xmlacc ok\n" : " xmlacc MISMATCH\n");

    LEDS = ok ? 0xA : 0x5;
    uart_puts("cycles = ");
    uart_puthex(CYCLE);
    uart_putc('\n');
    return (ok && order_ok) ? 0 : 1;
}
