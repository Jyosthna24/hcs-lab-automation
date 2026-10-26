import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hcsauto.demo import fake_imagexpress, fake_phenix, fake_results  # noqa: E402
from hcsauto.detect import detect_instrument  # noqa: E402
from hcsauto.ingest import ingest_plate, plate_is_complete  # noqa: E402
from hcsauto.metaxpress import read_htd, write_htd  # noqa: E402
from hcsauto.watch import scan_once  # noqa: E402
from hcsauto.plateplan import read_plate_map  # noqa: E402
from hcsauto.prepare import PrepareError, prepare_run  # noqa: E402
from hcsauto.profiles import (DEFAULT_PROFILES, ProfileError, load_profiles,  # noqa: E402
                              resolve_settings, write_pipeline_config)
from hcsauto.report import generate_report  # noqa: E402
from hcsauto.report.summarise import bh_fdr  # noqa: E402
from hcsauto.__main__ import main  # noqa: E402

PLATE_MAP = ROOT / "examples" / "demo_plate_map.csv"
WELLS = [w for ws in read_plate_map(PLATE_MAP).values() for w in ws]
TPS = range(1, 6)


@pytest.fixture
def short_profiles(tmp_path):
    """Default profiles with a short analysis window, so test image folders stay tiny."""
    p = load_profiles()
    for inst in p.values():
        inst.update(first_timepoint=2, last_timepoint=5, reference_timepoint=3)
    f = tmp_path / "profiles.yaml"
    f.write_text(yaml.safe_dump(p))
    return f


# ---------------------------------------------------------------- detection
def test_detects_phenix_and_reads_index_xml(tmp_path):
    fake_phenix(tmp_path / "M1", WELLS[:3], TPS, size=24, pixel_um=0.598, binning=2)
    info = detect_instrument(tmp_path / "M1")
    assert info.instrument == "phenix"
    assert info.pixel_size_um == pytest.approx(0.598)
    assert info.binning == 2 and info.objective == 20
    assert info.image_shape == (24, 24) and info.bit_depth == 16
    assert info.timepoints == list(TPS) and info.channels == [1, 2]
    assert info.wells == sorted(WELLS[:3])


def test_detects_imagexpress_and_reads_metamorph_tags(tmp_path):
    fake_imagexpress(tmp_path / "P1", WELLS[:2], TPS, pixel_um=0.645)
    info = detect_instrument(tmp_path / "P1")
    assert info.instrument == "imagexpress"
    assert info.pixel_size_um == pytest.approx(0.645)
    assert info.sources["pixel_size_um"] == "tiff MetaMorph metadata"
    assert info.timepoints == list(TPS)


def test_unknown_folder_is_rejected(tmp_path):
    (tmp_path / "junk.txt").write_text("x")
    with pytest.raises(ValueError):
        detect_instrument(tmp_path)


# ---------------------------------------------------------------- profiles
def test_profile_differs_by_instrument_and_rescales_pixels(tmp_path):
    fake_phenix(tmp_path / "M", WELLS[:1], range(1, 34), pixel_um=0.6)
    fake_imagexpress(tmp_path / "P", WELLS[:1], range(1, 34), pixel_um=0.3)
    ph, notes = resolve_settings(detect_instrument(tmp_path / "M"))
    ix, _ = resolve_settings(detect_instrument(tmp_path / "P"))
    assert (ph["lower_red_bound"], ph["upper_red_bound"]) == (0.5, 2.5)
    assert (ix["lower_red_bound"], ix["upper_red_bound"]) == (1.0, 4.0)
    assert ph["diameter"] == pytest.approx(47.236 / 2, abs=1e-3)   # 2x bigger pixels -> half the px diameter
    assert ix["diameter"] == pytest.approx(47.236, abs=1e-3)
    assert ph["min_size"] == 4                                      # 15 / 4, rounded
    assert any("rescaled" in n for n in notes)


def test_analysis_window_must_exist_in_the_data(tmp_path):
    fake_imagexpress(tmp_path / "P", WELLS[:1], TPS)
    with pytest.raises(ProfileError):
        resolve_settings(detect_instrument(tmp_path / "P"))          # default window 10-33 not acquired


def test_config_template_is_updated_not_replaced(tmp_path):
    tpl = tmp_path / "tpl.csv"
    tpl.write_text("variable_name,description,value\nmax_workers,cores,8\nlower_red_bound,red lo,1\n")
    out = write_pipeline_config({"lower_red_bound": 0.5, "pixel_size_um": 0.6}, tmp_path / "c.csv", tpl)
    cfg = pd.read_csv(out).set_index("variable_name")
    assert cfg.loc["max_workers", "value"] == "8" or cfg.loc["max_workers", "value"] == 8
    assert float(cfg.loc["lower_red_bound", "value"]) == 0.5
    assert cfg.loc["lower_red_bound", "description"] == "red lo"
    assert float(cfg.loc["pixel_size_um", "value"]) == 0.6


# ---------------------------------------------------------------- prepare
def test_prepare_phenix_run_folder(tmp_path, short_profiles):
    img = fake_phenix(tmp_path / "raw" / "Plate_9", [w for w in WELLS if w != "C03"], TPS)
    m = prepare_run(PLATE_MAP, img, tmp_path / "run", "WT_DMSO", "WT_DMSO", "F508del_DMSO",
                    profiles_file=short_profiles)
    df = pd.read_excel(m["outputs"]["input_file"])
    assert df.loc[0, "condWells1"].startswith("r0")                  # Phenix naming
    assert m["wells"]["dropped_not_imaged"] == {"WT_ETI": ["C03"]}
    assert m["plate"]["name"] == "Plate_9" and m["plate"]["file_prefix"] == "f01p01-"
    saved = json.loads((tmp_path / "run" / "run_manifest.json").read_text())
    assert saved["instrument"]["instrument"] == "phenix"
    assert len(saved["inputs"]["plate_map_sha256"]) == 64
    cfg = pd.read_csv(m["outputs"]["config"]).set_index("variable_name")["value"]
    assert cfg["microscope_type"] == "opera phenix"


def test_prepare_rejects_missing_control(tmp_path, short_profiles):
    img = fake_imagexpress(tmp_path / "P", WELLS, TPS)
    with pytest.raises(PrepareError):
        prepare_run(PLATE_MAP, img, tmp_path / "run", "WT_DMSO", neg_control="R117H_DMSO",
                    profiles_file=short_profiles)


# ---------------------------------------------------------------- report
def test_bh_fdr_matches_reference():
    q = bh_fdr(np.array([0.01, 0.04, 0.03, 0.005]))
    assert q == pytest.approx([0.02, 0.04, 0.04, 0.02])


def test_report_produces_every_output(tmp_path):
    res = tmp_path / "results.xlsx"
    with pd.ExcelWriter(res) as xl:
        fake_results(PLATE_MAP).to_excel(xl, sheet_name="full_results", index=False)
    out = generate_report(res, tmp_path / "report", "WT_DMSO", pos="WT_DMSO", neg="F508del_DMSO")

    xl = pd.ExcelFile(out["excel"])
    assert {"Summary", "Per condition", "Statistics", "Plate QC", "Per well", "Run info"} <= set(xl.sheet_names)
    stats = pd.read_excel(out["excel"], sheet_name="Statistics")
    f508 = stats[(stats.condition == "F508del_DMSO") & (stats.readout == "logMemProx")]
    assert f508.q_value.item() < 0.05 and f508.mean_diff.item() < 0      # planted effect found

    tree = ET.parse(out["prism"])
    assert len(tree.getroot().findall("Table")) == 3                      # logMemProx, its %ctrl, AACn_43

    from pptx import Presentation
    n_slides = len(Presentation(out["slides"]).slides)
    assert n_slides == 3 + 2 + 2                                          # title/summary/QC + 2 readouts + 2 plates
    assert (out["figures"] / "readout_logMemProx.svg").exists()
    assert "Plate QC" in out["summary"].read_text(encoding="utf-8")
    assert set(out["plate_qc"]["verdict"]) <= {"PASS", "CHECK", "FAIL"}


# ---------------------------------------------------------------- MetaXpress + well reconciliation
def test_htd_round_trip(tmp_path):
    f = write_htd(tmp_path / "p.HTD", ["B02", "C03", "H12"], 33, ["FITC", "Texas Red"])
    h = read_htd(f)
    assert h.plate_format == 96 and h.timepoints == 33
    assert h.selected_wells == ["B02", "C03", "H12"] and h.wavelengths == ["FITC", "Texas Red"]


def test_detection_uses_metaxpress_metadata(tmp_path):
    fake_imagexpress(tmp_path / "P", WELLS[:2], TPS, selected=WELLS[:3])
    info = detect_instrument(tmp_path / "P")
    assert info.plate_format == 96 and info.expected_timepoints == len(TPS)
    assert info.selected_wells == sorted(WELLS[:3], key=WELLS.index) or set(info.selected_wells) == set(WELLS[:3])
    assert info.sources["selected_wells"].startswith("MetaXpress")


def test_wells_are_reconciled_automatically(tmp_path, short_profiles):
    selected = [w for w in WELLS if w != "E05"]                 # E05 never ticked in MetaXpress
    written = [w for w in selected if w != "D03"] + ["G11"]     # D03 microscope fault; G11 not on plan
    img = fake_imagexpress(tmp_path / "Plate_7", written, TPS, selected=selected + ["G11"])
    (img / "TimePoint_3" / "Plate_1_C02_w2.TIF").unlink()      # C02 lost one frame
    m = prepare_run(PLATE_MAP, img, tmp_path / "run", "WT_DMSO", profiles_file=short_profiles)
    rec = pd.read_csv(tmp_path / "run" / "well_reconciliation.csv").set_index("well")
    assert rec.loc["E05", "status"] == "not_acquired"
    assert rec.loc["D03", "status"] == "acquisition_fault"
    assert rec.loc["C02", "status"] == "acquisition_fault" and "incomplete" in rec.loc["C02", "reason"]
    assert rec.loc["G11", "status"] == "unplanned"
    assert (rec.status == "analysed").sum() == len(WELLS) - 3
    used = {w for ws in m["wells"]["used"].values() for w in ws}
    assert not {"E05", "D03", "C02", "G11"} & used                # removed from the input file


# ---------------------------------------------------------------- ingest + watch
def test_plate_completion_detection(tmp_path):
    img = fake_imagexpress(tmp_path / "P", WELLS, TPS)
    assert plate_is_complete(img, settle_seconds=0)[0]
    write_htd(img / "Plate_1.HTD", WELLS, timepoints=10)       # microscope still has 5 to go
    ok, why = plate_is_complete(img, settle_seconds=0)
    assert not ok and "5/10" in why
    assert not plate_is_complete(tmp_path, settle_seconds=0)[0]


def test_ingest_copies_verifies_and_resumes(tmp_path):
    src = fake_imagexpress(tmp_path / "scope" / "Plate_1", WELLS[:4], TPS)
    m1 = ingest_plate(src, tmp_path / "store", verify="sha256")
    n_files = sum(1 for p in src.rglob("*") if p.is_file())
    assert m1["copied"] == n_files and m1["skipped_already_present"] == 0
    assert (tmp_path / "store" / "Plate_1" / "Plate_1.HTD").exists()
    m2 = ingest_plate(src, tmp_path / "store")
    assert m2["copied"] == 0 and m2["skipped_already_present"] == n_files
    assert not list((tmp_path / "store").rglob("*.partial"))


def test_watch_processes_only_finished_plates(tmp_path):
    scope = tmp_path / "scope"
    fake_imagexpress(scope / "Plate_A", WELLS, TPS)
    busy = fake_imagexpress(scope / "Plate_B", WELLS, TPS)
    write_htd(busy / "Plate_1.HTD", WELLS, timepoints=20)
    fake_imagexpress(scope / "Plate_C", WELLS, TPS)
    maps = tmp_path / "maps"
    maps.mkdir()
    for p in ("Plate_A", "Plate_B"):
        (maps / f"{p}.csv").write_text(PLATE_MAP.read_text())
    calls = []
    state = scan_once(scope, tmp_path / "store", tmp_path / "runs",
                      run_plate=lambda pm, img, out: calls.append((pm.name, Path(img).name)) or 0,
                      plate_maps_dir=maps, settle_seconds=0, log=lambda s: None)
    assert calls == [("Plate_A.csv", "Plate_A")]
    assert state["Plate_A"]["status"] == "done"
    assert "Plate_B" not in state                                # still acquiring, try next scan
    assert state["Plate_C"]["status"] == "needs_plate_map"
    scan_once(scope, tmp_path / "store", tmp_path / "runs", run_plate=lambda *a: calls.append(a) or 0,
              plate_maps_dir=maps, settle_seconds=0, log=lambda s: None)
    assert len(calls) == 1                                       # Plate_A not processed twice


# ---------------------------------------------------------------- HPC submission
class FakeCluster:
    """Stands in for SSH + SGE: files land in a local 'remote' folder, qsub runs the job at once."""

    def __init__(self):
        from hcsauto.hpc import LocalTransport
        self._t = LocalTransport()
        self.put_files, self.submitted = [], []
        self.mkdir, self.get, self.listdir = self._t.mkdir, self._t.get, self._t.listdir

    def put(self, local, remote):
        self.put_files.append(remote)
        self._t.put(local, remote)

    def run(self, cmd):
        import re
        import subprocess
        if "qsub" in cmd:
            root = re.search(r"cd '(.*?)'", cmd).group(1)
            script = Path(root, "job.sh").read_text().splitlines()
            assert any(l.startswith("#$ -l h_rt=") for l in script)
            job = [l for l in script if l.strip()][-1]
            self.submitted.append(job)
            p = subprocess.run(job, shell=True, capture_output=True, text=True)
            assert p.returncode == 0, p.stdout + p.stderr
            return 0, 'Your job 4242 ("job") has been submitted'
        if "qstat" in cmd:
            return 1, ""                                    # finished
        return self._t.run(cmd)

    def close(self):
        pass


def test_hpc_uploads_only_needed_images_and_collects_results(tmp_path, short_profiles):
    from hcsauto.hpc import HPCConfig, run_on_hpc
    img = fake_imagexpress(tmp_path / "raw" / "Plate_1", WELLS + ["H12"], TPS)   # H12 not on plan
    m = prepare_run(PLATE_MAP, img, tmp_path / "run", "WT_DMSO", profiles_file=short_profiles)
    fake = ROOT / "scripts" / "fake_pipeline.py"
    cfg = HPCConfig(host="cluster", remote_root=str(tmp_path / "cluster"), poll_seconds=0,
                    command=f'"{sys.executable}" "{fake}" --input {{input}} --config {{config}} --out {{results_dir}}')
    cluster = FakeCluster()
    out = run_on_hpc(cfg, "Plate_1_run", Path(m["outputs"]["input_file"]), Path(m["outputs"]["config"]),
                     img, img, tmp_path / "run" / "results", range(2, 6), transport=cluster, log=lambda s: None)
    assert (out / "results.xlsx").exists()
    tifs = [p for p in cluster.put_files if p.upper().endswith(".TIF")]
    assert tifs and not any("_H12_" in p for p in tifs)                           # unneeded well skipped
    assert not any("TimePoint_1/" in p.replace("\\", "/") for p in tifs)           # outside window skipped
    assert any(p.endswith(".HTD") for p in cluster.put_files)                       # metadata travels too
    remote_input = pd.read_excel(tmp_path / "cluster" / "Plate_1_run" / "input_file.xlsx")
    assert remote_input["baseFolder"].str.contains("cluster").all()                 # paths rewritten


# ---------------------------------------------------------------- QC rejection
def _results_with_bad_plate(path):
    df = fake_results(PLATE_MAP)
    bad = (df.plate_name == "Plate_2") & (df.condition == "F508del_DMSO")
    wt = df[(df.plate_name == "Plate_2") & (df.condition == "WT_DMSO")]
    rng = np.random.default_rng(3)
    df.loc[bad, "logMemProx"] = rng.choice(wt.logMemProx, bad.sum())               # controls no longer separate
    df.loc[bad, "AACn_43"] = rng.choice(wt.AACn_43, bad.sum())
    with pd.ExcelWriter(path) as xl:
        df.to_excel(xl, sheet_name="full_results", index=False)
    return path


@pytest.mark.parametrize("mode,status", [("flag", "flagged"), ("exclude", "excluded"), ("stop", "rejected")])
def test_failed_plate_handling(tmp_path, mode, status):
    res = _results_with_bad_plate(tmp_path / "r.xlsx")
    out = generate_report(res, tmp_path / mode, "WT_DMSO", pos="WT_DMSO", neg="F508del_DMSO", on_fail=mode)
    assert out["status"] == status and out["failed_plates"] == ["Plate_2"]
    assert out["summary_lines"][0].startswith("⚠ QC REJECTED: Plate_2")
    if mode == "exclude":
        assert (out["per_condition"]["n_plates"] == 1).all()
    if mode == "stop":
        assert out["slides"] is None and not (tmp_path / mode / "run_report.pptx").exists()
        assert "Statistics" not in pd.ExcelFile(out["excel"]).sheet_names


# ---------------------------------------------------------------- notifications
def test_email_and_teams_notifications(monkeypatch, tmp_path):
    from hcsauto import notify as nt
    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            sent["host"] = host
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def starttls(self):
            sent["tls"] = True
        def login(self, u, p):
            sent["login"] = u
        def send_message(self, msg):
            sent["subject"] = msg["Subject"]
            sent["attachments"] = [p.get_filename() for p in msg.iter_attachments()]

    class FakeResp:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout):
        sent["teams"] = json.loads(req.data)
        return FakeResp()

    monkeypatch.setattr(nt.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(nt.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("U", "bot")
    monkeypatch.setenv("P", "secret")
    monkeypatch.setenv("HOOK", "https://example.invalid/webhook")
    deck = tmp_path / "run_report.pptx"
    deck.write_bytes(b"pptx")
    cfg = {"email": {"smtp_host": "smtp.test", "sender": "a@test", "to": ["b@test"], "username_env": "U",
                     "password_env": "P", "attach_deck": True}, "teams": {"webhook_env": "HOOK"}}
    got = nt.notify(cfg, "Plate_1", "flagged", ["2 plates", "QC flagged"], {"slides": deck}, log=lambda s: None)
    assert got == ["email", "teams"]
    assert sent["tls"] and sent["login"] == "bot" and "FLAGGED" in sent["subject"]
    assert sent["attachments"] == ["run_report.pptx"]
    assert sent["teams"]["attachments"][0]["content"]["type"] == "AdaptiveCard"


def test_notification_failure_does_not_crash(monkeypatch):
    from hcsauto import notify as nt
    logs = []
    got = nt.notify({"teams": {"webhook_env": "NOT_SET_ANYWHERE"}}, "P", "ok", [], {}, log=logs.append)
    assert got == [] and "teams failed" in logs[0]


# ---------------------------------------------------------------- run database + SPC
def test_spc_flags_outlier_and_drift():
    from hcsauto.rundb import spc_flags
    v = np.r_[np.random.default_rng(0).normal(0.6, 0.03, 10), 0.2, [0.56] * 7]
    f = spc_flags(v, baseline=10)
    assert f["beyond_3sd"].iloc[10]
    assert f["drift_run"].iloc[-7:].all()
    assert not f["beyond_3sd"].iloc[:10].any()


def test_run_database_records_and_trends(tmp_path):
    from hcsauto.rundb import connect, record_run, trend
    db = tmp_path / "runs.sqlite"
    res = tmp_path / "r.xlsx"
    with pd.ExcelWriter(res) as xl:
        fake_results(PLATE_MAP).to_excel(xl, sheet_name="full_results", index=False)
    for i in range(3):
        out = generate_report(res, tmp_path / f"rep{i}", "WT_DMSO", pos="WT_DMSO", neg="F508del_DMSO")
        record_run(db, f"run{i}", tmp_path, out)
    con = connect(db)
    assert con.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 3
    n_qc = con.execute("SELECT COUNT(*) FROM plate_qc WHERE readout='logMemProx'").fetchone()[0]
    assert n_qc == 6                                                              # 3 runs x 2 plates
    mean_wt = con.execute("SELECT AVG(mean) FROM results WHERE condition='WT_DMSO' AND readout='AACn_43'"
                          ).fetchone()[0]
    assert 10 < mean_wt < 14
    con.close()
    t = trend(db, "logMemProx", tmp_path / "trend")
    assert t["figure"].exists() and len(t["table"]) == 6


# ---------------------------------------------------------------- end to end
def test_cli_run_end_to_end(tmp_path, short_profiles):
    img = fake_imagexpress(tmp_path / "raw" / "Plate_1", WELLS, TPS)
    fake = ROOT / "scripts" / "fake_pipeline.py"
    code = main(["run", str(PLATE_MAP), str(img), "--norm", "WT_DMSO", "--pos", "WT_DMSO",
                 "--neg", "F508del_DMSO", "--profiles", str(short_profiles), "-o", str(tmp_path / "run"),
                 "--pipeline", f'"{sys.executable}" "{fake}" --input {{input}} --config {{config}} '
                               "--out {results_dir}"])
    assert code == 0, (tmp_path / "run" / "pipeline.log").read_text()
    for f in ("analysis_summary.xlsx", "prism_tables.pzfx", "run_report.pptx", "summary.md"):
        assert (tmp_path / "run" / "report" / f).exists()


def test_cli_watch_once_hands_off(tmp_path, short_profiles):
    fake_imagexpress(tmp_path / "scope" / "Plate_1", WELLS, TPS)
    fake = ROOT / "scripts" / "fake_pipeline.py"
    code = main(["watch", str(tmp_path / "scope"), "--dest", str(tmp_path / "store"),
                 "--runs", str(tmp_path / "runs"), "--plate-map", str(PLATE_MAP), "--norm", "WT_DMSO",
                 "--profiles", str(short_profiles), "--settle", "0", "--once",
                 "--pipeline", f'"{sys.executable}" "{fake}" --input {{input}} --config {{config}} '
                               "--out {results_dir}"])
    assert code == 0
    assert (tmp_path / "store" / "Plate_1" / "ingest_manifest.json").exists()
    assert (tmp_path / "runs" / "Plate_1" / "report" / "run_report.pptx").exists()


def test_cli_run_records_db_and_exports_eln(tmp_path, short_profiles):
    import sqlite3
    import zipfile
    img = fake_imagexpress(tmp_path / "raw" / "Plate_1", WELLS, TPS)
    fake = ROOT / "scripts" / "fake_pipeline.py"
    db = tmp_path / "runs.sqlite"
    code = main(["run", str(PLATE_MAP), str(img), "--norm", "WT_DMSO", "--pos", "WT_DMSO",
                 "--neg", "F508del_DMSO", "--profiles", str(short_profiles), "-o", str(tmp_path / "run"),
                 "--db", str(db), "--eln", "--operator", "Test Operator",
                 "--pipeline", f'"{sys.executable}" "{fake}" --input {{input}} --config {{config}} '
                               "--out {results_dir}"])
    assert code == 0
    assert sqlite3.connect(db).execute("SELECT status FROM runs").fetchone()[0] == "ok"
    with zipfile.ZipFile(tmp_path / "run" / "eln_package.zip") as z:
        names = set(z.namelist())
        record = json.loads(z.read("eln_record.json"))
        crate = json.loads(z.read("ro-crate-metadata.json"))
    assert {"eln_record.md", "lims_wells.csv", "attachments/run_manifest.json"} <= names
    assert record["operator"] == "Test Operator" and record["acquired_on"]["instrument"] == "imagexpress"
    assert all(len(a["sha256"]) == 64 for a in record["attachments"])
    assert crate["@context"].startswith("https://w3id.org/ro/crate/1.1")
