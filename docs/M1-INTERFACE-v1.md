# M1 — in-order core interface contract (frozen v1)

Scope of M1: a single-hart, machine-mode **RV64I** base-integer core, in-order,
that runs small programs and halts via a tohost store. M-extension (mul/div),
C-extension (compressed), CSRs beyond the minimum, traps/interrupts, and the
full `riscv-arch-test` suite are **M1.1+** follow-ups, explicitly out of M1.

The point of M1 is a correct, simulatable baseline whose architectural state can
be compared, instruction by instruction, against the Python ISS golden model —
that co-sim harness is what the OoO engine (M4) will reuse.

## Top-level RTL contract (`rtl/core/vortex_core.sv`)
Parameters: `RESET_PC` (default `64'h8000_0000`), `XLEN=64`.

Ports (synchronous, active-low reset `rst_n`, single clock `clk`):
- **Instruction fetch** (combinational-read memory in the TB is fine for M1):
  - out `imem_addr` [63:0]
  - in  `imem_rdata` [31:0]   (little-endian; 32-bit aligned instruction)
- **Data memory** (single port, one access at a time in M1):
  - out `dmem_req`, out `dmem_we`
  - out `dmem_addr` [63:0]
  - out `dmem_size` [1:0]      (0=byte,1=half,2=word,3=double)
  - out `dmem_wdata` [63:0]
  - in  `dmem_rdata` [63:0]
  - (loads: unsigned/signed handled in-core via the funct3 of the load)
- **Halt / test result** (riscv-tests style `tohost`):
  - A store to address `TOHOST_ADDR` (default `64'h8000_1000`, a parameter) ends the run.
  - value `1` == PASS; any other odd value == FAIL with code `value>>1`.
  - The TB watches dmem writes to `TOHOST_ADDR` and finishes the simulation.
- **Debug introspection** (for the TB oracle, M1 only — may be removed later):
  - out `dbg_retire_valid`, out `dbg_retire_pc` [63:0]
  - out `dbg_wb_valid`, out `dbg_wb_rd` [4:0], out `dbg_wb_data` [63:0]
  These let the cocotb co-sim compare each retired instruction's (pc, rd, data)
  against the ISS. x0 writes must report `dbg_wb_valid=0` or data 0.

## v1.1 additive ports (ADR 0007, two-wide `vortex_ooo_core`)
`vortex_ooo_core` has every v1 port above plus these. `vortex_core` keeps v1 only; the
harness serves and reads the extra ports only when the toplevel has them.
- **Second fetch port:** out `imem_addr1` [63:0] (always `imem_addr + 4`), in `imem_rdata1` [31:0].
- **Second retire slot:** out `dbg1_retire_valid`, `dbg1_retire_pc` [63:0], `dbg1_wb_valid`,
  `dbg1_wb_rd` [4:0], `dbg1_wb_data` [63:0]. In a cycle where both slots are valid, slot 0 is
  the older instruction. Slot 1 is never valid without slot 0. With `WIDTH=1` it stays 0.

## Golden model contract (`model/iss/rv64i.py`)
A pure-Python RV64I instruction-set simulator:
- `step()` executes one instruction, returns a record `{pc, inst, rd, rd_val, mem}`.
- byte-accurate little-endian memory, x0 hardwired to 0, arithmetic/logical/shift/
  branch/jump/load/store/lui/auipc, `ecall`→trap-to-halt optional.
- Loads a flat memory image (addr→byte dict or bytearray + base).
- This is the oracle both for its own pytest and for the cocotb co-sim.

## Test-program format
- Programs are assembled to a flat little-endian image loaded at `RESET_PC`.
- Use a tiny Python assembler subset (or hand-encoded words) for M1 directed tests;
  reuse `asm/` style. Each program ends by storing its result then `1` to `TOHOST_ADDR`.

## Verify commands
- ISS golden model:  `pytest model/iss/ -q`
- RTL directed + co-sim:  `pytest verif/core/ -q`  (cocotb → Icarus/Verilator)
