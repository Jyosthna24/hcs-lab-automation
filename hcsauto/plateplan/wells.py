"""Well naming and detection of which wells were actually imaged.

Instruments name the same well differently:
    plate map / ImageXpress : B02
    Opera Phenix            : r02c02
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

ROW_LETTERS = "ABCDEFGHIJKLMNOP"  # up to 384-well


def parse_well(well: str) -> tuple[int, int]:
    """'B02', 'b2' or 'r02c02' -> (row, col), both 1-based."""
    w = well.strip()
    m = re.fullmatch(r"[rR](\d{1,2})[cC](\d{1,2})", w)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.fullmatch(r"([A-Pa-p])(\d{1,2})", w)
    if m:
        return ROW_LETTERS.index(m.group(1).upper()) + 1, int(m.group(2))
    raise ValueError(f"unrecognised well name: {well!r}")


def format_well(row: int, col: int, instrument: str = "imagexpress") -> str:
    if instrument == "phenix":
        return f"r{row:02d}c{col:02d}"
    return f"{ROW_LETTERS[row - 1]}{col:02d}"


def convert(well: str, instrument: str) -> str:
    return format_well(*parse_well(well), instrument=instrument)


def imaged_wells(
    image_dir: Path,
    instrument: str,
    timepoints: Iterable[int],
    channels: Iterable[int] = (1, 2),
    prefix: str = "f01p01-",
) -> set[str]:
    """Wells with an image for EVERY required timepoint (and channel).

    A well missing a single frame inside the analysis window would break the
    time course, so it is treated as not imaged. Returned in plate-map form (B02).
    """
    image_dir = Path(image_dir)
    timepoints, channels = list(timepoints), list(channels)

    if instrument == "phenix":
        # <image_dir>/r02c02f01p01-ch1sk10fk1fl1.tiff
        names = {p.name for p in image_dir.glob("*.tiff")}
        found = {m.group(1) for n in names if (m := re.match(r"^(r\d\dc\d\d)", n))}
        ok = {w for w in found
              if all(f"{w}{prefix}ch{ch}sk{t}fk1fl1.tiff" in names
                     for t in timepoints for ch in channels)}
    elif instrument == "imagexpress":
        # <image_dir>/TimePoint_10/<anything>_B02_w1.TIF ; every channel must be present
        per_t = []
        for t in timepoints:
            found = {(m.group(1).upper(), int(m.group(2)))
                     for p in (image_dir / f"TimePoint_{t}").glob("*")
                     if (m := re.search(r"_([A-P]\d{2})(?:_s\d+)?_w(\d)\.tif$", p.name, re.I))}
            wells_t = {w for w, _ in found}
            per_t.append({w for w in wells_t if all((w, ch) in found for ch in channels)})
        ok = set.intersection(*per_t) if per_t else set()
    else:
        raise ValueError("instrument must be 'phenix' or 'imagexpress'")

    return {convert(w, "imagexpress") for w in ok}
