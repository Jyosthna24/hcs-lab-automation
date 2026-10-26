"""hcs-lab-automation command line.

    python -m hcsauto watch   MICROSCOPE_OUT --dest DATA --runs RUNS --plate-maps MAPS --norm WT_DMSO --pipeline "..."
    python -m hcsauto run     PLATE_MAP IMAGE_FOLDER --norm WT_DMSO -o runs/plate1 --hpc hpc.yaml --db runs.sqlite
    python -m hcsauto run     PLATE_MAP IMAGE_FOLDER --norm WT_DMSO -o runs/plate1 --cloud cloud_aws.yaml
    python -m hcsauto ingest  PLATE_FOLDER DATA
    python -m hcsauto detect  IMAGE_FOLDER
    python -m hcsauto prepare PLATE_MAP IMAGE_FOLDER --norm WT_DMSO -o runs/plate1
    python -m hcsauto report  RESULTS.xlsx --norm WT_DMSO -o runs/plate1/report
    python -m hcsauto trend   runs.sqlite --readout logMemProx -o trends/
    python -m hcsauto eln     runs/plate1
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from .detect import detect_instrument
from .ingest import ingest_plate, plate_is_complete
from .prepare import PrepareError, prepare_run
from .profiles import ProfileError
from .report import ON_FAIL, generate_report
from .watch import scan_once, watch

EXIT_REJECTED = 3


def _prepare_args(p):
    p.add_argument("plate_map", type=Path)
    p.add_argument("images", type=Path, help="raw image folder (Phenix measurement or ImageXpress plate)")
    p.add_argument("--norm", required=True, help="normalisation condition, e.g. WT_DMSO")
    p.add_argument("--pos", help="positive control for QC")
    p.add_argument("--neg", help="negative control for QC")
    p.add_argument("--config-template", type=Path, help="existing pipeline config to update")
    p.add_argument("--profiles", type=Path, help="custom instrument profiles YAML")
    p.add_argument("--min-replicates", type=int, default=1)
    p.add_argument("-o", "--outdir", type=Path, required=True)


def _qc_args(p):
    p.add_argument("--readouts", nargs="+", help="columns to report (default: logMemProx + last AACn)")
    p.add_argument("--title")
    p.add_argument("--min-cells", type=int, default=20)
    p.add_argument("--zprime-min", type=float, default=0.0, help="plates below this control Z' FAIL QC")
    p.add_argument("--on-fail", choices=ON_FAIL, default="flag",
                   help="failed plates: flag them, exclude them from results, or stop the report")


def _analysis_args(p, required=True):
    g = p.add_mutually_exclusive_group(required=required)
    g.add_argument("--pipeline", help="local analysis command; placeholders {input} {config} {results_dir}")
    g.add_argument("--hpc", type=Path, help="HPC settings YAML: run the analysis on the cluster")
    g.add_argument("--cloud", type=Path, help="cloud settings YAML: run the analysis on AWS Batch")
    p.add_argument("--results-name", default=None, help="results file name (default: newest .xlsx produced)")
    p.add_argument("--notify", type=Path, help="notification settings YAML (email / Teams)")
    p.add_argument("--db", type=Path, help="SQLite run database to record QC and results in")
    p.add_argument("--eln", action="store_true", help="also export an ELN/LIMS package")
    p.add_argument("--operator", help="name recorded in the ELN export")


def _print_report(out):
    print("\n".join(f"  {l}" for l in out["summary_lines"]))
    for k in ("excel", "prism", "slides", "summary", "figures"):
        if out.get(k):
            print(f"  {k:<8} {out[k]}")


def _run(a) -> int:
    """prepare -> analyse (local or HPC) -> report -> db / ELN / notify."""
    m = prepare_run(a.plate_map, a.images, a.outdir, a.norm, a.pos, a.neg,
                    config_template=a.config_template, profiles_file=a.profiles,
                    min_replicates=a.min_replicates)
    print("\n".join(f"  {n}" for n in m["notes"]))
    print(m["_report_text"])
    run_name = m["plate"]["name"]
    notify_cfg = None
    if a.notify:
        from .notify import load
        notify_cfg = load(a.notify)

    def tell(status, summary, outputs):
        if notify_cfg:
            from .notify import notify
            sent = notify(notify_cfg, run_name, status, summary, outputs)
            if sent:
                print(f"  notified: {', '.join(sent)}")

    results_dir = a.outdir / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    s = m["settings"]
    remote_args = (f"{run_name}_{a.outdir.name}", Path(m["outputs"]["input_file"]), Path(m["outputs"]["config"]),
                   Path(m["instrument"]["image_folder"]), Path(m["plate"]["base_folder"]) / run_name,
                   results_dir, range(int(s["first_timepoint"]), int(s["last_timepoint"]) + 1))
    try:
        if a.hpc:
            from .hpc import HPCConfig, run_on_hpc
            run_on_hpc(HPCConfig.load(a.hpc), *remote_args)
        elif a.cloud:
            from .cloud import CloudConfig, run_on_cloud
            run_on_cloud(CloudConfig.load(a.cloud), *remote_args)
        else:
            # paths are quoted here, so lab folder names with spaces just work
            cmd = a.pipeline.format(input=f'"{m["outputs"]["input_file"]}"',
                                    config=f'"{m["outputs"]["config"]}"', results_dir=f'"{results_dir}"')
            print(f"\nRunning analysis: {cmd}")
            log = a.outdir / "pipeline.log"
            with open(log, "w", encoding="utf-8") as fh:
                proc = subprocess.run(cmd, shell=True, stdout=fh, stderr=subprocess.STDOUT)
            if proc.returncode != 0:
                raise RuntimeError(f"analysis exited with code {proc.returncode}; see {log}")
    except Exception as e:
        print(f"Analysis failed: {e}", file=sys.stderr)
        tell("failed", [f"Analysis failed: {e}"], {})
        return 1

    if a.results_name:
        results = results_dir / a.results_name
    else:
        found = sorted(results_dir.rglob("*.xlsx"), key=lambda p: p.stat().st_mtime)
        if not found:
            print(f"No results .xlsx found in {results_dir}", file=sys.stderr)
            tell("failed", ["Analysis produced no results file."], {})
            return 1
        results = found[-1]
    print(f"Reporting on {results}")
    out = generate_report(results, a.outdir / "report", a.norm, a.readouts, a.pos, a.neg,
                          manifest=a.outdir / "run_manifest.json", title=a.title, min_cells=a.min_cells,
                          zprime_min=a.zprime_min, on_fail=a.on_fail)
    _print_report(out)

    if a.db:
        from .rundb import record_run
        rid = record_run(a.db, run_name, a.outdir, out,
                         json.loads((a.outdir / "run_manifest.json").read_text(encoding="utf-8")))
        print(f"  recorded in {a.db} as run {rid}")
    if a.eln:
        from .eln import export_eln
        print(f"  ELN package {export_eln(a.outdir, operator=a.operator)}")
    tell(out["status"], out["summary_lines"], out)
    return EXIT_REJECTED if out["status"] == "rejected" else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="hcsauto", description="Lab automation for high-content screening runs.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("detect", help="identify the microscope and read image metadata")
    d.add_argument("images", type=Path)

    _prepare_args(sub.add_parser("prepare", help="plate map + images -> input file, config, manifest"))

    rp = sub.add_parser("report", help="pipeline output -> Excel, Prism, figures, slides, summary")
    rp.add_argument("results", type=Path, help="pipeline output (xlsx/csv, per-cell)")
    rp.add_argument("--norm", required=True)
    rp.add_argument("--pos")
    rp.add_argument("--neg")
    rp.add_argument("--manifest", type=Path, help="run_manifest.json from the prepare stage")
    rp.add_argument("-o", "--outdir", type=Path, required=True)
    _qc_args(rp)

    r = sub.add_parser("run", help="prepare -> analyse (local or HPC) -> report, in one go")
    _prepare_args(r)
    _analysis_args(r)
    _qc_args(r)

    i = sub.add_parser("ingest", help="verified copy of a finished plate from the microscope store")
    i.add_argument("plate", type=Path)
    i.add_argument("dest", type=Path)
    i.add_argument("--verify", choices=["size", "sha256"], default="size")
    i.add_argument("--settle", type=float, default=60, help="seconds with no new files before copying")
    i.add_argument("--force", action="store_true", help="copy even if acquisition looks unfinished")

    w = sub.add_parser("watch", help="hands-off: process every plate as the microscope finishes it")
    w.add_argument("source", type=Path, help="microscope output folder (one sub-folder per plate)")
    w.add_argument("--dest", type=Path, required=True, help="analysis data store")
    w.add_argument("--runs", type=Path, required=True, help="where run folders and state are kept")
    w.add_argument("--plate-maps", type=Path, help="folder of <plate name>.csv plate maps")
    w.add_argument("--plate-map", type=Path, help="default plate map if no per-plate map exists")
    w.add_argument("--norm", required=True)
    w.add_argument("--pos")
    w.add_argument("--neg")
    w.add_argument("--config-template", type=Path)
    w.add_argument("--profiles", type=Path)
    w.add_argument("--verify", choices=["size", "sha256"], default="size")
    w.add_argument("--settle", type=float, default=60)
    w.add_argument("--interval", type=float, default=300, help="seconds between scans")
    w.add_argument("--once", action="store_true", help="scan once and exit (for scheduled tasks)")
    _analysis_args(w)
    _qc_args(w)

    t = sub.add_parser("trend", help="control chart of QC across all runs in the database")
    t.add_argument("db", type=Path)
    t.add_argument("--readout", required=True)
    t.add_argument("--metric", choices=["zprime", "ssmd"], default="zprime")
    t.add_argument("--baseline", type=int, default=10, help="runs used to set the control limits")
    t.add_argument("-o", "--outdir", type=Path, default=Path("trends"))

    e = sub.add_parser("eln", help="export a finished run as an ELN / LIMS package")
    e.add_argument("run_dir", type=Path)
    e.add_argument("--operator")
    e.add_argument("--project")

    a = ap.parse_args(argv)
    try:
        if a.cmd == "detect":
            print(json.dumps(detect_instrument(a.images).to_dict(), indent=2))
            return 0

        if a.cmd == "report":
            out = generate_report(a.results, a.outdir, a.norm, a.readouts, a.pos, a.neg,
                                  manifest=a.manifest, title=a.title, min_cells=a.min_cells,
                                  zprime_min=a.zprime_min, on_fail=a.on_fail)
            _print_report(out)
            return EXIT_REJECTED if out["status"] == "rejected" else 0

        if a.cmd == "ingest":
            ready, why = plate_is_complete(a.plate, a.settle)
            if not ready and not a.force:
                print(f"Not ingesting: {why} (use --force to override)", file=sys.stderr)
                return 2
            m = ingest_plate(a.plate, a.dest, a.verify)
            print(f"{m['copied']} copied, {m['skipped_already_present']} already present -> {m['destination']}")
            return 0

        if a.cmd == "trend":
            from .rundb import trend
            res = trend(a.db, a.readout, a.outdir, a.metric, a.baseline)
            print(f"{len(res['table'])} plate records, {res['n_out_of_control']} out of control -> {res['figure']}")
            return 0

        if a.cmd == "eln":
            from .eln import export_eln
            print(export_eln(a.run_dir, a.operator, a.project))
            return 0

        if a.cmd == "watch":
            pass_through = [("--pos", a.pos), ("--neg", a.neg), ("--profiles", a.profiles),
                            ("--config-template", a.config_template), ("--pipeline", a.pipeline),
                            ("--hpc", a.hpc), ("--cloud", a.cloud), ("--notify", a.notify), ("--db", a.db),
                            ("--operator", a.operator), ("--title", a.title),
                            ("--zprime-min", a.zprime_min), ("--on-fail", a.on_fail),
                            ("--min-cells", a.min_cells)]

            def run_plate(plate_map, images, out_dir):
                args = ["run", str(plate_map), str(images), "--norm", a.norm, "-o", str(out_dir)]
                for flag, val in pass_through:
                    if val is not None:
                        args += [flag, str(val)]
                if a.readouts:
                    args += ["--readouts", *a.readouts]
                if a.eln:
                    args.append("--eln")
                code = main(args)
                return 0 if code == EXIT_REJECTED else code      # QC rejection is a result, not a crash

            kw = dict(source_root=a.source, dest_root=a.dest, runs_root=a.runs, run_plate=run_plate,
                      plate_maps_dir=a.plate_maps, default_plate_map=a.plate_map,
                      settle_seconds=a.settle, verify=a.verify)
            if a.once:
                state = scan_once(**kw)
                return 0 if all(s.get("status") != "failed" for s in state.values()) else 1
            watch(a.interval, **kw)
            return 0

        if a.cmd == "prepare":
            m = prepare_run(a.plate_map, a.images, a.outdir, a.norm, a.pos, a.neg,
                            config_template=a.config_template, profiles_file=a.profiles,
                            min_replicates=a.min_replicates)
            print("\n".join(f"  {n}" for n in m["notes"]))
            print(m["_report_text"])
            print(f"  input file -> {m['outputs']['input_file']}\n  config     -> {m['outputs']['config']}")
            return 0

        return _run(a)
    except (PrepareError, ProfileError, ValueError, FileNotFoundError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
