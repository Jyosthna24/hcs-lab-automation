"""Read MetaXpress plate metadata (.HTD files written by the ImageXpress at acquisition).

The .HTD records what the microscope was TOLD to acquire: plate format, which
wells were selected, number of timepoints and wavelengths. Comparing that with
the plate map (what was planned) and the image files (what was actually
written) tells us exactly which wells to analyse, without manual checking.

Example .HTD:
    "HTSInfoFile", Version 1.0
    "TimePoints", 33
    "XWells", 12
    "YWells", 8
    "WellsSelection1", FALSE, FALSE, ...
    "WellsSelection2", FALSE, TRUE, TRUE, ...
    "NWavelengths", 2
    "WaveName1", "FITC"
    "WaveName2", "Texas Red"
    "EndFile"
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

from .plateplan.wells import format_well


@dataclass
class HTDInfo:
    path: str
    n_rows: int
    n_cols: int
    timepoints: int
    wavelengths: list[str] = field(default_factory=list)
    selected_wells: list[str] = field(default_factory=list)   # B02 naming
    description: str = ""

    @property
    def plate_format(self) -> int:
        return self.n_rows * self.n_cols


def _parse_lines(path: Path) -> dict[str, list[str]]:
    out = {}
    with open(path, newline="", encoding="latin-1") as fh:
        for row in csv.reader(fh, skipinitialspace=True):
            if row:
                out[row[0].strip()] = [c.strip() for c in row[1:]]
    return out


def read_htd(path: Path | str) -> HTDInfo:
    path = Path(path)
    kv = _parse_lines(path)
    if "HTSInfoFile" not in kv:
        raise ValueError(f"{path} is not a MetaXpress .HTD file")

    def num(key, default):
        try:
            return int(kv[key][0])
        except (KeyError, IndexError, ValueError):
            return default

    n_rows, n_cols = num("YWells", 8), num("XWells", 12)
    selected = []
    for r in range(1, n_rows + 1):
        flags = kv.get(f"WellsSelection{r}", [])
        selected += [format_well(r, c) for c, f in enumerate(flags, 1) if f.upper() == "TRUE"]
    n_w = num("NWavelengths", 0)
    waves = [kv.get(f"WaveName{i}", [f"w{i}"])[0] for i in range(1, n_w + 1)]
    return HTDInfo(str(path), n_rows, n_cols, num("TimePoints", 1), waves, selected,
                   (kv.get("Description") or [""])[0])


def find_htd(folder: Path | str) -> Path | None:
    folder = Path(folder)
    hits = sorted(list(folder.glob("*.HTD")) + list(folder.glob("*.htd")))
    return hits[0] if hits else None


def write_htd(path: Path | str, selected_wells, timepoints: int, wavelengths=("FITC", "Texas Red"),
              n_rows: int = 8, n_cols: int = 12) -> Path:
    """Write a minimal .HTD (used for demos and tests)."""
    sel = set(selected_wells)
    lines = ['"HTSInfoFile", Version 1.0', '"Description", "hcs-lab-automation demo"',
             f'"TimePoints", {timepoints}', f'"XWells", {n_cols}', f'"YWells", {n_rows}']
    for r in range(1, n_rows + 1):
        flags = ", ".join("TRUE" if format_well(r, c) in sel else "FALSE" for c in range(1, n_cols + 1))
        lines.append(f'"WellsSelection{r}", {flags}')
    lines.append(f'"NWavelengths", {len(wavelengths)}')
    lines += [f'"WaveName{i}", "{w}"' for i, w in enumerate(wavelengths, 1)]
    lines.append('"EndFile"')
    path = Path(path)
    path.write_text("\n".join(lines) + "\n", encoding="latin-1")
    return path
