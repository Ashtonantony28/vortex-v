"""cocotb run harness for Vortex-V cores: memory model, program runner, co-sim.

Any core with the docs/M1-INTERFACE-v1.md ports (vortex_core, and later
vortex_ooo_core) can be driven by these helpers.
"""
import os

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, FallingEdge, Timer

from rvasm import MASK64, RESET_PC, TOHOST_ADDR

# ===========================================================================
# Memory model + run harness
# ===========================================================================
def _safe_int(sig, default=None):
    try:
        return int(sig.value)
    except Exception:
        return default


def _rd_word(mem, addr):
    """Read a 32-bit little-endian instruction word."""
    return (mem.get(addr, 0) | (mem.get(addr + 1, 0) << 8)
            | (mem.get(addr + 2, 0) << 16) | (mem.get(addr + 3, 0) << 24))


def _rd_bytes(mem, addr, n):
    v = 0
    for i in range(n):
        v |= mem.get(addr + i, 0) << (8 * i)
    return v & MASK64


def _wr_bytes(mem, addr, n, data):
    for i in range(n):
        mem[addr + i] = (data >> (8 * i)) & 0xFF


def _load_program(words, base=RESET_PC, data=None):
    mem = {}
    addr = base
    for w in words:
        _wr_bytes(mem, addr, 4, w & 0xFFFF_FFFF)
        addr += 4
    # `data` seeds the data region: list of (addr, nbytes, value) tuples.
    if data:
        for a, n, v in data:
            _wr_bytes(mem, a, n, v & MASK64)
    return mem


async def _mem_model(dut, mem, state):
    """Serve imem combinationally and handle the dmem port each cycle."""
    if os.environ.get("VORTEX_SYNC_MEM") == "1":
        await _mem_model_sync(dut, mem, state)
        return
    dual_fetch = hasattr(dut, "imem_addr1")
    while not state["stop"]:
        await Timer(1, units="ns")                 # let pc / comb settle
        iaddr = _safe_int(dut.imem_addr, RESET_PC)
        dut.imem_rdata.value = _rd_word(mem, iaddr)
        if dual_fetch:                             # M1-INTERFACE v1.1 second fetch port
            dut.imem_rdata1.value = _rd_word(mem, _safe_int(dut.imem_addr1, RESET_PC + 4))
        await Timer(1, units="ns")                 # let decode -> dmem_addr settle
        if _safe_int(dut.rst_n, 0) == 1 and _safe_int(dut.dmem_req, 0) == 1:
            daddr = _safe_int(dut.dmem_addr, 0)
            size = _safe_int(dut.dmem_size, 0)
            nbytes = 1 << size
            if _safe_int(dut.dmem_we, 0) == 1:
                data = _safe_int(dut.dmem_wdata, 0)
                _wr_bytes(mem, daddr, nbytes, data)
                if daddr == TOHOST_ADDR:
                    state["tohost"] = data & MASK64
                    state["halt"] = True
            else:
                dut.dmem_rdata.value = _rd_bytes(mem, daddr, nbytes)
        await RisingEdge(dut.clk)


async def _mem_model_sync(dut, mem, state):
    """Block-RAM-style memory (ADR 0012): every read returns one cycle after its
    address. The address and request are sampled just after the falling edge,
    once the core's combinational outputs have settled; the data is driven just
    after the next rising edge. Writes take effect at the sample point. In reset
    the core fetches RESET_PC, so that word is on the bus when reset is released."""
    dual_fetch = hasattr(dut, "imem_addr1")
    dut.imem_rdata.value = _rd_word(mem, RESET_PC)
    if dual_fetch:
        dut.imem_rdata1.value = _rd_word(mem, RESET_PC + 4)
    while not state["stop"]:
        await FallingEdge(dut.clk)
        await Timer(1, units="ns")
        iaddr = _safe_int(dut.imem_addr, RESET_PC)
        cur = {"i0": _rd_word(mem, iaddr)}
        if dual_fetch:
            cur["i1"] = _rd_word(mem, _safe_int(dut.imem_addr1, RESET_PC + 4))
        if _safe_int(dut.rst_n, 0) == 1 and _safe_int(dut.dmem_req, 0) == 1:
            daddr = _safe_int(dut.dmem_addr, 0)
            nbytes = 1 << _safe_int(dut.dmem_size, 0)
            if _safe_int(dut.dmem_we, 0) == 1:
                data = _safe_int(dut.dmem_wdata, 0)
                _wr_bytes(mem, daddr, nbytes, data)
                if daddr == TOHOST_ADDR:
                    state["tohost"] = data & MASK64
                    state["halt"] = True
            else:
                cur["d"] = _rd_bytes(mem, daddr, nbytes)
        nxt = cur
        await RisingEdge(dut.clk)
        await Timer(1, units="ns")
        dut.imem_rdata.value = nxt["i0"]
        if dual_fetch:
            dut.imem_rdata1.value = nxt["i1"]
        if "d" in nxt:
            dut.dmem_rdata.value = nxt["d"]


async def run_program(dut, words, max_cycles=2000, debug=False, data=None):
    """Reset, load `words` at RESET_PC, run until tohost halt. Returns (retires, mem, state).

    `data` optionally seeds the data region: list of (addr, nbytes, value).
    state["mlstat_sat"] holds the final Xmlacc SAT sticky (dbg_mlstat_sat).
    state["cycles"] counts clock cycles from reset release to the end of the run.

    A fresh clock is started per call: cocotb cleans up scheduled coroutines
    between tests, so a shared clock would stop after the first test.
    """
    clk_task = cocotb.start_soon(Clock(dut.clk, 10, units="ns").start())
    mem = _load_program(words, data=data)
    state = {"halt": False, "tohost": None, "stop": False, "mlstat_sat": 0, "cycles": 0}

    # Assert reset and initialise memory inputs
    dut.rst_n.value = 0
    dut.imem_rdata.value = 0
    dut.dmem_rdata.value = 0
    two_slots = hasattr(dut, "dbg1_retire_valid")    # M1-INTERFACE v1.1 second retire slot
    if two_slots:
        dut.imem_rdata1.value = 0
    for _ in range(3):
        await RisingEdge(dut.clk)                  # reset branch sets pc <- RESET_PC

    mem_task = cocotb.start_soon(_mem_model(dut, mem, state))
    await RisingEdge(dut.clk)                       # mem serves inst0 while still in reset
    await FallingEdge(dut.clk)
    dut.rst_n.value = 1                             # release reset

    retires = []
    for cyc in range(max_cycles):
        await RisingEdge(dut.clk)
        await Timer(1, units="ns")
        state["cycles"] = cyc + 1
        rv = _safe_int(dut.dbg_retire_valid, 0)
        if rv == 1:
            retires.append({
                "pc":   _safe_int(dut.dbg_retire_pc, 0) & MASK64,
                "wbv":  _safe_int(dut.dbg_wb_valid, 0),
                "rd":   _safe_int(dut.dbg_wb_rd, 0),
                "data": _safe_int(dut.dbg_wb_data, 0) & MASK64,
            })
        rv1 = _safe_int(dut.dbg1_retire_valid, 0) if two_slots else 0
        if rv1 == 1:                                # slot 1 is younger than slot 0
            assert rv == 1, "dbg1_retire_valid without dbg_retire_valid"
            retires.append({
                "pc":   _safe_int(dut.dbg1_retire_pc, 0) & MASK64,
                "wbv":  _safe_int(dut.dbg1_wb_valid, 0),
                "rd":   _safe_int(dut.dbg1_wb_rd, 0),
                "data": _safe_int(dut.dbg1_wb_data, 0) & MASK64,
            })
        if debug and rv == 1:
            for r in retires[-(1 + rv1):]:
                dut._log.info("retire pc=%016x wbv=%d rd=%d data=%016x",
                              r["pc"], r["wbv"], r["rd"], r["data"])
        state["mlstat_sat"] = _safe_int(dut.dbg_mlstat_sat, 0)
        if state["halt"] and rv == 0 and len(retires) > 0:
            break

    state["stop"] = True
    mem_task.kill()
    clk_task.kill()
    return retires, mem, state


def final_regs(retires):
    """Replay the dbg retire stream into a 32-entry architectural register model."""
    regs = [0] * 32
    for r in retires:
        if r["wbv"] and r["rd"] != 0:
            regs[r["rd"]] = r["data"]
    return regs


def sext(val, bits):
    val &= (1 << bits) - 1
    if val & (1 << (bits - 1)):
        val -= (1 << bits)
    return val


# ===========================================================================
# Lockstep co-simulation against the golden ISS
# ===========================================================================
def _iss_module():
    """Import model/iss/rv64i.py from the repo root (a hard dependency)."""
    import sys
    from pathlib import Path
    root = str(Path(__file__).resolve().parents[2])
    if root not in sys.path:
        sys.path.insert(0, root)
    from model.iss import rv64i
    return rv64i


async def cosim_lockstep(dut, name, words, data=None, max_cycles=2000, mem_check=None):
    """Run `words` on the RTL and compare every retired instruction with the ISS.

    Each RTL retire is matched by one ISS step on (pc, writeback-valid, rd, value).
    The run must end with a tohost PASS on both, with the same mlstat.SAT. If `mem_check=(base, nbytes)` is
    given, that memory region must also match byte-for-byte at the end, so wrong
    stores are caught even if nothing reads them back.

    Returns (retires, state, iss) for callers that want extra checks.
    """
    iss_mod = _iss_module()
    mem_iss = iss_mod.Memory(base=RESET_PC)
    mem_iss.load_words(words, base=RESET_PC)
    if data:
        for addr, nbytes, val in data:
            mem_iss.store(addr, nbytes, val)
    iss = iss_mod.Hart(mem=mem_iss, reset_pc=RESET_PC, tohost_addr=TOHOST_ADDR)

    retires, mem_rtl, state = await run_program(dut, words, max_cycles=max_cycles, data=data)
    assert state["tohost"] == 1, f"{name}: RTL tohost={state['tohost']} (expected PASS=1)"

    for i, rtl_rec in enumerate(retires):
        assert not iss.halted, \
            f"{name}[{i}]: RTL retired pc={rtl_rec['pc']:#x} after the tohost store"
        rec = iss.step()
        iss_pc = rec["pc"] & MASK64
        iss_rd = rec.get("rd")
        iss_wb = (iss_rd is not None) and (iss_rd != 0)
        iss_val = (rec.get("rd_val", 0) or 0) & MASK64
        assert rtl_rec["pc"] == iss_pc, \
            f"{name}[{i}] pc RTL={rtl_rec['pc']:#x} ISS={iss_pc:#x}"
        assert bool(rtl_rec["wbv"]) == iss_wb, \
            f"{name}[{i}] pc={iss_pc:#x} wb-valid RTL={rtl_rec['wbv']} ISS={iss_wb}"
        if iss_wb:
            assert rtl_rec["rd"] == iss_rd, \
                f"{name}[{i}] pc={iss_pc:#x} rd RTL=x{rtl_rec['rd']} ISS=x{iss_rd}"
            assert rtl_rec["data"] == iss_val, \
                f"{name}[{i}] pc={iss_pc:#x} x{iss_rd} RTL={rtl_rec['data']:#x} ISS={iss_val:#x}"
    assert iss.halted, f"{name}: ISS did not reach the tohost store after {len(retires)} retires"
    assert iss.halt_code == (True, 0), f"{name}: ISS halt_code={iss.halt_code}"
    assert bool(state["mlstat_sat"]) == bool(iss.mlstat & 1), \
        f"{name}: mlstat.SAT RTL={state['mlstat_sat']} ISS={iss.mlstat & 1}"

    if mem_check is not None:
        base, nbytes = mem_check
        for off in range(nbytes):
            a = base + off
            r = mem_rtl.get(a, 0)
            s = mem_iss.load(a, 1) & 0xFF
            assert r == s, f"{name}: memory @{a:#x} RTL={r:#04x} ISS={s:#04x}"
    return retires, state, iss
