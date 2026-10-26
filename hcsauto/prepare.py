"""Stage 1 - PREPARE: plate map + raw image folder -> pipeline-ready run folder.

    detect instrument & metadata -> pick and adapt QC profile -> validate plate map
    -> drop wells without a complete image set -> write input sheet, config, manifest
"""
from __future__ import annotations

import hashlib
import json
import platform
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from . import __version__
from .detect import detect_instrument
from .plateplan import build_input_sheet, imaged_wells, read_plate_map, validate
from .profiles import resolve_settings, write_pipeline_config

PHENIX_PREFIX = "f01p01-"


class PrepareError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def reconcile_wells(plan: dict[str, list[str]], selected: list[str], complete: set[str],
                    any_image: list[str]) -> pd.DataFrame:
    """Compare what was PLANNED (plate map), SELECTED (MetaXpress) and IMAGED (files).

    analysed          planned, and a complete image set exists
    not_acquired      planned, but not selected for acquisition on the microscope
    acquisition_fault selected (or selection unknown) but frames/channels missing
    unplanned         imaged but not on the plate map (ignored, reported for checking)
    """
    selected_known = bool(selected)
    sel = set(selected)
    rows = []
    for cond, wells in plan.items():
        for w in wells:
            if w in complete:
                status, reason = "analysed", ""
            elif selected_known and w not in sel:
                status, reason = "not_acquired", "not selected in MetaXpress acquisition"
            elif w in any_image:
                status, reason = "acquisition_fault", "incomplete image set (missing frames or channels)"
            else:
                status, reason = "acquisition_fault", "no images written for this well"
            rows.append({"well": w, "condition": cond, "status": status, "reason": reason})
    planned = {w for ws in plan.values() for w in ws}
    for w in sorted((set(any_image) | sel) - planned):
        rows.append({"well": w, "condition": "", "status": "unplanned",
                     "reason": "imaged/selected but not on the plate map"})
    return pd.DataFrame(rows, columns=["well", "condition", "status", "reason"])


def prepare_run(
    plate_map: Path | str,
    image_folder: Path | str,
    out_dir: Path | str,
    norm_condition: str,
    pos_control: str | None = None,
    neg_control: str | None = None,
    plate_name: str | None = None,
    base_folder: str | None = None,
    file_prefix: str | None = None,
    config_template: Path | str | None = None,
    profiles_file: Path | str | None = None,
    overrides: dict | None = None,
    min_replicates: int = 1,
) -> dict:
    """Run stage 1 and return the manifest (also written to run_manifest.json)."""
    plate_map, image_folder, out_dir = Path(plate_map), Path(image_folder), Path(out_dir)

    info = detect_instrument(image_folder)

    from .profiles import load_profiles
    settings, notes = resolve_settings(info, load_profiles(profiles_file), overrides)
    if pos_control:
        settings["positive_control"] = pos_control
    if neg_control:
        settings["negative_control"] = neg_control

    plan = read_plate_map(plate_map)
    problems = validate(plan, norm_condition, min_replicates)
    for c in (pos_control, neg_control):
        if c and c not in plan:
            problems.append(f"control '{c}' is not on the plate map")
    unknown = sorted({w for ws in plan.values() for w in ws} - set(info.wells))
    if unknown:
        notes.append(f"{len(unknown)} planned well(s) have no images at all: {', '.join(unknown)}")
    if problems:
        raise PrepareError("plate map problems:\n  " + "\n  ".join(problems))

    # Phenix plates are addressed from their measurement folder, ImageXpress from the plate folder
    plate_dir = image_folder.parent if info.instrument == "phenix" and image_folder.name == "Images" else image_folder
    plate_name = plate_name or plate_dir.name
    base_folder = base_folder or str(plate_dir.parent)
    file_prefix = file_prefix or (PHENIX_PREFIX if info.instrument == "phenix" else plate_name)

    window = range(int(settings["first_timepoint"]), int(settings["last_timepoint"]) + 1)
    have = imaged_wells(Path(info.image_folder) if info.instrument == "phenix" else image_folder,
                        info.instrument, window, channels=info.channels or (1, 2), prefix=file_prefix)
    df, report = build_input_sheet(plan, plate_name, base_folder, file_prefix, norm_condition,
                                   instrument=info.instrument, imaged=have)
    if norm_condition in report.skipped_conditions:
        raise PrepareError(f"normalisation condition {norm_condition} has no fully imaged wells")

    recon = reconcile_wells(plan, info.selected_wells, have, info.wells)
    counts = recon["status"].value_counts().to_dict()
    notes.append("wells: " + ", ".join(f"{v} {k}" for k, v in counts.items()))

    out_dir.mkdir(parents=True, exist_ok=True)
    recon.to_csv(out_dir / "well_reconciliation.csv", index=False)
    input_path = out_dir / "input_file.xlsx"
    df.to_excel(input_path, index=False)
    config_path = write_pipeline_config(settings, out_dir / "config.csv", config_template)

    manifest = {
        "stage": "prepare",
        "tool": f"hcs-lab-automation {__version__}",
        "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "host": platform.node(),
        "inputs": {"plate_map": str(plate_map), "plate_map_sha256": _sha256(plate_map),
                   "image_folder": str(image_folder)},
        "instrument": info.to_dict(),
        "settings": settings,
        "plate": {"name": plate_name, "base_folder": base_folder, "file_prefix": file_prefix,
                  "norm_condition": norm_condition},
        "wells": {"used": report.used, "dropped_not_imaged": report.dropped,
                  "skipped_conditions": report.skipped_conditions, "status_counts": counts,
                  "reconciliation": str(out_dir / "well_reconciliation.csv")},
        "notes": notes,
        "outputs": {"input_file": str(input_path), "config": str(config_path)},
    }
    (out_dir / "run_manifest.json").write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    manifest["_report_text"] = report.text()
    return manifest
