"""Regenerate the NeuroFlight fixtures from a neuroflight-fc checkout.

The fixtures are the bit-exact contract between the two projects. They are made
by NeuroFlight's own golden model (tools/golden.py, docs/INTERFACES.md section 7),
not by anything in this repo, so the Vortex-V kernel is checked against the
reference that the flight firmware is checked against.

    python3 verif/workloads/neuroflight/gen_fixture.py --nf ../neuroflight-fc

Regenerating a fixture is a reviewed change (CLAUDE.md): commit the new JSON with
the neuroflight-fc commit it came from (recorded inside the file).
"""
import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
POLICY = "train/policies/hover_ctbr_v3.q.nfpol.json"
PER_TAG = 16          # vectors kept from each make_obs category
SEED = 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--nf", required=True, type=Path, help="path to a neuroflight-fc checkout")
    a = ap.parse_args(argv)
    nf = a.nf.resolve()
    sys.path.insert(0, str(nf))
    from tools.golden import Int8Arrays, forward_int8, load_policy  # noqa: E402
    from tools.gen_vectors import make_obs, rshift_cases  # noqa: E402

    pol_path = nf / POLICY
    pol = load_policy(str(pol_path))
    q = Int8Arrays.from_policy(pol)
    commit = subprocess.run(["git", "-C", str(nf), "rev-parse", "HEAD"],
                            capture_output=True, text=True, check=True).stdout.strip()

    obs, info = make_obs(q, 1000, SEED)
    # keep PER_TAG rows from each category, in make_obs order (deterministic)
    keep, start = [], 0
    for tag in ("zero_mean", "tie", "grid", "saturating", "nan", "in_dist", "wide", "beyond"):
        n = info[tag]
        keep += [(tag, i) for i in range(start, start + min(n, PER_TAG))]
        start += n
    idx = np.array([i for _, i in keep])
    g = forward_int8(q, obs[idx])

    layers = []
    for l, L in enumerate(q.layers):
        d = {"W_q": L.W_q.astype(int).tolist(), "b_q": L.b_q.astype(int).tolist()}
        if L.M is not None:
            d.update(M=L.M.astype(int).tolist(), S=L.S.astype(int).tolist(),
                     lut=L.lut.astype(int).tolist())
        layers.append(d)

    fixture = {
        "source": {"repo": "Ashtonantony28/neuroflight-fc", "commit": commit, "policy": POLICY,
                   "policy_sha256": hashlib.sha256(pol_path.read_bytes()).hexdigest(),
                   "generator": "tools/golden.py forward_int8 on tools/gen_vectors.py make_obs"
                                f"(n=1000, seed={SEED}), {PER_TAG} rows per category"},
        "layers": layers,
        "vectors": [{"tag": tag, "x_q": g.x_q[k].astype(int).tolist(),
                     "hidden": [h[k].astype(int).tolist() for h in g.hidden],
                     "last_acc": g.last_acc[k].astype(int).tolist()}
                    for k, (tag, _) in enumerate(keep)],
    }
    (HERE / "fixtures" / "hover_ctbr_v3_int8.json").write_text(json.dumps(fixture, separators=(",", ":")) + "\n")

    rv, rs, rr = rshift_cases(np.random.default_rng(SEED + 1))
    cases = {"source": {**fixture["source"], "generator": "tools/gen_vectors.py rshift_cases(seed=1); "
                        "expect = clamp(rshift_round(v, s), -128, 127)"},
             "cases": [[int(v), int(s), int(min(127, max(-128, r)))] for v, s, r in zip(rv, rs, rr)]}
    (HERE / "fixtures" / "rshift_cases.json").write_text(json.dumps(cases, separators=(",", ":")) + "\n")
    print(f"wrote {len(keep)} vectors and {len(cases['cases'])} rshift cases from neuroflight-fc {commit[:7]}")


if __name__ == "__main__":
    main()
