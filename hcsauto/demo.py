"""Synthetic data for demos and tests: tiny fake image folders and fake pipeline output.

No real images or experimental results are distributed with this repository.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import tifffile

from .metaxpress import write_htd
from .plateplan import convert, read_plate_map

PHENIX_INDEX = """<?xml version="1.0" encoding="utf-8"?>
<EvaluationInputData><Images><Image>
  <ImageResolutionX Unit="m">{res_m}</ImageResolutionX>
  <ImageSizeX>{size}</ImageSizeX><BinningX>{binning}</BinningX>
  <ObjectiveMagnification Unit="">{objective}</ObjectiveMagnification>
</Image></Images></EvaluationInputData>"""

METAMORPH_DESC = ('<MetaData><prop id="spatial-calibration-x" type="float" value="{px}"/>'
                  '<prop id="binning-x" type="int" value="{binning}"/>'
                  '<prop id="_MagSetting_" type="string" value="{objective}X"/></MetaData>')


def _img(size, rng):
    return rng.integers(100, 4000, (size, size), dtype=np.uint16)


def fake_phenix(root: Path, wells, timepoints, channels=(1, 2), size=16, pixel_um=0.598,
                binning=2, objective=20, prefix="f01p01-", seed=0) -> Path:
    """<root>/Images/Index.xml + r02c02f01p01-ch1sk10fk1fl1.tiff ..."""
    rng = np.random.default_rng(seed)
    img_dir = Path(root) / "Images"
    img_dir.mkdir(parents=True, exist_ok=True)
    (img_dir / "Index.xml").write_text(PHENIX_INDEX.format(res_m=pixel_um * 1e-6, size=size,
                                                           binning=binning, objective=objective))
    for w in wells:
        for t in timepoints:
            for ch in channels:
                tifffile.imwrite(img_dir / f"{convert(w, 'phenix')}{prefix}ch{ch}sk{t}fk1fl1.tiff", _img(size, rng))
    return img_dir


def fake_imagexpress(root: Path, wells, timepoints, channels=(1, 2), size=16, pixel_um=0.645,
                     binning=1, objective=20, plate="Plate_1", selected=None, seed=0) -> Path:
    """<root>/Plate_1.HTD + TimePoint_N/Plate_1_B02_w1.TIF ...

    `selected` = wells ticked in MetaXpress (default: the wells written). Pass a
    larger set to simulate wells the microscope was meant to image but did not.
    """
    rng = np.random.default_rng(seed)
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    timepoints = list(timepoints)
    write_htd(root / f"{plate}.HTD", selected if selected is not None else wells, len(timepoints),
              wavelengths=[f"w{c}" for c in channels])
    desc = METAMORPH_DESC.format(px=pixel_um, binning=binning, objective=objective)
    for t in timepoints:
        d = root / f"TimePoint_{t}"
        d.mkdir(exist_ok=True)
        for w in wells:
            for ch in channels:
                tifffile.imwrite(d / f"{plate}_{w}_w{ch}.TIF", _img(size, rng),
                                 description=desc, metadata=None)
    return root


EFFECTS = {  # (logMemProx shift, AACn shift) relative to WT_DMSO
    "WT_DMSO": (0.0, 0.0), "WT_ETI": (0.02, 4.0),
    "F508del_DMSO": (-0.35, -9.0), "F508del_ETI": (-0.12, -4.0),
}


def fake_results(plate_map: Path, plates=("Plate_1", "Plate_2"), cells_per_well=60, seed=0) -> pd.DataFrame:
    """Per-cell table shaped like the pipeline's 'full_results' sheet."""
    rng = np.random.default_rng(seed)
    plan = read_plate_map(plate_map)
    rows = []
    for plate in plates:
        shift = rng.normal(0, 0.03)
        for cond, wells in plan.items():
            base = next((v for k, v in EFFECTS.items() if cond.startswith(k)), (0.0, -2.0))
            for w in wells:
                n = int(rng.integers(cells_per_well // 2, cells_per_well * 2))
                rows.append(pd.DataFrame({
                    "plate_name": plate, "well": w, "condition": cond, "norm_condition": "WT_DMSO",
                    "logMemProx": 0.25 + shift + base[0] + rng.normal(0, 0.15, n),
                    "AACn_43": 12 + base[1] + rng.normal(0, 3, n),
                }))
    return pd.concat(rows, ignore_index=True)
