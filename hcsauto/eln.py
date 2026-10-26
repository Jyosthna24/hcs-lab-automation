"""Export a finished run as an electronic-lab-notebook / LIMS record.

Writes into <run>/eln/ and zips it as <run>/eln_package.zip:

    eln_record.json        structured experiment record (who/when/what instrument, settings,
                           inputs with checksums, well reconciliation, QC verdicts, results)
    eln_record.md          the same, human-readable, ready to paste into any ELN
    lims_wells.csv         one row per well: plate, well, condition, status, reason, readouts
    ro-crate-metadata.json RO-Crate 1.1 description of the package (FAIR research-object standard)
    attachments/           report, Excel, Prism file, slides, manifest

Upload the zip to the ELN (e.g. LabArchives, Benchling, eLabFTW) or import the CSV into a LIMS.
"""
from __future__ import annotations

import getpass
import hashlib
import json
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd


def _sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def export_eln(run_dir: Path | str, operator: str | None = None, project: str | None = None) -> Path:
    run_dir = Path(run_dir)
    manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
    report = run_dir / "report"
    eln = run_dir / "eln"
    att = eln / "attachments"
    att.mkdir(parents=True, exist_ok=True)

    for f in [run_dir / "run_manifest.json", run_dir / "well_reconciliation.csv",
              *(report.glob("*.xlsx") if report.exists() else []),
              *(report.glob("*.pzfx") if report.exists() else []),
              *(report.glob("*.pptx") if report.exists() else []),
              *(report.glob("*.md") if report.exists() else [])]:
        if f.exists():
            shutil.copy2(f, att / f.name)

    qc = pd.read_excel(report / "analysis_summary.xlsx", sheet_name="Plate QC") \
        if (report / "analysis_summary.xlsx").exists() else pd.DataFrame()
    summary = pd.read_excel(report / "analysis_summary.xlsx", sheet_name="Summary")["summary"].tolist() \
        if (report / "analysis_summary.xlsx").exists() else []
    recon = pd.read_csv(run_dir / "well_reconciliation.csv").fillna("") \
        if (run_dir / "well_reconciliation.csv").exists() else pd.DataFrame()

    lims = recon.copy()
    try:
        per_well = pd.read_excel(report / "analysis_summary.xlsx", sheet_name="Per well")
        keep = [c for c in per_well.columns if c not in ("condition",)]
        lims = lims.merge(per_well[keep], on="well", how="left")
    except (FileNotFoundError, ValueError, KeyError):
        pass
    lims.insert(0, "plate", manifest.get("plate", {}).get("name"))
    lims.to_csv(eln / "lims_wells.csv", index=False)

    inst = manifest.get("instrument", {})
    record = {
        "record_type": "high-content screening run",
        "title": f"HCS run {manifest.get('plate', {}).get('name')}",
        "project": project,
        "operator": operator or getpass.getuser(),
        "exported_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "acquired_on": {k: inst.get(k) for k in ("instrument", "image_shape", "bit_depth", "pixel_size_um",
                                                 "binning", "objective", "plate_format", "wavelengths")},
        "analysis_settings": manifest.get("settings", {}),
        "inputs": manifest.get("inputs", {}),
        "software": manifest.get("tool"),
        "wells": manifest.get("wells", {}).get("status_counts", {}),
        "qc": qc.to_dict("records"),
        "summary": summary,
        "attachments": [{"name": f.name, "sha256": _sha256(f), "bytes": f.stat().st_size}
                        for f in sorted(att.iterdir())],
    }
    (eln / "eln_record.json").write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")

    md = [f"# {record['title']}", "", f"**Operator:** {record['operator']}  ",
          f"**Exported:** {record['exported_utc']}  ", f"**Software:** {record['software']}", "",
          "## Instrument", *[f"- {k}: {v}" for k, v in record["acquired_on"].items()], "",
          "## Wells", *[f"- {k}: {v}" for k, v in record["wells"].items()], "",
          "## Summary", *[f"- {s}" for s in summary], "",
          "## Attachments", *[f"- {a['name']} (sha256 {a['sha256'][:12]}…)" for a in record["attachments"]], ""]
    (eln / "eln_record.md").write_text("\n".join(md), encoding="utf-8")

    parts = [p for p in sorted(eln.rglob("*")) if p.is_file() and p.name != "ro-crate-metadata.json"]
    crate = {"@context": "https://w3id.org/ro/crate/1.1/context", "@graph": [
        {"@id": "ro-crate-metadata.json", "@type": "CreativeWork",
         "conformsTo": {"@id": "https://w3id.org/ro/crate/1.1"}, "about": {"@id": "./"}},
        {"@id": "./", "@type": "Dataset", "name": record["title"], "datePublished": record["exported_utc"],
         "author": {"@id": "#operator"}, "hasPart": [{"@id": p.relative_to(eln).as_posix()} for p in parts]},
        {"@id": "#operator", "@type": "Person", "name": record["operator"]},
        *[{"@id": p.relative_to(eln).as_posix(), "@type": "File", "contentSize": p.stat().st_size,
           "sha256": _sha256(p)} for p in parts]]}
    (eln / "ro-crate-metadata.json").write_text(json.dumps(crate, indent=2), encoding="utf-8")

    zpath = run_dir / "eln_package.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(eln.rglob("*")):
            if p.is_file():
                z.write(p, p.relative_to(eln).as_posix())
    return zpath
