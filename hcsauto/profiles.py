"""Choose and adapt the instrument QC profile, then write the pipeline config."""
from __future__ import annotations

import csv
from pathlib import Path

import yaml

from .detect import InstrumentInfo

DEFAULT_PROFILES = Path(__file__).with_name("profiles.yaml")


class ProfileError(ValueError):
    pass


def load_profiles(path: Path | str | None = None) -> dict:
    with open(path or DEFAULT_PROFILES, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def resolve_settings(info: InstrumentInfo, profiles: dict | None = None,
                     overrides: dict | None = None) -> tuple[dict, list[str]]:
    """Profile for the detected instrument, adapted to the data. Returns (settings, notes).

    * pixel size comes from image metadata when available, else the profile reference
    * Cellpose diameter scales with 1 / pixel size; minimum object size with 1 / pixel area
    * the analysis window is checked against the timepoints actually acquired
    """
    profiles = profiles or load_profiles()
    if info.instrument not in profiles:
        raise ProfileError(f"no QC profile for instrument '{info.instrument}'")
    p = dict(profiles[info.instrument])
    notes = [f"instrument detected: {info.instrument} (profile '{info.instrument}')"]

    ref = float(p.pop("reference_pixel_size_um"))
    px = info.pixel_size_um
    if px is None:
        px = ref
        notes.append(f"pixel size not in metadata; using profile value {ref} µm")
    else:
        notes.append(f"pixel size {px:.4g} µm read from {info.sources.get('pixel_size_um')}")
    scale = ref / px
    p["pixel_size_um"] = round(px, 6)
    p["diameter"] = round(float(p.pop("diameter_px")) * scale, 3)
    p["min_size"] = max(1, round(float(p.pop("min_size_px")) * scale ** 2))
    if abs(scale - 1) > 1e-6:
        notes.append(f"pixel-based settings rescaled x{scale:.3g} "
                     f"(diameter {p['diameter']} px, min_size {p['min_size']} px)")

    p.update(overrides or {})

    if info.timepoints:
        lo, hi = min(info.timepoints), max(info.timepoints)
        for k in ("first_timepoint", "last_timepoint", "reference_timepoint"):
            if not lo <= int(p[k]) <= hi:
                raise ProfileError(f"{k}={p[k]} is outside the acquired timepoints {lo}-{hi}")
        notes.append(f"analysis window {p['first_timepoint']}-{p['last_timepoint']} "
                     f"within acquired {lo}-{hi}")
    return p, notes


def write_pipeline_config(settings: dict, path: Path | str, template: Path | str | None = None) -> Path:
    """Write the pipeline's variable_name,description,value CSV.

    With a template (an existing config), every other setting and description is
    kept and only the automated values are replaced.
    """
    path = Path(path)
    rows: list[dict] = []
    if template:
        with open(template, newline="", encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))
    known = {r["variable_name"] for r in rows}
    for r in rows:
        if r["variable_name"] in settings:
            r["value"] = settings[r["variable_name"]]
    for k, v in settings.items():
        if k not in known:
            rows.append({"variable_name": k, "description": "set by hcs-lab-automation", "value": v})
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["variable_name", "description", "value"])
        w.writeheader()
        w.writerows(rows)
    return path
