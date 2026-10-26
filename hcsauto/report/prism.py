"""Write GraphPad Prism projects (.pzfx, Prism's open XML format) without Prism installed.

Each readout becomes one Column data table: one column per condition, one row
per well (replicate). Open the file in Prism and the tables are ready to graph
and analyse with the lab's usual Prism templates.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

import numpy as np


def _cell(v) -> str:
    return "<d/>" if v is None or (isinstance(v, float) and not np.isfinite(v)) else f"<d>{float(v):.6g}</d>"


def write_pzfx(tables: dict[str, dict[str, list[float]]], path: Path | str, notes: str = "") -> Path:
    """tables = {table title: {column title: [values]}}"""
    path = Path(path)
    ids = [f"Table{i}" for i in range(len(tables))]
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        # header mirrors what Prism itself writes, so every Prism version (>= 5) opens it
        '<GraphPadPrismFile PrismXMLVersion="5.00">',
        '<Created><OriginalVersion CreatedByProgram="GraphPad Prism" CreatedByVersion="6.0f.254" '
        f'Login="" DateTime="{datetime.now().isoformat(timespec="seconds")}"/></Created>',
        '<InfoSequence><Ref ID="Info0" Selected="1"/></InfoSequence>',
        f'<Info ID="Info0"><Title>Run info (hcs-lab-automation)</Title><Notes>{escape(notes)}</Notes></Info>',
        '<TableSequence Selected="1">'
        + "".join('<Ref ID="%s"%s/>' % (i, ' Selected="1"' if k == 0 else "") for k, i in enumerate(ids))
        + "</TableSequence>",
    ]
    for tid, (title, columns) in zip(ids, tables.items()):
        parts.append(f'<Table ID="{tid}" XFormat="none" TableType="OneWay" EVFormat="AsteriskAfterNumber">')
        parts.append(f"<Title>{escape(title)}</Title>")
        for col, values in columns.items():
            parts.append('<YColumn Width="120" Decimals="4" Subcolumns="1">')
            parts.append(f"<Title>{escape(str(col))}</Title><Subcolumn>")
            parts.append("".join(_cell(v) for v in values))
            parts.append("</Subcolumn></YColumn>")
        parts.append("</Table>")
    parts.append("</GraphPadPrismFile>")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(parts), encoding="utf-8")
    return path


def tables_from_wells(wells, readouts, condition_col: str = "condition") -> dict:
    """Well means per condition, one Prism table per readout (plus % of control)."""
    tables = {}
    for r in readouts:
        for col, title in ((r, r), (f"{r}_pct_ctrl", f"{r} (% of control)")):
            if col in wells and wells[col].notna().any():
                tables[title] = {c: s[col].tolist() for c, s in wells.groupby(condition_col, sort=False)}
    return tables
