// Capture/compare timer demo and test (ADR 0017): an engine-control loop in
// miniature, run by verif/soc/test_soc.py on a CAPCMP=1 SoC.
//
// The bench drives crank edges (capture channel 0) and one cam edge (channel 1)
// once LEDS reads 1. Each capture raises the machine external interrupt; the
// handler drains the FIFO and, for every crank edge at tick ts, schedules in
// hardware what Redline schedules per tooth:
//   coil 4 + i%4: dwell (set) at ts + DWELL_AT, spark (clear) at ts + SPARK_AT
//   injector i%4: a pulse at ts + INJ_AT lasting INJ_LEN ticks
// The bench checks every output edge lands on exactly that tick. Before the
// edges, one pulse is armed 5 ticks in the past to show a late compare fires at
// once. main prints what it captured; the bench compares it with what it drove.
#include "vortex.h"

#define N_EVENTS  5          // 4 crank edges + 1 cam edge
#define DWELL_AT  120
#define INJ_AT    150
#define INJ_LEN   40
#define SPARK_AT  200
#define LATE_OUT  3
#define LATE_LEN  10
#define TIMEOUT   400000     // cycles

static volatile int n_events, n_irqs, n_crank;
static volatile uint64_t events[N_EVENTS];

void irq_handler(uint64_t mcause) {
    if (mcause != MCAUSE_EXT) return;
    n_irqs++;
    uint64_t e;
    while (CC_CAP_VALID(e = CC_CAP_DATA)) {
        CC_CAP_RD = CC_CAP_RD + 1;           // pop (reads never pop)
        if (n_events < N_EVENTS) events[n_events++] = e;
        if (CC_CAP_KIND(e) == 1) {           // crank
            uint32_t ts = CC_CAP_TICK(e);
            unsigned i = n_crank++ & 3;
            cc_push(ts + DWELL_AT, 4 + i, CC_ACT_SET);
            cc_pulse(ts + INJ_AT, i, INJ_LEN);
            cc_push(ts + SPARK_AT, 4 + i, CC_ACT_CLEAR);
        }
    }
}

int main(void) {
    uart_puts("capcmp\n");
    uint32_t t0 = (uint32_t)CC_TIME;
    cc_pulse(t0 - 5, LATE_OUT, LATE_LEN);   // already due: fires at once
    while (CC_CMP_STATUS & 0xff) { }         // fired (the output rises in the same clock)
    while (CC_OUT) { }                       // and ended, before the bench starts
    CC_CTRL = (CC_EDGE_RISE << 0) | (CC_EDGE_RISE << 2) | CC_CTRL_CAP_IE;
    ext_irq_enable();
    LEDS = 1;                                // ready for edges

    uint64_t start = CYCLE;
    while (n_events < N_EVENTS && CYCLE - start < TIMEOUT) { }
    while ((CC_CMP_STATUS & 0xff) && CYCLE - start < TIMEOUT) { }   // compares fired
    while (CC_OUT && CYCLE - start < TIMEOUT) { }                   // pulses ended
    irq_disable();
    LEDS = 2;

    uart_puts("late ");
    uart_puthex(t0);
    uart_putc('\n');
    for (int i = 0; i < n_events; i++) {
        uart_puts("cap ");
        uart_puthex(events[i]);
        uart_putc('\n');
    }
    int ok = n_events == N_EVENTS && n_irqs >= 1 && CC_CAP_OVF == 0 && CC_CMP_STATUS == 0
          && !CC_CAP_VALID(CC_CAP_DATA) && CC_OUT == 0;
    uart_puts(ok ? " capcmp ok\n" : " capcmp BROKEN\n");
    return ok ? 0 : 1;
}
