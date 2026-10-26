"""Stage 3 - REPORT: pipeline output -> Excel workbook, Prism file, figures, slides, summary."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from .figures import condition_plot, plate_heatmap
from .prism import tables_from_wells, write_pzfx
from .slides import build_deck
from .summarise import (Columns, compare_to_control, default_readouts, load_results,
                        per_condition, per_well, plate_qc)


def _summary_lines(cond, stats, qc, readouts, norm, cols) -> list[str]:
    lines = [f"{qc.shape[0]} plate(s), {int(cond['n_wells'].sum())} wells, "
             f"{int(cond['n_cells'].sum()):,} cells analysed across {len(cond)} conditions."]
    n_pass = int((qc["verdict"] == "PASS").sum())
    lines.append(f"Plate QC: {n_pass}/{len(qc)} plates PASS"
                 + ("" if n_pass == len(qc) else
                    f"; check {', '.join(qc.loc[qc.verdict != 'PASS', cols.plate].astype(str))}") + ".")
    for r in readouts:
        s = stats[(stats.readout == r) & (stats.q_value < 0.05)] if len(stats) else stats
        if s is None or s.empty:
            lines.append(f"{r}: no condition differs from {norm} (FDR 5 %).")
            continue
        top = s.reindex(s["mean_diff"].abs().sort_values(ascending=False).index).head(3)
        parts = [f"{row.condition} ({'+' if row.mean_diff > 0 else ''}{row.mean_diff:.3g}"
                 + (f", {row.pct_ctrl:.0f} % of control" if np.isfinite(row.pct_ctrl) else "")
                 + f", q = {row.q_value:.2g})" for row in top.itertuples()]
        lines.append(f"{r}: {len(s)} condition(s) differ from {norm}; largest: " + "; ".join(parts) + ".")
    return lines


def _write_workbook(path: Path, sheets: dict[str, pd.DataFrame]) -> Path:
    from openpyxl.styles import Font, PatternFill
    fills = {"PASS": "C6EFCE", "FAIL": "FFC7CE", "CHECK": "FFEB9C"}
    with pd.ExcelWriter(path, engine="openpyxl") as xl:
        for name, df in sheets.items():
            df.to_excel(xl, sheet_name=name[:31], index=False)
            ws = xl.sheets[name[:31]]
            for c in ws[1]:
                c.font = Font(bold=True)
            for col in ws.columns:
                width = max(len(str(c.value)) if c.value is not None else 0 for c in col)
                ws.column_dimensions[col[0].column_letter].width = min(max(10, width + 2), 50)
            if "verdict" in df.columns:
                j = df.columns.get_loc("verdict") + 1
                for i, v in enumerate(df["verdict"], start=2):
                    ws.cell(i, j).fill = PatternFill("solid", fgColor=fills.get(v, "FFFFFF"))
            ws.freeze_panes = "A2"
    return path


ON_FAIL = ("flag", "exclude", "stop")


def generate_report(results: Path | str, out_dir: Path | str, norm_condition: str,
                    readouts: list[str] | None = None, pos: str | None = None, neg: str | None = None,
                    manifest: dict | Path | str | None = None, title: str | None = None,
                    min_cells: int = 20, zprime_min: float = 0.0, on_fail: str = "flag",
                    cols: Columns = Columns()) -> dict:
    """on_fail decides what happens to plates whose control Z' is below zprime_min:
    flag    - report everything, failed plates clearly marked
    exclude - drop failed plates from statistics, Prism tables and figures
    stop    - write only the QC workbook and summary; no results are released
    """
    if on_fail not in ON_FAIL:
        raise ValueError(f"on_fail must be one of {ON_FAIL}")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if isinstance(manifest, (str, Path)):
        manifest = json.loads(Path(manifest).read_text(encoding="utf-8"))
    manifest = manifest or {}
    if pos is None:
        pos = manifest.get("settings", {}).get("positive_control")
    if neg is None:
        neg = manifest.get("settings", {}).get("negative_control")

    cells = load_results(results)
    readouts = readouts or default_readouts(cells)
    all_wells = per_well(cells, readouts, norm_condition, cols)
    qc = plate_qc(all_wells, readouts, pos, neg, min_cells=min_cells, zprime_min=zprime_min, cols=cols)
    failed = qc.loc[qc["verdict"] == "FAIL", cols.plate].astype(str).tolist()
    status = "ok" if not failed else {"flag": "flagged", "exclude": "excluded", "stop": "rejected"}[on_fail]
    wells = all_wells[~all_wells[cols.plate].astype(str).isin(failed)] if on_fail == "exclude" else all_wells
    if wells.empty:
        status, wells = "rejected", all_wells
    cond = per_condition(wells, readouts, cols)
    stats = compare_to_control(wells, readouts, norm_condition, cols)
    summary = _summary_lines(cond, stats, qc, readouts, norm_condition, cols)
    if failed:
        msg = {"flagged": "results include them - interpret with care",
               "excluded": "excluded from statistics, Prism tables and figures",
               "rejected": "results NOT released; repeat or review the plate(s)"}[status]
        summary.insert(0, f"⚠ QC REJECTED: {', '.join(failed)} (control Z′ < {zprime_min}) - {msg}.")

    inst = manifest.get("instrument", {})
    run_info = pd.DataFrame({"item": ["results file", "generated", "instrument", "pixel size (µm)",
                                      "normalised to", "positive control", "negative control", "readouts"],
                             "value": [str(results), datetime.now().isoformat(timespec="seconds"),
                                       inst.get("instrument", "n/a"), inst.get("pixel_size_um", "n/a"),
                                       norm_condition, pos or "n/a", neg or "n/a", ", ".join(readouts)]})
    if status == "rejected":
        xlsx = _write_workbook(out_dir / "analysis_summary.xlsx", {
            "Summary": pd.DataFrame({"summary": summary}), "Plate QC": qc, "Run info": run_info})
        md = out_dir / "summary.md"
        md.write_text("\n".join(["# QC rejected", "", *[f"- {l}" for l in summary], ""]), encoding="utf-8")
        return {"status": status, "failed_plates": failed, "excel": xlsx, "prism": None, "slides": None,
                "summary": md, "figures": None, "summary_lines": summary, "plate_qc": qc,
                "per_condition": cond}

    xlsx = _write_workbook(out_dir / "analysis_summary.xlsx", {
        "Summary": pd.DataFrame({"summary": summary}), "Per condition": cond, "Statistics": stats,
        "Plate QC": qc, "Per well": all_wells, "Run info": run_info})

    pzfx = write_pzfx(tables_from_wells(wells, readouts, cols.condition), out_dir / "prism_tables.pzfx",
                      notes=" | ".join(summary))

    fig_dir = out_dir / "figures"
    figures = []
    for r in readouts:
        png = condition_plot(wells, r, fig_dir, cols.condition, control=norm_condition)
        st = stats[stats.readout == r] if len(stats) else stats
        n_sig = int((st.q_value < 0.05).sum()) if len(st) else 0
        figures.append((r, png, f"Bars: mean ± SEM of well means; points: wells. "
                                f"{n_sig} condition(s) differ from {norm_condition} (Welch t-test, BH-FDR 5 %)."))
    for plate in wells[cols.plate].unique():
        png = plate_heatmap(wells, plate, "n_cells", fig_dir, cols.plate, cols.well)
        figures.append((f"Cells per well: {plate}", png, "Plate layout view of segmented cells per well."))

    qc_lines = []
    for row in qc.to_dict("records"):
        z = [f"{r} Z′ = {row[f'{r}_zprime']:.2f}" for r in readouts if f"{r}_zprime" in row
             and np.isfinite(row[f"{r}_zprime"])]
        qc_lines.append(f"{row[cols.plate]}: {row['verdict']} — median {row['median_cells_per_well']:.0f} "
                        f"cells/well" + (f"; {'; '.join(z)}" if z else ""))
    title = title or f"Screening run report: {', '.join(map(str, all_wells[cols.plate].unique()))}"
    if failed:
        title = f"[QC FAIL: {', '.join(failed)}] " + title
    subtitle = (f"{inst.get('instrument', 'instrument n/a')} · generated "
                f"{datetime.now():%d %b %Y} by hcs-lab-automation")
    pptx = build_deck(out_dir / "run_report.pptx", title, subtitle, summary, qc_lines, figures)

    md = out_dir / "summary.md"
    md.write_text("\n".join([f"# {title}", "", subtitle, "", "## Summary", *[f"- {l}" for l in summary],
                             "", "## Plate QC", *[f"- {l}" for l in qc_lines], ""]), encoding="utf-8")

    return {"status": status, "failed_plates": failed, "excel": xlsx, "prism": pzfx, "slides": pptx,
            "summary": md, "figures": fig_dir, "summary_lines": summary, "plate_qc": qc,
            "per_condition": cond}
