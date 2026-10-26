"""Build a complete demo: a fake microscope output folder, processed hands-off by `watch`.

    python scripts/demo.py            # writes everything to demo_output/

The simulated ImageXpress plate has the problems real plates have:
  E05 planned but not ticked in MetaXpress, D03 ticked but never imaged
  (microscope fault). Both are removed automatically and listed in
  demo_output/runs/Plate_1/well_reconciliation.csv.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hcsauto.demo import fake_imagexpress  # noqa: E402
from hcsauto.plateplan import read_plate_map  # noqa: E402
from hcsauto.__main__ import main  # noqa: E402

out = ROOT / "demo_output"
plate_map = ROOT / "examples" / "demo_plate_map.csv"
wells = [w for ws in read_plate_map(plate_map).values() for w in ws]
selected = [w for w in wells if w != "E05"]
fake_imagexpress(out / "microscope" / "Plate_1", [w for w in selected if w != "D03"],
                 range(1, 34), selected=selected)

sys.exit(main([
    "watch", str(out / "microscope"), "--dest", str(out / "data_store"), "--runs", str(out / "runs"),
    "--plate-map", str(plate_map), "--norm", "WT_DMSO", "--pos", "WT_DMSO", "--neg", "F508del_DMSO",
    "--settle", "0", "--once",
    "--pipeline", f'"{sys.executable}" "{ROOT / "scripts" / "fake_pipeline.py"}" '
                  "--input {input} --config {config} --out {results_dir}",
]))
