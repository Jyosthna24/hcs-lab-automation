"""Hands-off mode: watch the microscope's output folder and process each plate when it finishes.

    new plate folder appears -> wait until acquisition is complete -> ingest (verified copy)
    -> find its plate map -> prepare -> analyse -> report -> record in state file

Plate maps are matched by name: <plate_maps_dir>/<plate folder name>.csv, else a
default map. Plates without a map wait (status 'needs_plate_map') instead of failing.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .ingest import image_folder_for, ingest_plate, plate_is_complete

STATE_FILE = "hcsauto_state.json"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def scan_once(
    source_root: Path | str,
    dest_root: Path | str,
    runs_root: Path | str,
    run_plate: Callable[[Path, Path, Path], int],
    plate_maps_dir: Path | str | None = None,
    default_plate_map: Path | str | None = None,
    settle_seconds: float = 60,
    verify: str = "size",
    log: Callable[[str], None] = print,
) -> dict:
    """One pass over source_root. run_plate(plate_map, image_folder, out_dir) -> exit code."""
    source_root, dest_root, runs_root = Path(source_root), Path(dest_root), Path(runs_root)
    state_path = runs_root / STATE_FILE
    state = _load(state_path)
    for plate in sorted(p for p in source_root.iterdir() if p.is_dir()):
        prev = state.get(plate.name, {})
        if prev.get("status") in ("done", "failed"):
            continue
        ready, why = plate_is_complete(plate, settle_seconds)
        if not ready:
            log(f"[wait] {plate.name}: {why}")
            continue
        pm = None
        if plate_maps_dir and (Path(plate_maps_dir) / f"{plate.name}.csv").exists():
            pm = Path(plate_maps_dir) / f"{plate.name}.csv"
        elif default_plate_map:
            pm = Path(default_plate_map)
        if pm is None:
            log(f"[hold] {plate.name}: no plate map found")
            state[plate.name] = {"status": "needs_plate_map"}
            continue

        log(f"[ingest] {plate.name}")
        ing = ingest_plate(plate, dest_root, verify)
        log(f"         {ing['copied']} files copied, {ing['skipped_already_present']} already present")
        out = runs_root / plate.name
        log(f"[run] {plate.name} with plate map {pm.name}")
        code = run_plate(pm, image_folder_for(Path(ing["destination"])), out)
        state[plate.name] = {"status": "done" if code == 0 else "failed", "exit_code": code,
                             "plate_map": str(pm), "run_folder": str(out),
                             "finished_utc": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        log(f"[{state[plate.name]['status']}] {plate.name} -> {out}")
    runs_root.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return state


def watch(interval_seconds: float = 300, **kwargs) -> None:
    """Run scan_once forever (Ctrl+C to stop). Suitable for a lab PC or a scheduled task."""
    while True:
        scan_once(**kwargs)
        time.sleep(interval_seconds)
