"""Turn a plate plan into the analysis pipeline's input sheet."""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from .wells import convert

META_COLUMNS = ["saveResultsHere", "plateStr", "baseFolder", "filePrefix",
                "condition", "normCondition"]


@dataclass
class BuildReport:
    used: dict[str, list[str]] = field(default_factory=dict)
    dropped: dict[str, list[str]] = field(default_factory=dict)
    skipped_conditions: list[str] = field(default_factory=list)

    @property
    def n_used(self) -> int:
        return sum(map(len, self.used.values()))

    @property
    def n_dropped(self) -> int:
        return sum(map(len, self.dropped.values()))

    def text(self) -> str:
        lines = [f"{len(self.used)} conditions, {self.n_used} wells used, "
                 f"{self.n_dropped} wells dropped (not imaged)"]
        for cond, wells in self.used.items():
            miss = self.dropped.get(cond)
            lines.append(f"  {cond:<20} {', '.join(wells)}"
                         + (f"   << not imaged: {', '.join(miss)}" if miss else ""))
        for cond in self.skipped_conditions:
            lines.append(f"  {cond:<20} SKIPPED - no wells imaged ({', '.join(self.dropped[cond])})")
        return "\n".join(lines)


def build_input_sheet(
    plan: dict[str, list[str]],
    plate: str,
    base_folder: str,
    file_prefix: str,
    norm_condition: str,
    instrument: str = "imagexpress",
    imaged: set[str] | None = None,
    results_folder: str = "results",
) -> tuple[pd.DataFrame, BuildReport]:
    """One row per condition, wells spread over condWells1..N.

    If `imaged` is given (from wells.imaged_wells), wells without a complete
    image set are dropped and reported rather than silently breaking the run.
    Wells are written in the instrument's own naming (B02 or r02c02).
    """
    report = BuildReport()
    rows = []
    for cond, wells in plan.items():
        present = [w for w in wells if imaged is None or w in imaged]
        missing = [w for w in wells if w not in present]
        if missing:
            report.dropped[cond] = missing
        if not present:
            report.skipped_conditions.append(cond)
            continue
        report.used[cond] = present
        rec = {"saveResultsHere": results_folder, "plateStr": plate, "baseFolder": base_folder,
               "filePrefix": file_prefix, "condition": cond, "normCondition": norm_condition}
        for i, w in enumerate(present, 1):
            rec[f"condWells{i}"] = convert(w, instrument)
        rows.append(rec)

    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(columns=META_COLUMNS), report
    well_cols = sorted((c for c in df.columns if c.startswith("condWells")),
                       key=lambda c: int(c[len("condWells"):]))
    return df[META_COLUMNS + well_cols], report
