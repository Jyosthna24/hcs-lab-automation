"""Read and validate a plate map drawn as a grid, the way it is planned at the bench.

The plate map is a CSV laid out like the plate itself:

    ,1,2,3,4,...
    A,,,,
    B,,WT_DMSO,WT_DMSO,WT_DMSO,...
    C,,WT_ETI,WT_ETI,WT_ETI,...

Empty cells are unused wells. Every non-empty cell is a condition label.
"""
from __future__ import annotations

from collections import OrderedDict
from pathlib import Path

import pandas as pd

from .wells import ROW_LETTERS, format_well

PLATE_SHAPES = {96: (8, 12), 384: (16, 24)}


def read_plate_map(path: Path | str) -> "OrderedDict[str, list[str]]":
    """Return {condition: [wells]} in plate order (row by row, left to right)."""
    grid = pd.read_csv(path, index_col=0, dtype=str)
    grid.index = grid.index.str.strip().str.upper()
    grid.columns = [int(str(c).strip()) for c in grid.columns]
    return grid_to_plan(grid)


def grid_to_plan(grid: pd.DataFrame) -> "OrderedDict[str, list[str]]":
    n_rows, n_cols = grid.shape
    shape = next((s for s, (r, c) in PLATE_SHAPES.items() if n_rows <= r and n_cols <= c), None)
    if shape is None:
        raise ValueError(f"plate map is {n_rows}x{n_cols}; larger than a 384-well plate")
    bad_rows = [r for r in grid.index if r not in ROW_LETTERS[: PLATE_SHAPES[shape][0]]]
    if bad_rows:
        raise ValueError(f"unexpected row labels: {bad_rows}")

    plan: "OrderedDict[str, list[str]]" = OrderedDict()
    for row_label, row in grid.iterrows():
        r = ROW_LETTERS.index(row_label) + 1
        for c, value in row.items():
            if pd.isna(value) or not str(value).strip():
                continue
            plan.setdefault(str(value).strip(), []).append(format_well(r, int(c)))
    if not plan:
        raise ValueError("plate map has no conditions")
    return plan


def validate(plan: dict[str, list[str]], norm_condition: str | None = None,
             min_replicates: int = 1) -> list[str]:
    """Return human-readable problems; empty list means the plan is usable."""
    problems = []
    if norm_condition and norm_condition not in plan:
        problems.append(f"normalisation condition '{norm_condition}' is not on the plate")
    for cond, wells in plan.items():
        if len(wells) < min_replicates:
            problems.append(f"{cond}: only {len(wells)} well(s), need >= {min_replicates}")
    # catch near-duplicate labels, a common typo source (e.g. 'WT_DMSO' vs 'WT_dmso')
    seen = {}
    for cond in plan:
        key = cond.lower().replace(" ", "").replace("-", "_")
        if key in seen and seen[key] != cond:
            problems.append(f"labels '{seen[key]}' and '{cond}' look like the same condition")
        seen.setdefault(key, cond)
    return problems
