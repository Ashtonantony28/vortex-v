# NeuroFlight policy on Vortex-V

This workload runs the int8 hover policy from
[neuroflight-fc](https://github.com/Ashtonantony28/neuroflight-fc) (`hover_ctbr_v3`,
a 19 → 64 → 64 → 4 MLP with tanh after every layer) on Vortex-V, and checks it bit for
bit against NeuroFlight's own golden model. It is the first cross-project workload:
the same integer arithmetic the drone firmware is verified against now also runs on
the Vortex-V RTL.

## Why no new instructions are needed

NeuroFlight's int8 contract (`docs/INTERFACES.md` §7 in neuroflight-fc) maps onto the
Xmlacc Tier-1 ops as they are:

| NeuroFlight step | Vortex-V |
| --- | --- |
| `acc = b_q + Σ W_q·x_q` (int32) | `mlacc.macc`, 8 int8 lanes per instruction |
| `acc · M[j]` (int64) | `mul` |
| `clamp(rshift_round(v, S), −128, 127)` with `rshift_round(v, s) = (v + 2^(s−1)) >> s` | `mlacc.rqs` (round-half-up, arithmetic shift, int8 clamp; 65-bit add since PR #2) |
| `lut[y + 128]` | `lb` |

The float32 parts (input normalisation and the final tanh) stay on the host, since
Vortex-V has no FPU. The program starts from `x_q` and ends at `last_acc`, the full
integer path.

## Results (2026-11-12)

All 114 fixture vectors match on the ISS (both hidden layers and the int32 output),
and two vectors (an in-distribution input and the input with the most `rqs` clamps)
run in lockstep with the ISS on every core configuration, with the whole data image
compared at the end.

| Core configuration | Cycles per inference |
| --- | --- |
| `vortex_core` (in-order) | 8,072 |
| `vortex_ooo_core` (1-wide) | 9,944 |
| `vortex_ooo_core_w2` | 5,938 |
| `vortex_ooo_core_spec` | 4,948 |
| `vortex_ooo_core_lsq` | 4,948 |

At an assumed 100 MHz that is about 50 µs per inference on the fastest configuration,
well inside NeuroFlight's 1 kHz control period. Without `macc` (plain RV64IM byte
loads, multiplies and adds) the same kernel needs 3.73x the instructions (29,153 vs
7,809 per inference).

The 1-wide OoO core is slower than the in-order core here: at one commit per cycle
it only wins where multi-cycle units overlap, and this loop is mostly loads and
single-cycle ops.

## Files

- `gen_fixture.py`: regenerates the fixtures from a neuroflight-fc checkout using
  NeuroFlight's `tools/golden.py`; the source commit and policy hash are stored in the
  JSON. Regenerating is a reviewed change.
- `fixtures/hover_ctbr_v3_int8.json`: int8 weights, scales, LUTs and 114 vectors
  (16 from each `make_obs` category: ties, grid, saturating, NaN, in-distribution,
  wide, beyond range, plus the zero and mean rows).
- `fixtures/rshift_cases.json`: NeuroFlight's 4,000 `rshift_round` edge and random
  cases, checked against `mlacc.rqs` in the Xmlacc golden model.
- `nf_kernel.py`: data layout, the RV64IM + Xmlacc program, an ISS runner and a plain
  Python restatement of the contract.
- `test_neuroflight.py`: the checks below.

## Verify

```
python3 -m pytest verif/workloads/neuroflight -q
```

No fixture input drives an int32 accumulator out of range, so the kernel's
`sat_int32` branch is checked separately: a copy of the policy with output biases at
the int32 limits must still match the plain Python reference.
