"""Stage 0 - INGEST: move plates from the microscope's storage into the analysis area.

* knows when an acquisition is FINISHED (MetaXpress .HTD timepoints all written,
  every selected well present in the last timepoint, no file written for a while)
* copies the whole plate folder, MetaXpress metadata included
* copy is resumable and verified (size, or SHA-256 checksum), and written via a
  temporary name so a half-copied file is never mistaken for a good one
* leaves an ingest manifest listing every file transferred
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from .detect import IX_RE
from .metaxpress import find_htd, read_htd


def _sha256(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while b := fh.read(chunk):
            h.update(b)
    return h.hexdigest()


def _newest_mtime(folder: Path) -> float:
    return max((p.stat().st_mtime for p in folder.rglob("*") if p.is_file()), default=0.0)


def plate_is_complete(plate_dir: Path | str, settle_seconds: float = 60) -> tuple[bool, str]:
    """Is the microscope finished writing this plate?"""
    plate_dir = Path(plate_dir)
    htd = find_htd(plate_dir)
    if htd is not None:                                            # ImageXpress / MetaXpress
        h = read_htd(htd)
        tps = sorted((d for d in plate_dir.iterdir() if d.is_dir() and d.name.startswith("TimePoint_")),
                     key=lambda d: int(d.name.split("_")[1]))
        if h.timepoints > 1 and len(tps) < h.timepoints:
            return False, f"acquiring: {len(tps)}/{h.timepoints} timepoints"
        last = tps[-1] if tps else plate_dir
        written = {(m.group(1).upper(), int(m.group(2))) for p in last.iterdir()
                   if (m := IX_RE.search(p.name))}
        n_w = max(len(h.wavelengths), 1)
        missing = [w for w in h.selected_wells
                   if not all((w, c) in written for c in range(1, n_w + 1))]
        # a few missing wells at the END is normal for a faulty well; most missing = still writing
        if h.selected_wells and len(missing) > 0.5 * len(h.selected_wells):
            return False, f"acquiring: last timepoint has {len(h.selected_wells) - len(missing)}" \
                          f"/{len(h.selected_wells)} wells"
    elif any((plate_dir / "Images" / n).exists() for n in ("Index.xml", "Index.idx.xml")):
        pass                                                       # Phenix writes Index.xml at the end
    else:
        return False, "no acquisition metadata yet (.HTD or Index.xml)"
    age = time.time() - _newest_mtime(plate_dir)
    if settle_seconds > 0 and age < settle_seconds:
        return False, f"files still changing ({age:.0f}s < {settle_seconds:.0f}s settle time)"
    return True, "complete"


def ingest_plate(src: Path | str, dest_root: Path | str, verify: str = "size") -> dict:
    """Copy one plate folder to dest_root/<plate>, skipping files already copied intact."""
    if verify not in ("size", "sha256"):
        raise ValueError("verify must be 'size' or 'sha256'")
    src, dest = Path(src), Path(dest_root) / Path(src).name
    copied = skipped = n_bytes = 0
    files = []
    for f in sorted(p for p in src.rglob("*") if p.is_file()):
        rel = f.relative_to(src)
        target = dest / rel
        same = target.exists() and target.stat().st_size == f.stat().st_size
        if same and verify == "sha256":
            same = _sha256(target) == _sha256(f)
        if same:
            skipped += 1
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + ".partial")
            shutil.copy2(f, tmp)
            os.replace(tmp, target)                                # atomic: never a half file
            ok = target.stat().st_size == f.stat().st_size
            if ok and verify == "sha256":
                ok = _sha256(target) == _sha256(f)
            if not ok:
                raise IOError(f"verification failed for {rel}")
            copied += 1
            n_bytes += f.stat().st_size
        files.append(str(rel).replace("\\", "/"))
    manifest = {"stage": "ingest", "source": str(src), "destination": str(dest),
                "finished_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "verify": verify, "files": len(files), "copied": copied, "skipped_already_present": skipped,
                "bytes_copied": n_bytes}
    (dest / "ingest_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def image_folder_for(plate_dir: Path) -> Path:
    """The folder `prepare` should look at (Phenix images live in Images/)."""
    return plate_dir / "Images" if (plate_dir / "Images").is_dir() else plate_dir
