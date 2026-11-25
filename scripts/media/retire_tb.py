"""cocotb test used by scripts/media/retire_race.py: runs the verif/core/test_ooo.py programs and
records the clock cycle of every retired instruction (both retire slots)."""
import json
import os

import cocotb
from cocotb.triggers import RisingEdge, Timer

from harness import run_program
from test_ooo import PROGRAM_DATA, PROGRAMS


async def _monitor(dut, log, stop):
    cyc = 0
    two = hasattr(dut, "dbg1_retire_valid")
    while not stop["v"]:
        await RisingEdge(dut.clk)
        await Timer(1, units="ns")
        if int(dut.rst_n.value) == 0:
            cyc = 0
            continue
        cyc += 1
        n = 0
        try:
            n += int(dut.dbg_retire_valid.value)
            if two:
                n += int(dut.dbg1_retire_valid.value)
        except ValueError:
            pass
        if n:
            log.append((cyc, n))


@cocotb.test()
async def retire_trace(dut):
    out = {}
    for name in os.environ.get("VORTEX_PROGRAMS", ",".join(PROGRAMS)).split(","):
        log, stop = [], {"v": False}
        mon = cocotb.start_soon(_monitor(dut, log, stop))
        data = PROGRAM_DATA[name]() if name in PROGRAM_DATA else None
        retires, _, state = await run_program(dut, PROGRAMS[name](), max_cycles=4000, data=data)
        stop["v"] = True
        mon.cancel()
        macs = int(dut.dbg_xm_macs.value) if hasattr(dut, "dbg_xm_macs") else None
        out[name] = {"cycles": state["cycles"], "retired": len(retires), "trace": log, "macs": macs}
    with open(os.environ["VORTEX_RETIRE_OUT"], "w") as fh:
        json.dump(out, fh)
