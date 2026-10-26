"""Run the analysis on an HPC cluster (SGE, e.g. UCL Myriad): upload, submit, wait, download.

    upload only the images the input file needs -> rewrite paths for the cluster
    -> render job script from hpc.yaml -> qsub -> poll qstat -> download results

Hands-off by design: SSH key authentication only (optionally through a jump host
such as a university gateway), so it can run unattended from `watch`. No
usernames, passwords or paths live in the code; they are all in hpc.yaml.
"""
from __future__ import annotations

import posixpath
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
import yaml

from .detect import IX_RE, PHENIX_RE
from .plateplan.wells import convert

JOB_TEMPLATE = """#!/bin/bash -l
#$ -N {job_name}
#$ -l h_rt={walltime}
#$ -l mem={memory}
#$ -pe smp {cores}
#$ -wd {run_dir}
#$ -o {run_dir}/job.out
#$ -j y
set -e
{modules}
{setup}
{command}
"""


@dataclass
class HPCConfig:
    host: str
    remote_root: str
    command: str
    user: str | None = None
    jump_host: str | None = None
    key_file: str | None = None
    resources: dict = field(default_factory=lambda: {"walltime": "4:00:00", "memory": "8G", "cores": 8})
    modules: list[str] = field(default_factory=list)
    setup: list[str] = field(default_factory=list)
    submit_cmd: str = "qsub {script}"
    status_cmd: str = "qstat -j {job_id}"        # exit code 0 while the job exists
    job_id_regex: str = r"Your job(?:-array)? (\d+)"
    poll_seconds: float = 60
    timeout_hours: float = 24

    @classmethod
    def load(cls, path: Path | str) -> "HPCConfig":
        return cls(**yaml.safe_load(Path(path).read_text(encoding="utf-8")))


class SSHTransport:
    """paramiko SSH/SFTP, optionally tunnelled through a jump host. Key auth only."""

    def __init__(self, cfg: HPCConfig):
        import paramiko
        key = str(Path(cfg.key_file).expanduser()) if cfg.key_file else None
        kw = dict(username=cfg.user, key_filename=key, timeout=30, allow_agent=True, look_for_keys=True)
        self._jump = None
        sock = None
        if cfg.jump_host:
            self._jump = paramiko.SSHClient()
            self._jump.set_missing_host_key_policy(paramiko.RejectPolicy())
            self._jump.load_system_host_keys()
            self._jump.connect(cfg.jump_host, **kw)
            sock = self._jump.get_transport().open_channel("direct-tcpip", (cfg.host, 22), ("127.0.0.1", 0))
        self._ssh = paramiko.SSHClient()
        self._ssh.set_missing_host_key_policy(paramiko.RejectPolicy())   # never trust unknown hosts silently
        self._ssh.load_system_host_keys()
        self._ssh.connect(cfg.host, sock=sock, **kw)
        self._sftp = self._ssh.open_sftp()

    def run(self, cmd: str) -> tuple[int, str]:
        _, out, err = self._ssh.exec_command(cmd)
        code = out.channel.recv_exit_status()
        return code, out.read().decode() + err.read().decode()

    def mkdir(self, remote: str) -> None:
        self.run(f"mkdir -p '{remote}'")

    def put(self, local: Path, remote: str) -> None:
        self._sftp.put(str(local), remote)

    def get(self, remote: str, local: Path) -> None:
        self._sftp.get(remote, str(local))

    def listdir(self, remote: str) -> list[str]:
        return self._sftp.listdir(remote)

    def close(self) -> None:
        self._sftp.close()
        self._ssh.close()
        if self._jump:
            self._jump.close()


class LocalTransport:
    """Same interface on the local machine: for a local SGE install, or for tests."""

    def run(self, cmd: str) -> tuple[int, str]:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        return p.returncode, p.stdout + p.stderr

    def mkdir(self, remote: str) -> None:
        Path(remote).mkdir(parents=True, exist_ok=True)

    def put(self, local: Path, remote: str) -> None:
        Path(remote).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(local, remote)

    def get(self, remote: str, local: Path) -> None:
        shutil.copy2(remote, local)

    def listdir(self, remote: str) -> list[str]:
        return [p.name for p in Path(remote).iterdir()]

    def close(self) -> None:
        pass


def files_needed(input_file: Path, image_folder: Path, timepoints: range) -> list[Path]:
    """Only the images for wells in the input file and timepoints in the window."""
    df = pd.read_excel(input_file)
    wells = {convert(v, "imagexpress") for c in df.columns if c.startswith("condWells")
             for v in df[c].dropna()}
    tps = set(timepoints)
    keep = []
    for p in image_folder.rglob("*"):
        if not p.is_file():
            continue
        if m := PHENIX_RE.match(p.name):
            if convert(m.group(1), "imagexpress") in wells and int(m.group(5)) in tps:
                keep.append(p)
        elif m := IX_RE.search(p.name):
            tp = re.search(r"TimePoint_(\d+)", str(p.parent))
            if m.group(1).upper() in wells and (tp is None or int(tp.group(1)) in tps):
                keep.append(p)
        elif p.suffix.upper() in (".HTD", ".XML"):
            keep.append(p)                                     # acquisition metadata
    return sorted(keep)


def run_on_hpc(cfg: HPCConfig, run_name: str, input_file: Path, config_file: Path, image_folder: Path,
               plate_dir: Path, results_dir: Path, timepoints: range, transport=None,
               log=print) -> Path:
    """Upload, submit, wait and download. Returns the local results folder."""
    t = transport or SSHTransport(cfg)
    try:
        rroot = posixpath.join(cfg.remote_root, run_name)
        rimg_base = posixpath.join(rroot, "images")
        rplate = posixpath.join(rimg_base, plate_dir.name)
        rresults = posixpath.join(rroot, "results")
        t.mkdir(rplate)
        t.mkdir(rresults)

        files = files_needed(input_file, image_folder, timepoints)
        mb = sum(f.stat().st_size for f in files) / 1e6
        log(f"[hpc] uploading {len(files)} files ({mb:.0f} MB)")
        made = set()
        for f in files:
            rel = f.relative_to(plate_dir).as_posix()
            rdir = posixpath.dirname(posixpath.join(rplate, rel))
            if rdir not in made:
                t.mkdir(rdir)
                made.add(rdir)
            t.put(f, posixpath.join(rplate, rel))

        # the cluster sees the images under its own path
        df = pd.read_excel(input_file)
        df["baseFolder"] = rimg_base
        remote_input = Path(results_dir).parent / "input_file_hpc.xlsx"
        df.to_excel(remote_input, index=False)
        t.put(remote_input, posixpath.join(rroot, "input_file.xlsx"))
        t.put(config_file, posixpath.join(rroot, "config.csv"))

        command = cfg.command.format(input=posixpath.join(rroot, "input_file.xlsx"),
                                     config=posixpath.join(rroot, "config.csv"), results_dir=rresults)
        script = JOB_TEMPLATE.format(job_name=re.sub(r"\W", "_", run_name)[:30], run_dir=rroot,
                                     modules="\n".join(cfg.modules), setup="\n".join(cfg.setup),
                                     command=command, **cfg.resources)
        local_script = Path(results_dir).parent / "job.sh"
        local_script.write_text(script, encoding="utf-8", newline="\n")
        t.put(local_script, posixpath.join(rroot, "job.sh"))

        code, out = t.run(f"cd '{rroot}' && " + cfg.submit_cmd.format(script="job.sh"))
        m = re.search(cfg.job_id_regex, out)
        if code != 0 or not m:
            raise RuntimeError(f"job submission failed: {out.strip()}")
        job_id = m.group(1)
        log(f"[hpc] submitted job {job_id}")

        deadline = time.time() + cfg.timeout_hours * 3600
        while t.run(cfg.status_cmd.format(job_id=job_id))[0] == 0:
            if time.time() > deadline:
                raise TimeoutError(f"job {job_id} still running after {cfg.timeout_hours} h")
            time.sleep(cfg.poll_seconds)
        log(f"[hpc] job {job_id} finished; downloading results")

        results_dir = Path(results_dir)
        results_dir.mkdir(parents=True, exist_ok=True)
        names = [n for n in t.listdir(rresults) if n.lower().endswith((".xlsx", ".csv"))]
        if not names:
            raise RuntimeError(f"job {job_id} produced no results in {rresults}")
        for n in names:
            t.get(posixpath.join(rresults, n), results_dir / n)
        try:
            t.get(posixpath.join(rroot, "job.out"), results_dir.parent / "hpc_job.out")
        except (OSError, IOError):
            pass
        return results_dir
    finally:
        t.close()
