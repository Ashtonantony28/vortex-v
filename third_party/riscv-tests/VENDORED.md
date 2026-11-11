# Vendored: riscv-tests (subset)

- Upstream: https://github.com/riscv-software-src/riscv-tests
- Commit: `3df703cd52d5f0d8224e3c50bf03c6cb70efff98` (2026-10-19)
- License: BSD-3-Clause, see `LICENSE` (unchanged from upstream).

Copied unmodified: `isa/rv64ui/*.S`, `isa/rv64um/*.S`, `isa/macros/scalar/test_macros.h`.
Not copied: upstream `env/` (a submodule with CSR/trap setup). Vortex-V supplies its
own CSR-free environment in `verif/conformance/rvtest_env/`.

Vendored rather than fetched so CI never depends on the network and the exact test
sources are pinned and reviewable. Updating them is a reviewed change: bump the
commit above and re-copy.
