"""SQLite run database: every run's QC and results, queryable for months of assay history.

Tables
    runs          one row per run (plate, instrument, status, cell counts, settings)
    plate_qc      Z' / SSMD per plate and readout
    well_status   reconciliation outcome of every planned well
    results       per-condition means per readout

`trend()` turns the history into a statistical process control (Shewhart) chart:
centre line and ±3 SD limits from a baseline of passing runs, flagging runs outside
the limits and runs of 7+ consecutive points on one side of the mean (drift).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_name    TEXT NOT NULL,
    plate       TEXT,
    instrument  TEXT,
    recorded_at TEXT NOT NULL,
    status      TEXT NOT NULL,
    n_wells     INTEGER,
    n_cells     INTEGER,
    pixel_size_um REAL,
    settings    TEXT,
    run_folder  TEXT
);
CREATE TABLE IF NOT EXISTS plate_qc (
    run_id INTEGER REFERENCES runs(run_id), plate TEXT, readout TEXT,
    zprime REAL, ssmd REAL, verdict TEXT, median_cells_per_well REAL
);
CREATE TABLE IF NOT EXISTS well_status (
    run_id INTEGER REFERENCES runs(run_id), well TEXT, condition TEXT, status TEXT, reason TEXT
);
CREATE TABLE IF NOT EXISTS results (
    run_id INTEGER REFERENCES runs(run_id), condition TEXT, readout TEXT,
    mean REAL, sem REAL, n_wells INTEGER
);
CREATE INDEX IF NOT EXISTS ix_qc_readout ON plate_qc(readout);
"""


def connect(path: Path | str) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    return con


def record_run(db: Path | str, run_name: str, run_folder: Path | str, report: dict,
               manifest: dict | None = None, readouts: list[str] | None = None) -> int:
    """Insert one finished run; returns its run_id."""
    manifest = manifest or {}
    inst = manifest.get("instrument", {})
    qc: pd.DataFrame = report["plate_qc"]
    cond: pd.DataFrame = report["per_condition"]
    readouts = readouts or sorted({c[: -len("_zprime")] for c in qc.columns if c.endswith("_zprime")}
                                  | {c[: -len("_mean")] for c in cond.columns if c.endswith("_mean")})
    plate_col = next((c for c in qc.columns if c not in ("n_wells", "median_cells_per_well",
                                                        "wells_below_min_cells", "verdict")
                      and not c.endswith(("_zprime", "_ssmd"))), None)
    with connect(db) as con:
        cur = con.execute(
            "INSERT INTO runs (run_name, plate, instrument, recorded_at, status, n_wells, n_cells,"
            " pixel_size_um, settings, run_folder) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (run_name, manifest.get("plate", {}).get("name") or ", ".join(qc[plate_col].astype(str)),
             inst.get("instrument"), datetime.now(timezone.utc).isoformat(timespec="seconds"),
             report["status"], int(cond["n_wells"].sum()), int(cond["n_cells"].sum()),
             inst.get("pixel_size_um"), json.dumps(manifest.get("settings", {}), default=str),
             str(run_folder)))
        run_id = cur.lastrowid
        for row in qc.to_dict("records"):
            for r in readouts:
                if f"{r}_zprime" in row:
                    con.execute("INSERT INTO plate_qc VALUES (?,?,?,?,?,?,?)",
                                (run_id, str(row[plate_col]), r, _f(row[f"{r}_zprime"]),
                                 _f(row.get(f"{r}_ssmd")), row["verdict"], _f(row["median_cells_per_well"])))
        for row in cond.to_dict("records"):
            for r in readouts:
                if f"{r}_mean" in row:
                    con.execute("INSERT INTO results VALUES (?,?,?,?,?,?)",
                                (run_id, row["condition"], r, _f(row[f"{r}_mean"]),
                                 _f(row.get(f"{r}_sem")), int(row["n_wells"])))
        recon = Path(run_folder) / "well_reconciliation.csv"
        if recon.exists():
            w = pd.read_csv(recon).fillna("")
            con.executemany("INSERT INTO well_status VALUES (?,?,?,?,?)",
                            [(run_id, *r) for r in w[["well", "condition", "status", "reason"]].itertuples(index=False)])
    return run_id


def _f(v):
    try:
        v = float(v)
        return v if np.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def qc_history(db: Path | str, readout: str) -> pd.DataFrame:
    with connect(db) as con:
        return pd.read_sql_query(
            """SELECT r.run_id, r.run_name, r.recorded_at, r.instrument, q.plate, q.zprime, q.ssmd, q.verdict
               FROM plate_qc q JOIN runs r USING (run_id)
               WHERE q.readout = ? ORDER BY r.recorded_at, r.run_id""", con, params=(readout,))


def spc_flags(values: np.ndarray, baseline: int = 10, run_length: int = 7) -> pd.DataFrame:
    """Shewhart limits from the first `baseline` points; Western Electric rules 1 and 'run of 7'."""
    v = np.asarray(values, float)
    base = v[:baseline][np.isfinite(v[:baseline])]
    centre = float(np.mean(base)) if base.size else np.nan
    sd = float(np.std(base, ddof=1)) if base.size > 1 else np.nan
    lo, hi = centre - 3 * sd, centre + 3 * sd
    beyond = (v < lo) | (v > hi)
    side = np.sign(v - centre)
    run = np.zeros(len(v), bool)
    count = 0
    for i in range(len(v)):
        count = count + 1 if i and side[i] == side[i - 1] and side[i] != 0 else 1
        if count >= run_length:
            run[i - run_length + 1: i + 1] = True
    return pd.DataFrame({"value": v, "centre": centre, "lcl": lo, "ucl": hi,
                         "beyond_3sd": beyond, "drift_run": run})


def trend(db: Path | str, readout: str, out_dir: Path | str, metric: str = "zprime",
          baseline: int = 10) -> dict:
    """Control chart of a QC metric across all recorded runs."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    h = qc_history(db, readout)
    if h.empty:
        raise ValueError(f"no QC history for readout '{readout}'")
    f = spc_flags(h[metric].to_numpy(), baseline=min(baseline, len(h)))
    h = pd.concat([h, f.drop(columns="value")], axis=1)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    h.to_csv(out_dir / f"trend_{readout}_{metric}.csv", index=False)

    fig, ax = plt.subplots(figsize=(max(6, 0.35 * len(h) + 2), 3.8))
    x = np.arange(len(h))
    ax.plot(x, h[metric], "-o", color="C0", ms=4, lw=1)
    bad = h["beyond_3sd"] | h["drift_run"]
    ax.scatter(x[bad], h.loc[bad, metric], color="C3", zorder=3, s=40, label="out of control")
    for y, ls, lab in ((h["centre"].iloc[0], "-", "centre"), (h["ucl"].iloc[0], "--", "±3 SD"),
                       (h["lcl"].iloc[0], "--", None)):
        ax.axhline(y, color="0.4", ls=ls, lw=0.8, label=lab)
    if metric == "zprime":
        ax.axhline(0.5, color="C2", ls=":", lw=1, label="Z′ = 0.5 (excellent)")
    ax.set_xticks(x, h["plate"].astype(str), rotation=90, fontsize=7)
    ax.set_ylabel(f"{metric} ({readout})")
    ax.set_title(f"Assay performance over time: {metric}, {readout}", loc="left", fontsize=10)
    ax.legend(fontsize=7, frameon=False)
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    png = out_dir / f"trend_{readout}_{metric}.png"
    fig.savefig(png, dpi=200)
    plt.close(fig)
    return {"table": h, "figure": png, "n_out_of_control": int(bad.sum())}
