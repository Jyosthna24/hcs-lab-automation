"""Stand-in for the image-analysis pipeline, so the full automation can be demoed and tested.

Reads the input sheet the PREPARE stage wrote and produces a per-cell results
workbook with the same structure as the real pipeline's output.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hcsauto.demo import EFFECTS  # noqa: E402
from hcsauto.plateplan import convert  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--input", required=True)
ap.add_argument("--config", required=True)
ap.add_argument("--out", required=True)
a = ap.parse_args()

inp = pd.read_excel(a.input)
cfg = pd.read_csv(a.config).set_index("variable_name")["value"]
print("pipeline config:", dict(cfg[["microscope_type", "pixel_size_um", "diameter",
                                     "lower_red_bound", "upper_red_bound"]]))
rng = np.random.default_rng(1)
rows = []
for rec in inp.to_dict("records"):
    base = next((v for k, v in EFFECTS.items() if rec["condition"].startswith(k)), (0.0, -2.0))
    for col in [c for c in inp.columns if c.startswith("condWells")]:
        if isinstance(rec[col], str):
            n = int(rng.integers(40, 120))
            rows.append(pd.DataFrame({
                "plate_name": rec["plateStr"], "well": convert(rec[col], "imagexpress"),
                "condition": rec["condition"], "norm_condition": rec["normCondition"],
                "logMemProx": 0.25 + base[0] + rng.normal(0, 0.15, n),
                "AACn_43": 12 + base[1] + rng.normal(0, 3, n)}))
out = Path(a.out)
out.mkdir(parents=True, exist_ok=True)
with pd.ExcelWriter(out / "results.xlsx") as xl:
    pd.concat(rows).to_excel(xl, sheet_name="full_results", index=False)
print("wrote", out / "results.xlsx")
