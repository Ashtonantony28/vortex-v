"""Animated "retire race" of the Vortex-V core configurations for the README.

    python3 scripts/media/retire_race.py [--out docs/media]

Builds four core configurations under cocotb + Icarus (the same RTL and harness as verif/), runs the
verif/core/test_ooo.py microbenchmarks on each, records the cycle of every retired instruction,
and animates instructions retired vs clock cycle. These are cycle counts from RTL simulation, not
FPGA measurements (no bitstream yet). Writes retire-race.{gif,mp4,png}.
"""
import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "verif" / "core"))
from cores import build_and_test  # noqa: E402

CONFIGS = [("vortex_core", "in-order (M1)", "#8b949e"),
           ("vortex_ooo_core", "out-of-order, 1-wide (M4)", "#d2a8ff"),
           ("vortex_ooo_core_w2", "OoO 2-wide (M5)", "#58a6ff"),
           ("vortex_ooo_core_xm2", "2-wide + speculation + LSQ + 2 Xmlacc (M8)", "#f0883e")]
PROGS = [("overlap", "Independent work behind a 65-cycle divide"),
         ("wide", "64 independent ALU ops"),
         ("loop", "Branchy loop, 40 iterations"),
         ("lsq", "Loads past a store stuck behind a divide")]
BG, PANEL, GRID, TXT, DIM = "#0d1117", "#161b22", "#30363d", "#e6edf3", "#8b949e"


def measure(work):
    res = {}
    for cfg, _, _ in CONFIGS:
        out = work / f"{cfg}.json"
        build_and_test(cfg, "retire_tb", work, extra_pythonpath=[ROOT / "scripts" / "media", ROOT / "verif" / "core", ROOT],
                       env={"VORTEX_RETIRE_OUT": str(out), "VORTEX_PROGRAMS": ",".join(p for p, _ in PROGS)})
        res[cfg] = json.loads(out.read_text())
    return res


def curves(res):
    cur = {}
    for cfg, _, _ in CONFIGS:
        for p, _ in PROGS:
            tr = np.array(res[cfg][p]["trace"], int)
            c = np.zeros(res[cfg][p]["cycles"] + 1, int)
            for cyc, n in tr:
                if cyc < len(c):
                    c[cyc] += n
            cur[cfg, p] = np.cumsum(c)
    return cur


def animate(res, out, fps=24, secs=9.0):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({"font.family": "DejaVu Sans", "text.color": TXT, "axes.labelcolor": DIM,
                         "xtick.color": DIM, "ytick.color": DIM, "axes.edgecolor": GRID})
    cur = curves(res)
    fig, axs = plt.subplots(2, 2, figsize=(12.8, 7.2), dpi=100, facecolor=BG)
    fig.subplots_adjust(left=0.06, right=0.98, top=0.79, bottom=0.08, hspace=0.38, wspace=0.14)
    fig.text(0.06, 0.94, "Vortex-V: instructions retired vs clock cycle", fontsize=18, weight="bold", color=TXT)
    fig.text(0.06, 0.905, "RTL SIMULATION (cocotb + Icarus) of the same RTL four ways, on the verif/core/test_ooo.py "
             "microbenchmarks  ·  lower right = faster", fontsize=9.5, color=DIM)
    for k, (_, lab, col) in enumerate(CONFIGS):
        fig.text(0.06 + 0.40 * (k % 2), 0.862 - 0.03 * (k // 2), "━ " + lab, color=col, fontsize=10)
    lines, labels, maxc = {}, {}, {}
    for ax, (p, title) in zip(axs.flat, PROGS):
        m = max(len(cur[c, p]) for c, _, _ in CONFIGS)
        maxc[p] = m
        ax.set_facecolor(PANEL)
        ax.grid(color=GRID, lw=0.6)
        ax.set_title(title, color=TXT, fontsize=11.5, loc="left")
        ax.set_xlim(0, m * 1.02)
        ax.set_ylim(0, max(cur[c, p][-1] for c, _, _ in CONFIGS) * 1.12)
        ax.set_xlabel("clock cycle", fontsize=9)
        ax.set_ylabel("retired", fontsize=9)
        for k, (c, _, col) in enumerate(CONFIGS):
            lines[c, p], = ax.plot([], [], color=col, lw=4.0 - 0.6 * k, solid_capstyle="round")
        order = sorted(CONFIGS, key=lambda cc: len(cur[cc[0], p]))
        for r, (c, _, col) in enumerate(order):
            labels[c, p] = ax.text(0.98, 0.05 + 0.085 * (len(order) - 1 - r), "", color=col, fontsize=10, weight="bold",
                                   ha="right", transform=ax.transAxes, family="DejaVu Sans Mono")
    nfr = int(secs * fps)
    tmp = Path(tempfile.mkdtemp())
    for f in range(nfr + fps * 2):
        a = min(1.0, f / nfr)
        for p, _ in PROGS:
            lim = int(a * maxc[p])
            for c, _, _ in CONFIGS:
                y = cur[c, p]
                n = min(lim, len(y) - 1)
                lines[c, p].set_data(np.arange(n + 1), y[:n + 1])
                done = lim >= len(y) - 1
                labels[c, p].set_text(f"{len(y) - 1:4d} cycles" if done else "")
        fig.savefig(tmp / f"f{f:04d}.png", facecolor=BG)
    fig.savefig(out / "retire-race.png", facecolor=BG)
    plt.close(fig)
    mp4, gif = out / "retire-race.mp4", out / "retire-race.gif"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-framerate", str(fps), "-i", str(tmp / "f%04d.png"), "-c:v", "libx264",
                    "-crf", "22", "-preset", "slow", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(mp4)], check=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(mp4), "-vf",
                    "fps=15,scale=960:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=64:stats_mode=diff[p];"
                    "[b][p]paletteuse=dither=none:diff_mode=rectangle", str(gif)], check=True)
    return mp4, gif


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "docs" / "media"))
    ap.add_argument("--cache", default="")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    if a.cache and Path(a.cache).exists():
        res = json.loads(Path(a.cache).read_text())
    else:
        res = measure(Path(tempfile.mkdtemp()))
        if a.cache:
            Path(a.cache).write_text(json.dumps(res))
    for c, lab, _ in CONFIGS:
        print(f"{lab:48s}", {p: res[c][p]["cycles"] for p, _ in PROGS})
    for f in animate(res, out):
        print(f, f"{f.stat().st_size / 1e6:.2f} MB")


if __name__ == "__main__":
    main()
