"""NeuroFlight's int8 hover policy as an RV64IM + Xmlacc program for Vortex-V.

The policy (19 -> 64 -> 64 -> 4 MLP, tanh after every layer) follows NeuroFlight's
int8 contract, docs/INTERFACES.md section 7 in neuroflight-fc. Per output j:

    acc = sat_int32(b_q[j] + sum_i W_q[j,i] * x_q[i])
    hidden:  y = clamp(rshift_round(acc * M[j], S[j]), -128, 127);  a = lut[y + 128]
    last:    acc is the output (tanh and scaling run in float32 on the host)

The integer path maps onto existing instructions with no new opcodes:

    sum_i W*x            mlacc.macc   8 int8 lanes per instruction, accumulator in rd/rs3
    acc * M[j]           mul          int32 x int32 fits in 64 bits exactly
    rshift_round + clamp mlacc.rqs    rqs rounds half-up with an arithmetic shift and
                                      clamps to int8: the same arithmetic as NeuroFlight's
                                      rshift_round (rqs's 65-bit add, Vortex-V PR #2)
    lut[y + 128]         lb

The float32 input normalisation (obs -> x_q) and the final tanh stay on the host:
Vortex-V has no FPU. The program therefore starts from x_q and ends at last_acc,
which is the full integer path that NeuroFlight's firmware checks bit for bit.
"""
from pathlib import Path
import json
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
CORE_DIR = ROOT / "verif" / "core"
for _p in (str(CORE_DIR), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from rvasm import Asm, MASK64, RESET_PC, TOHOST_ADDR  # noqa: E402

FIXTURE = HERE / "fixtures" / "hover_ctbr_v3_int8.json"
NF_BASE = 0x8000_4000          # weights and buffers live above the shared data region
LANES = 8                      # int8 lanes per 64-bit register (XMLACC-SPEC-v1)
INT8_OFFSET = 128              # LUT index = y + 128 (INTERFACES.md section 7, step 4)
ACC_BYTES = 4                  # last_acc is int32

# register names (RISC-V ABI numbers)
RA, T0, T1, T2, S2, S3, S4, S5, S6, S7, S8 = 1, 5, 6, 7, 18, 19, 20, 21, 22, 23, 24
A0, A1, A2, A3, A4, A5, A6, A7, T3, T4, T5 = 10, 11, 12, 13, 14, 15, 16, 17, 28, 29, 30


def load_fixture(path=FIXTURE):
    return json.loads(Path(path).read_text())


def _pad(n):
    return -(-n // LANES) * LANES


class Layout:
    """Byte addresses of every array in the data image."""

    def __init__(self, layers, n_vec):
        self.n_vec = n_vec
        self.dims = [(len(L["W_q"]), len(L["W_q"][0])) for L in layers]   # (out, in)
        addr = NF_BASE
        self.layer = []
        for l, (n_out, n_in) in enumerate(self.dims):
            hidden = "M" in layers[l]
            d = {"n_out": n_out, "n_in": n_in, "in_words": _pad(n_in) // LANES, "hidden": hidden}
            for name, size in (("W", n_out * _pad(n_in)), ("b", 4 * n_out),
                               ("M", 4 * n_out if hidden else 0), ("S", n_out if hidden else 0),
                               ("lut", 256 if hidden else 0)):
                d[name] = addr if size else 0
                addr = _pad(addr + size)
            self.layer.append(d)
        for l, (n_out, n_in) in enumerate(self.dims):
            assert l == 0 or _pad(n_in) == n_in, "hidden widths must be multiples of 8"
        self.x = addr
        self.x_stride = _pad(self.dims[0][1])
        addr += n_vec * self.x_stride
        self.hid = []                        # one buffer per hidden layer, n_vec rows
        for (n_out, _), d in zip(self.dims, self.layer):
            if d["hidden"]:
                self.hid.append((addr, n_out))
                addr = _pad(addr + n_vec * n_out)
        self.out = addr
        self.out_stride = ACC_BYTES * self.dims[-1][0]
        addr += n_vec * self.out_stride
        self.end = addr


def _bytes_to_data(base, raw):
    """(addr, 8, value) tuples for the harness/ISS loaders."""
    raw = bytes(raw) + b"\0" * (-len(raw) % 8)
    return [(base + i, 8, int.from_bytes(raw[i:i + 8], "little")) for i in range(0, len(raw), 8)]


def _i8(v):
    return bytes((x & 0xFF) for x in v)


def _i32(v):
    return b"".join((x & 0xFFFF_FFFF).to_bytes(4, "little") for x in v)


def data_image(fix, vectors, lay):
    data = []
    for L, d in zip(fix["layers"], lay.layer):
        w = b"".join(_i8(row) + b"\0" * (_pad(d["n_in"]) - d["n_in"]) for row in L["W_q"])
        data += _bytes_to_data(d["W"], w) + _bytes_to_data(d["b"], _i32(L["b_q"]))
        if d["hidden"]:
            data += _bytes_to_data(d["M"], _i32(L["M"])) + _bytes_to_data(d["S"], bytes(L["S"]))
            data += _bytes_to_data(d["lut"], _i8(L["lut"]))
    x = b"".join(_i8(v["x_q"]) + b"\0" * (lay.x_stride - len(v["x_q"])) for v in vectors)
    return data + _bytes_to_data(lay.x, x)


def program(lay, mutant=None):
    """Assemble the batch loop and the layer subroutine.

    `mutant="floor"` replaces rqs with a plain arithmetic shift (no rounding, no clamp);
    the tests use it to show the fixtures catch a wrong requantize rule.
    `mutant="scalar"` replaces macc with plain RV64IM byte loads, multiplies and adds,
    as the baseline for the Xmlacc speed-up.
    """
    a = Asm()
    a.addi(S3, 0, 0)                         # vector counter
    a.addi(S4, 0, lay.n_vec)
    a.la(S5, lay.x)
    a.la(S6, lay.hid[0][0])
    a.la(S7, lay.hid[1][0])
    a.la(S8, lay.out)
    a.label("vec")
    srcs = [S5, S6, S7]
    dsts = [S6, S7, S8]
    for l, d in enumerate(lay.layer):
        a.addi(A0, srcs[l], 0)
        a.la(A1, d["W"])
        a.la(A2, d["b"])
        if d["hidden"]:
            a.la(A3, d["M"])
            a.la(A4, d["S"])
            a.la(A5, d["lut"])
        else:
            a.addi(A3, 0, 0)                 # M == 0 selects the int32 output path
        a.addi(A6, dsts[l], 0)
        a.addi(A7, 0, d["n_out"])
        a.addi(T3, 0, d["in_words"])
        a.jal(RA, "layer")
    a.addi(S5, S5, lay.x_stride)
    a.addi(S6, S6, lay.hid[0][1])
    a.addi(S7, S7, lay.hid[1][1])
    a.addi(S8, S8, lay.out_stride)
    a.addi(S3, S3, 1)
    a.bne(S3, S4, "vec")
    a.la(T0, TOHOST_ADDR)
    a.addi(T1, 0, 1)
    a.sw(T1, T0, 0)                          # halt PASS
    a.label("spin")
    a.jal(0, "spin")

    # layer(a0=x, a1=W, a2=b, a3=M|0, a4=S, a5=lut, a6=out, a7=n_out, t3=in_words)
    a.label("layer")
    a.addi(S2, 0, 0)
    a.label("row")
    a.lw(T0, A2, 0)                          # acc = b_q[j]
    a.addi(T1, A0, 0)
    a.addi(T2, T3, 0)
    a.label("dot")
    if mutant == "scalar":                   # RV64IM-only baseline: 8 x (lb, lb, mul, add)
        for lane in range(LANES):
            a.lb(T4, T1, lane)
            a.lb(T5, A1, lane)
            a.mul(T4, T4, T5)
            a.add(T0, T0, T4)
    else:
        a.ld(T4, T1, 0)                      # 8 activations
        a.ld(T5, A1, 0)                      # 8 weights of row j
        a.macc(T0, T4, T5, T0)               # acc += sum of 8 int8 products
    a.addi(T1, T1, LANES)
    a.addi(A1, A1, LANES)
    a.addi(T2, T2, -1)
    a.bne(T2, 0, "dot")
    a.addiw(T4, T0, 0)                       # sat_int32: in range iff sext32(acc) == acc
    a.beq(T4, T0, "inrange")
    a.blt(T0, 0, "satneg")
    a.lui(T0, 0x80000)
    a.addiw(T0, T0, -1)                      # INT32_MAX
    a.jal(0, "inrange")
    a.label("satneg")
    a.lui(T0, 0x80000)                       # INT32_MIN
    a.label("inrange")
    a.beq(A3, 0, "last")
    a.lw(T4, A3, 0)
    a.mul(T0, T0, T4)                        # acc * M[j], exact in 64 bits
    a.lbu(T4, A4, 0)
    if mutant == "floor":
        a.sra(T0, T0, T4)
    else:
        a.rqs(T0, T0, T4)                    # clamp(rshift_round(., S[j]), -128, 127)
    a.add(T0, T0, A5)
    a.lb(T0, T0, INT8_OFFSET)                # tanh LUT, index y + 128
    a.sb(T0, A6, 0)
    a.addi(A6, A6, 1)
    a.addi(A3, A3, 4)
    a.addi(A4, A4, 1)
    a.jal(0, "next")
    a.label("last")
    a.sw(T0, A6, 0)
    a.addi(A6, A6, ACC_BYTES)
    a.label("next")
    a.addi(A2, A2, 4)
    a.addi(S2, S2, 1)
    a.bne(S2, A7, "row")
    a.jalr(0, RA, 0)
    return a


def build(fix, vectors, mutant=None):
    """(words, data, layout) for a batch of fixture vectors."""
    lay = Layout(fix["layers"], len(vectors))
    return program(lay, mutant).assemble(), data_image(fix, vectors, lay), lay


def decode(load, lay):
    """Read hidden activations and last_acc back with `load(addr, nbytes) -> unsigned`."""
    def s(v, bits):
        return v - (1 << bits) if v >> (bits - 1) else v
    out = []
    for k in range(lay.n_vec):
        hidden = [[s(load(base + k * n + i, 1), 8) for i in range(n)] for base, n in lay.hid]
        n_act = lay.dims[-1][0]
        acc = [s(load(lay.out + k * lay.out_stride + ACC_BYTES * i, ACC_BYTES), 32) for i in range(n_act)]
        out.append({"hidden": hidden, "last_acc": acc})
    return out


def run_on_iss(fix, vectors, max_steps=2_000_000, mutant=None):
    """Run the kernel on the golden ISS. Returns (decoded outputs, hart)."""
    from model.iss.rv64i import Hart, Memory
    words, data, lay = build(fix, vectors, mutant)
    mem = Memory(base=RESET_PC)
    mem.load_words(words, base=RESET_PC)
    for addr, n, v in data:
        mem.store(addr, n, v)
    hart = Hart(mem=mem, reset_pc=RESET_PC, tohost_addr=TOHOST_ADDR)
    r = hart.run(max_steps=max_steps)
    assert r["halted"] and r["halt_code"] == (True, 0), f"ISS did not PASS: {r}"
    return decode(mem.load, lay), hart


def reference(fix, x_q):
    """Independent plain-int restatement of INTERFACES.md section 7 (integer part)."""
    x = list(x_q)
    hidden = []
    for L in fix["layers"]:
        accs = []
        for j, row in enumerate(L["W_q"]):
            acc = L["b_q"][j] + sum(w * xi for w, xi in zip(row, x))
            accs.append(max(-(1 << 31), min((1 << 31) - 1, acc)))
        if "M" not in L:
            return {"hidden": hidden, "last_acc": accs}
        y = [max(-128, min(127, (acc * m + (1 << (s - 1))) >> s))
             for acc, m, s in zip(accs, L["M"], L["S"])]
        x = [L["lut"][v + INT8_OFFSET] for v in y]
        hidden.append(x)
