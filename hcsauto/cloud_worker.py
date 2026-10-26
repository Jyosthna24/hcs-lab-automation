"""Entry point inside the cloud container: pull the run from S3, analyse, push results back.

Environment (set by hcsauto.cloud when it submits the job):
    HCSAUTO_S3_URI   s3://bucket/prefix/run   - run folder in S3
    HCSAUTO_COMMAND  analysis command with {input} {config} {results_dir} placeholders
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .cloud import WORKER_DATA, list_keys


def _split(uri: str) -> tuple[str, str]:
    if not uri.startswith("s3://"):
        raise ValueError(f"HCSAUTO_S3_URI must start with s3:// (got {uri!r})")
    bucket, _, prefix = uri[5:].partition("/")
    return bucket, prefix.rstrip("/")


def run(env: dict | None = None, s3=None, workdir: Path | str = WORKER_DATA) -> int:
    env = dict(os.environ if env is None else env)
    bucket, root = _split(env["HCSAUTO_S3_URI"])
    if s3 is None:
        import boto3
        s3 = boto3.client("s3")
    work = Path(workdir)

    for key in list_keys(s3, bucket, root + "/"):
        rel = key[len(root) + 1:]
        if not rel or rel.startswith("results/"):
            continue
        dest = work / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        s3.download_file(bucket, key, str(dest))

    results = work / "results"
    results.mkdir(parents=True, exist_ok=True)
    # the input sheet was written for WORKER_DATA; point it at the real staging folder
    if str(work) != WORKER_DATA:
        import pandas as pd
        df = pd.read_excel(work / "input_file.xlsx")
        df["baseFolder"] = str(work / "images")
        df.to_excel(work / "input_file.xlsx", index=False)

    cmd = env["HCSAUTO_COMMAND"].format(input=f'"{work / "input_file.xlsx"}"',
                                        config=f'"{work / "config.csv"}"', results_dir=f'"{results}"')
    with open(results / "analysis.log", "w", encoding="utf-8") as log:
        code = subprocess.run(cmd, shell=True, stdout=log, stderr=subprocess.STDOUT).returncode

    for f in results.rglob("*"):
        if f.is_file():
            s3.upload_file(str(f), bucket, f"{root}/results/{f.relative_to(results).as_posix()}")
    return code


if __name__ == "__main__":
    sys.exit(run())
