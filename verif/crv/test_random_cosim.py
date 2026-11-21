"""Constrained-random co-simulation: random RV64IM + Xmlacc programs, RTL vs ISS.

Each seeded program (verif/crv/rvgen.py) runs on the RTL and is compared with the
golden ISS on every retired instruction, and on the whole data region at the end.

Fast pure-Python checks validate the generator itself (deterministic, always
terminates with PASS on the ISS, covers every instruction class), so a co-sim
failure points at the RTL, not at a malformed program.

Run (from the repo root):  python3 -m pytest verif/crv/ -q
More seeds:  VORTEX_RANDOM_SEEDS=0-499 python3 -m pytest verif/crv/ -q
Xmlacc-heavy programs run on the configurations with a pipelined or second Xmlacc
unit (test_random_cosim_xm_mix), where a squash can land on an op in flight.
test_random_cosim_traps runs the programs with timer interrupts arriving every
48-111 cycles (ADR 0015) and some misaligned loads and stores that trap
(ADR 0016), on every configuration.
"""
import os
import sys
from collections import Counter
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
CORE_DIR = ROOT / "verif" / "core"
for _p in (str(HERE), str(CORE_DIR), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from cores import CORE_CONFIGS, CORES, build_and_test  # noqa: E402
from rvasm import DATA_BASE, DATA_SIZE, RESET_PC, TOHOST_ADDR  # noqa: E402
from rvgen import IRQ_SAVE, MIXES, RandomProgram, parse_seeds  # noqa: E402

DEFAULT_SEEDS = "0-24"           # what CI runs on every core
N_ITEMS = 180
ISS_SEEDS = range(200)           # generator sanity sweep (ISS only, fast)
MAX_ISS_STEPS = 50_000
MAX_RTL_CYCLES = 30_000          # ~400 instructions + ~65 cycles per divide


def _run_on_iss(seed, mix="default", irq=False):
    from model.iss.rv64i import Hart, Memory
    asm, data, stats = RandomProgram(seed, n_items=N_ITEMS, mix=MIXES[mix], irq=irq,
                                     faults=irq).build()
    mem = Memory(base=RESET_PC)
    mem.load_words(asm.assemble(), base=RESET_PC)
    for addr, n, v in data:
        mem.store(addr, n, v)
    hart = Hart(mem=mem, reset_pc=RESET_PC, tohost_addr=TOHOST_ADDR, traps=irq)
    return hart.run(max_steps=MAX_ISS_STEPS), stats


# ---------------------------------------------------------------------------
# Generator sanity (pure Python)
# ---------------------------------------------------------------------------
def test_generator_is_deterministic():
    a1, d1, _ = RandomProgram(1234, n_items=N_ITEMS).build()
    a2, d2, _ = RandomProgram(1234, n_items=N_ITEMS).build()
    assert a1.assemble() == a2.assemble() and d1 == d2
    a3, _, _ = RandomProgram(1235, n_items=N_ITEMS).build()
    assert a3.assemble() != a1.assemble()


def test_generator_programs_terminate_with_pass_on_iss():
    for mix, seed, irq in [(m, s, i) for m in MIXES for s in ISS_SEEDS for i in (False, True)]:
        r, _ = _run_on_iss(seed, mix, irq)
        assert r["halted"], f"{mix} seed {seed}: no tohost halt in {MAX_ISS_STEPS} steps (pc={r['pc']:#x})"
        assert r["halt_code"] == (True, 0), f"{mix} seed {seed}: halt_code={r['halt_code']}"


def test_generator_covers_every_instruction_class():
    total = Counter()
    for seed in parse_seeds(DEFAULT_SEEDS):
        _, _, stats = RandomProgram(seed, n_items=N_ITEMS).build()
        total.update(stats)
    for kind in ("alu_rr", "alu_ri", "shift", "upper", "muldiv", "xmlacc", "load", "store",
                 "branch", "jal", "jalr", "loop", "csr", "csr_read", "mlstat_clear",
                 "csrrw", "csrrsi", "fence",
                 "div", "divu", "rem", "remu", "divw", "remuw", "mulh", "mulhsu",
                 "dot8", "dot8u", "dot8su", "macc", "rqs"):
        assert total[kind] > 0, f"{kind} never generated across seeds {DEFAULT_SEEDS}"


# ---------------------------------------------------------------------------
# RTL co-simulation (cocotb)
# ---------------------------------------------------------------------------
try:
    import cocotb
except ImportError:  # pragma: no cover
    cocotb = None

if cocotb is not None:
    @cocotb.test()
    async def random_cosim(dut):
        """Lockstep co-sim of every seed in VORTEX_RANDOM_SEEDS against the ISS."""
        from harness import cosim_lockstep
        seeds = parse_seeds(os.environ.get("VORTEX_RANDOM_SEEDS", DEFAULT_SEEDS))
        mix = MIXES[os.environ.get("VORTEX_RANDOM_MIX", "default")]
        irq = os.environ.get("VORTEX_RANDOM_IRQ") == "1"
        total = spec = mispredicts = irqs = faults = 0
        for seed in seeds:
            asm, data, _ = RandomProgram(seed, n_items=N_ITEMS, mix=mix, irq=irq,
                                         faults=irq).build()
            retires, state, iss = await cosim_lockstep(
                dut, f"seed{seed}", asm.assemble(), data=data,
                max_cycles=MAX_RTL_CYCLES, mem_check=(DATA_BASE, DATA_SIZE))
            total += len(retires)
            if irq:
                count = iss.mem.load(IRQ_SAVE + 16, 8)
                assert count == len(state["irqs"]) > 0, \
                    f"seed{seed}: handler counted {count}, RTL took {len(state['irqs'])}"
                irqs += count
                faults += iss.mem.load(IRQ_SAVE + 24, 8)
            if hasattr(dut, "dbg_mispredicts"):
                spec += int(dut.dbg_spec_branches.value)
                mispredicts += int(dut.dbg_mispredicts.value)
        dut._log.info("random co-sim: %d seeds, %d retired instructions matched the ISS, "
                      "data region matched; %d predicted branches, %d mispredicts, "
                      "%d interrupts, %d misaligned faults", len(seeds), total, spec, mispredicts,
                      irqs, faults)
        assert not irq or faults > 0, "no misaligned access trapped"


@pytest.mark.parametrize("toplevel", CORES)
def test_random_cosim(toplevel):
    # build_and_test leaves its env in os.environ, so every test sets each knob.
    build_and_test(toplevel, "test_random_cosim", HERE, extra_pythonpath=[CORE_DIR, ROOT],
                   env={"VORTEX_RANDOM_SEEDS": os.environ.get("VORTEX_RANDOM_SEEDS",
                                                              DEFAULT_SEEDS),
                        "VORTEX_RANDOM_MIX": "default", "VORTEX_RANDOM_IRQ": "0"})


XM_MIX_CORES = [c for c in CORES if CORE_CONFIGS[c][1].get("XM_LATENCY", 1) > 1
                or CORE_CONFIGS[c][1].get("XM_PORTS", 1) > 1]


@pytest.mark.parametrize("toplevel", XM_MIX_CORES)
def test_random_cosim_xm_mix(toplevel):
    build_and_test(toplevel, "test_random_cosim", HERE, extra_pythonpath=[CORE_DIR, ROOT],
                   env={"VORTEX_RANDOM_SEEDS": os.environ.get("VORTEX_RANDOM_SEEDS",
                                                              DEFAULT_SEEDS),
                        "VORTEX_RANDOM_MIX": "xm", "VORTEX_RANDOM_IRQ": "0"})


@pytest.mark.parametrize("toplevel", CORES)
def test_random_cosim_traps(toplevel):
    build_and_test(toplevel, "test_random_cosim", HERE, extra_pythonpath=[CORE_DIR, ROOT],
                   env={"VORTEX_RANDOM_SEEDS": os.environ.get("VORTEX_RANDOM_SEEDS",
                                                              DEFAULT_SEEDS),
                        "VORTEX_RANDOM_MIX": "default", "VORTEX_RANDOM_IRQ": "1"})
