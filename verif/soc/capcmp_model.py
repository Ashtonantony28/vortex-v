"""Cycle-accurate reference model of rtl/soc/vortex_capcmp.sv (ADR 0017).

Written from the register and rule description in the module header and ADR 0017,
not from the RTL body. `read(addr)` is the combinational register read in the
current state; `step(we, addr, wdata, cap_in)` applies one rising clock edge.
"""
R_TIME, R_PRESC, R_CTRL, R_CAP_WR, R_CAP_RD, R_CAP_DATA, R_CAP_OVF = 0x00, 0x08, 0x10, 0x18, 0x20, 0x28, 0x30
R_CMP_ARG, R_CMP_PUSH, R_CMP_STATUS, R_OUT, R_CMP_FLUSH = 0x38, 0x40, 0x48, 0x50, 0x58
ACT_PULSE, ACT_SET, ACT_CLEAR = 1, 2, 3
CTRL_IE_BIT = 16
DROP_MAX = (1 << 32) - 1
PRESC_MASK = 0xFFFF


class CapCmp:
    def __init__(self, tw=32, n_cap=2, depth=32, n_out=8, n_cmp=8, presc=49):
        self.tw, self.n_cap, self.depth, self.n_out, self.n_cmp = tw, n_cap, depth, n_out, n_cmp
        self.tmask = (1 << tw) - 1
        self.time = 0
        self.div = 0
        self.presc = presc
        self.edge_mode = [1] * n_cap
        self.cap_ie = 0
        self.arg = 0
        self.sync1 = self.sync2 = self.prev = 0
        self.held = [False] * n_cap
        self.held_ts = [0] * n_cap
        self.fifo = [(0, 0)] * depth          # (kind, ts)
        self.cap_wr = self.cap_rd = 0
        self.cap_ovf = 0
        self.slots = [None] * n_cmp           # (tick, arg, out, act) or None
        self.cmp_drop = 0
        self.out = [0] * n_out
        self.pulse_on = [False] * n_out
        self.pulse_end = [0] * n_out

    def _reached(self, t, target):
        """t - target >= 0 as a signed tw-bit value."""
        return ((t - target) & self.tmask) >> (self.tw - 1) == 0

    def count(self):
        return (self.cap_wr - self.cap_rd) & self.tmask

    def irq(self):
        return int(bool(self.cap_ie) and self.count() != 0)

    def outputs(self):
        return sum(b << i for i, b in enumerate(self.out))

    def pending(self):
        return sum(s is not None for s in self.slots)

    def read(self, addr):
        if addr == R_TIME:
            return self.time
        if addr == R_PRESC:
            return self.presc
        if addr == R_CTRL:
            return (self.cap_ie << CTRL_IE_BIT) | sum(m << (2 * c) for c, m in enumerate(self.edge_mode))
        if addr == R_CAP_WR:
            return self.cap_wr
        if addr == R_CAP_RD:
            return self.cap_rd
        if addr == R_CAP_DATA:
            if self.count() == 0:
                return 0
            kind, ts = self.fifo[self.cap_rd % self.depth]
            return (1 << 63) | (kind << 32) | ts
        if addr == R_CAP_OVF:
            return self.cap_ovf
        if addr == R_CMP_ARG:
            return self.arg
        if addr == R_CMP_STATUS:
            return (self.cmp_drop << 16) | self.pending()
        if addr == R_OUT:
            return self.outputs()
        return 0

    def step(self, we=0, addr=0, wdata=0, cap_in=0):
        """One rising edge with these register-port and pin inputs."""
        wr = (lambda a: bool(we) and addr == a)
        t, wr0, rd0, arg0 = self.time, self.cap_wr, self.cap_rd, self.arg

        # ---- values of this cycle, from the state before the edge ----
        edges = []
        for c in range(self.n_cap):
            m = self.edge_mode[c]
            s, p = (self.sync2 >> c) & 1, (self.prev >> c) & 1
            edges.append(bool((m & 1 and s and not p) or (m & 2 and not s and p)))
        push_ch = next((c for c in range(self.n_cap) if self.held[c]), None)
        full = self.count() == self.depth
        fire = [s is not None and self._reached(t, s[0]) for s in self.slots]
        free = next((k for k, s in enumerate(self.slots) if s is None), None)
        f_clear, f_pulse, f_set = set(), {}, set()
        for k, s in enumerate(self.slots):
            if fire[k]:
                _, arg, o, act = s
                if act == ACT_CLEAR:
                    f_clear.add(o)
                elif act == ACT_PULSE:
                    f_pulse[o] = arg              # highest slot's length wins
                elif act == ACT_SET:
                    f_set.add(o)
        pulse_done = [self.pulse_on[o] and self._reached(t, self.pulse_end[o]) for o in range(self.n_out)]

        # ---- timer and plain registers ----
        if wr(R_PRESC):
            self.presc, self.div = wdata & PRESC_MASK, 0
        elif self.div == self.presc:
            self.div, self.time = 0, (t + 1) & self.tmask
        else:
            self.div = (self.div + 1) & PRESC_MASK
        if wr(R_CTRL):
            self.edge_mode = [(wdata >> (2 * c)) & 3 for c in range(self.n_cap)]
            self.cap_ie = (wdata >> CTRL_IE_BIT) & 1
        if wr(R_CMP_ARG):
            self.arg = wdata & self.tmask

        # ---- capture ----
        self.prev, self.sync2, self.sync1 = self.sync2, self.sync1, cap_in
        drops = 0
        held = list(self.held)
        if push_ch is not None:
            held[push_ch] = False
            if full:
                drops += 1
            else:
                self.fifo[wr0 % self.depth] = (push_ch + 1, self.held_ts[push_ch])
                self.cap_wr = (wr0 + 1) & self.tmask
        for c in range(self.n_cap):
            if edges[c]:
                if not self.held[c] or push_ch == c:
                    held[c], self.held_ts[c] = True, t
                else:
                    drops += 1
        self.held = held
        self.cap_ovf = 0 if wr(R_CAP_OVF) else min(DROP_MAX, self.cap_ovf + drops)
        if wr(R_CAP_RD):
            new = wdata & self.tmask
            self.cap_rd = wr0 if ((new - rd0) & self.tmask) > ((wr0 - rd0) & self.tmask) else new

        # ---- compare ----
        for k in range(self.n_cmp):
            if fire[k]:
                self.slots[k] = None
        if wr(R_CMP_PUSH):
            o = (wdata >> 32) & 0xFF
            if free is not None and o < self.n_out:
                self.slots[free] = (wdata & self.tmask, arg0, o, (wdata >> 40) & 7)
            else:
                self.cmp_drop = min(DROP_MAX, self.cmp_drop + 1)
        if wr(R_CMP_STATUS):
            self.cmp_drop = 0
        if wr(R_CMP_FLUSH):
            self.slots = [None] * self.n_cmp
        for o in range(self.n_out):
            if o in f_clear:
                self.out[o], self.pulse_on[o] = 0, False
            elif o in f_pulse:
                self.out[o], self.pulse_on[o] = 1, True
                self.pulse_end[o] = (t + f_pulse[o]) & self.tmask
            elif o in f_set:
                self.out[o], self.pulse_on[o] = 1, False
            elif pulse_done[o]:
                self.out[o], self.pulse_on[o] = 0, False
        if wr(R_OUT):
            self.out = [(wdata >> o) & 1 for o in range(self.n_out)]
            self.pulse_on = [False] * self.n_out
