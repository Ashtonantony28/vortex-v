#!/usr/bin/env bash
# Vortex-V simulation toolchain. Verified on Ubuntu 24.04 (root).
# These are the RTL-sim dependencies; a fresh cloud container is ephemeral,
# so run this once per environment (or fold it into a SessionStart hook).
set -euo pipefail
apt-get update -qq
apt-get install -y -qq iverilog verilator   # Icarus 12.0, Verilator 5.020
python3 -m pip install -q --upgrade pip
python3 -m pip install -q cocotb numpy pytest
echo "Installed:"; iverilog -V | head -1; verilator --version; python3 -c "import cocotb; print('cocotb', cocotb.__version__)"
# Deferred (added at their milestones): Spike (riscv-isa-sim) for M2 golden co-sim,
# riscv-dv for M4 random co-sim, SymbiYosys for M7 formal, a riscv-gnu-toolchain for M1.1.
