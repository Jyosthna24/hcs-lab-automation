"""Aggregate per-cell pipeline output into the tables a scientist actually reports.

Statistical unit = the WELL (cells within a well are not independent replicates).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy import stats


@dataclass
class Columns:
    plate: str = "plate_name"
    well: str = "well"
    condition: str = "condition"


def load_results(path, sheet: str | int = "full_results") -> pd.DataFrame:
    path = str(path)
    if path.lower().endswith((".xlsx", ".xls")):
        try:
            return pd.read_excel(path, sheet_name=sheet)
        except ValueError:                       # sheet name not present: take the first
            return pd.read_excel(path, sheet_name=0)
    return pd.read_csv(path)


def default_readouts(df: pd.DataFrame) -> list[str]:
    """logMemProx (localisation) and the last AACn time point (function), when present."""
    out = [c for c in ("logMemProx",) if c in df.columns]
    aacn = sorted((c for c in df.columns if c.startswith("AACn_")), key=lambda c: int(c.split("_")[1]))
    if aacn:
        out.append(aacn[-1])
    if not out:
        raise ValueError("no readouts given and none of the default columns found")
    return out


def per_well(df: pd.DataFrame, readouts: list[str], norm_condition: str, cols: Columns = Columns()) -> pd.DataFrame:
    g = df.groupby([cols.plate, cols.condition, cols.well], sort=False)
    w = g[readouts].mean()
    w.insert(0, "n_cells", g.size())
    w = w.reset_index()
    for r in readouts:
        ctrl = (w[w[cols.condition] == norm_condition].groupby(cols.plate)[r].mean().rename("_ctrl"))
        w = w.merge(ctrl, left_on=cols.plate, right_index=True, how="left")
        w[f"{r}_delta_vs_ctrl"] = w[r] - w["_ctrl"]
        # % of control is meaningless for log-scale readouts (they can be <= 0)
        is_log = r.lower().startswith("log")
        w[f"{r}_pct_ctrl"] = np.nan if is_log else np.where(w["_ctrl"] > 0, 100 * w[r] / w["_ctrl"], np.nan)
        w = w.drop(columns="_ctrl")
    return w


def per_condition(wells: pd.DataFrame, readouts: list[str], cols: Columns = Columns()) -> pd.DataFrame:
    g = wells.groupby(cols.condition, sort=False)
    out = pd.DataFrame({"n_plates": g[cols.plate].nunique(), "n_wells": g.size(),
                        "n_cells": g["n_cells"].sum()})
    for r in readouts:
        out[f"{r}_mean"] = g[r].mean()
        out[f"{r}_sd"] = g[r].std(ddof=1)
        out[f"{r}_sem"] = g[r].sem(ddof=1)
        out[f"{r}_pct_ctrl"] = g[f"{r}_pct_ctrl"].mean()
    return out.reset_index()


def bh_fdr(p: np.ndarray) -> np.ndarray:
    """Benjamini-Hochberg adjusted p-values (q-values)."""
    p = np.asarray(p, float)
    q = np.full_like(p, np.nan)
    ok = np.isfinite(p)
    if ok.any():
        pv = p[ok]
        order = np.argsort(pv)
        ranked = pv[order] * len(pv) / np.arange(1, len(pv) + 1)
        ranked = np.minimum.accumulate(ranked[::-1])[::-1].clip(max=1)
        qq = np.empty_like(pv)
        qq[order] = ranked
        q[ok] = qq
    return q


def compare_to_control(wells: pd.DataFrame, readouts: list[str], norm_condition: str,
                       cols: Columns = Columns()) -> pd.DataFrame:
    """Welch t-test of each condition vs the control on well means, BH-corrected per readout."""
    rows = []
    ctrl = wells[wells[cols.condition] == norm_condition]
    for r in readouts:
        block = []
        for cond, s in wells.groupby(cols.condition, sort=False):
            if cond == norm_condition:
                continue
            a, b = s[r].dropna(), ctrl[r].dropna()
            t, p = (stats.ttest_ind(a, b, equal_var=False) if len(a) > 1 and len(b) > 1
                    else (np.nan, np.nan))
            block.append({"readout": r, "condition": cond, "vs": norm_condition,
                          "n_wells": len(a), "n_wells_ctrl": len(b),
                          "mean_diff": a.mean() - b.mean(),
                          "pct_ctrl": s[f"{r}_pct_ctrl"].mean(), "t": t, "p_value": p})
        if block:
            q = bh_fdr(np.array([x["p_value"] for x in block]))
            for x, qq in zip(block, q):
                x["q_value"] = qq
            rows += block
    return pd.DataFrame(rows)


def _zprime(p, n):
    spread = abs(np.mean(p) - np.mean(n))
    return 1 - 3 * (np.std(p, ddof=1) + np.std(n, ddof=1)) / spread if spread > 0 else np.nan


def _ssmd(p, n):
    d = np.sqrt(np.var(p, ddof=1) + np.var(n, ddof=1))
    return (np.mean(p) - np.mean(n)) / d if d > 0 else np.nan


def plate_qc(wells: pd.DataFrame, readouts: list[str], pos: str | None, neg: str | None,
             min_cells: int = 20, zprime_min: float = 0.0, cols: Columns = Columns()) -> pd.DataFrame:
    """Per-plate QC: cell counts, and Z' / SSMD from control wells, with a PASS/FAIL verdict."""
    rows = []
    for plate, s in wells.groupby(cols.plate, sort=False):
        row = {cols.plate: plate, "n_wells": len(s), "median_cells_per_well": float(s["n_cells"].median()),
               "wells_below_min_cells": int((s["n_cells"] < min_cells).sum())}
        verdicts = []
        if pos and neg:
            P, N = s[s[cols.condition] == pos], s[s[cols.condition] == neg]
            for r in readouts:
                p, n = P[r].dropna().to_numpy(), N[r].dropna().to_numpy()
                z = _zprime(p, n) if len(p) > 1 and len(n) > 1 else np.nan
                row[f"{r}_zprime"] = z
                row[f"{r}_ssmd"] = _ssmd(p, n) if len(p) > 1 and len(n) > 1 else np.nan
                verdicts.append(np.isfinite(z) and z >= zprime_min)
        row["verdict"] = "PASS" if all(verdicts) and row["wells_below_min_cells"] == 0 else (
            "FAIL" if verdicts and not any(verdicts) else "CHECK")
        rows.append(row)
    return pd.DataFrame(rows)
