"""Standard figures for every run: one per readout, plus plate QC."""
from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from ..plateplan.wells import parse_well


def _save(fig, out: Path, name: str) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    png = out / f"{name}.png"
    fig.savefig(png, dpi=200, bbox_inches="tight")
    fig.savefig(out / f"{name}.svg", bbox_inches="tight")     # editable vector copy
    plt.close(fig)
    return png


def condition_plot(wells: pd.DataFrame, readout: str, out: Path, condition_col: str = "condition",
                   control: str | None = None) -> Path:
    """Mean ± SEM of well means, every well shown as a point."""
    conds = list(pd.unique(wells[condition_col]))
    fig, ax = plt.subplots(figsize=(max(4, 0.7 * len(conds) + 1.5), 4))
    rng = np.random.default_rng(0)
    for i, c in enumerate(conds):
        v = wells.loc[wells[condition_col] == c, readout].dropna()
        color = "0.55" if c == control else f"C{i % 10}"
        ax.bar(i, v.mean(), yerr=v.sem() if len(v) > 1 else 0, color=color, alpha=0.35,
               edgecolor=color, capsize=3, width=0.7)
        ax.scatter(i + rng.uniform(-0.15, 0.15, len(v)), v, s=14, color=color, zorder=3)
    if control in conds:
        ax.axhline(wells.loc[wells[condition_col] == control, readout].mean(),
                   color="0.4", lw=0.8, ls="--")
    ax.set_xticks(range(len(conds)), conds, rotation=45, ha="right", fontsize=8)
    ax.set_ylabel(readout)
    ax.spines[["top", "right"]].set_visible(False)
    ax.set_title(f"{readout}: mean ± SEM of wells", fontsize=10, loc="left")
    return _save(fig, out, f"readout_{readout}")


def plate_heatmap(wells: pd.DataFrame, plate: str, value: str, out: Path,
                  plate_col: str = "plate_name", well_col: str = "well") -> Path:
    """Value per well laid out as the physical plate (spots edge effects and dead wells)."""
    s = wells[wells[plate_col] == plate]
    rc = [parse_well(w) for w in s[well_col]]
    n_rows, n_cols = (16, 24) if any(r > 8 or c > 12 for r, c in rc) else (8, 12)
    grid = np.full((n_rows, n_cols), np.nan)
    for (r, c), v in zip(rc, s[value]):
        grid[r - 1, c - 1] = v
    fig, ax = plt.subplots(figsize=(n_cols * 0.45 + 1.5, n_rows * 0.45 + 0.8))
    im = ax.imshow(grid, cmap="viridis")
    ax.set_xticks(range(n_cols), range(1, n_cols + 1), fontsize=7)
    ax.set_yticks(range(n_rows), list("ABCDEFGHIJKLMNOP"[:n_rows]), fontsize=7)
    for (r, c), v in zip(rc, s[value]):
        ax.text(c - 1, r - 1, f"{v:.0f}", ha="center", va="center", fontsize=6, color="white")
    fig.colorbar(im, ax=ax, fraction=0.03, label=value)
    ax.set_title(f"{plate}: {value} per well", fontsize=10, loc="left")
    return _save(fig, out, f"plate_{plate}_{value}")
