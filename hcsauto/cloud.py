"""Run the analysis in the cloud: AWS S3 for data, AWS Batch for compute.

    upload only the images the run needs to S3 -> submit a Batch job running the
    analysis container -> poll until it SUCCEEDS or FAILS -> download results from S3

The container runs `python -m hcsauto.cloud_worker` (see Dockerfile), which pulls
the run from S3, executes the analysis command and pushes results back. Batch
scales compute to zero between runs, and GPU instances can be requested for
deep-learning segmentation.

Credentials come from the standard AWS chain (environment, ~/.aws, SSO or an
instance role); nothing secret lives in the config file.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import yaml

from .hpc import files_needed

WORKER_DATA = "/data"           # where the worker container stages the run


@dataclass
class CloudConfig:
    bucket: str
    job_queue: str
    job_definition: str
    command: str                               # placeholders {input} {config} {results_dir}
    region: str = "eu-west-2"
    prefix: str = "hcsauto"
    vcpus: int = 8
    memory_mib: int = 16384
    gpus: int = 0
    poll_seconds: float = 30
    timeout_hours: float = 24
    provider: str = "aws"

    @classmethod
    def load(cls, path: Path | str) -> "CloudConfig":
        cfg = cls(**yaml.safe_load(Path(path).read_text(encoding="utf-8")))
        if cfg.provider != "aws":
            raise ValueError(f"unsupported cloud provider '{cfg.provider}' (supported: aws)")
        return cfg


def _clients(cfg: CloudConfig):
    import boto3
    session = boto3.session.Session(region_name=cfg.region)
    return session.client("s3"), session.client("batch")


def list_keys(s3, bucket: str, prefix: str) -> list[str]:
    keys, token = [], None
    while True:
        kw = {"Bucket": bucket, "Prefix": prefix, **({"ContinuationToken": token} if token else {})}
        resp = s3.list_objects_v2(**kw)
        keys += [o["Key"] for o in resp.get("Contents", [])]
        if not resp.get("IsTruncated"):
            return keys
        token = resp["NextContinuationToken"]


def run_on_cloud(cfg: CloudConfig, run_name: str, input_file: Path, config_file: Path,
                 image_folder: Path, plate_dir: Path, results_dir: Path, timepoints: range,
                 s3=None, batch=None, log=print) -> Path:
    """Upload, submit, wait and download. Returns the local results folder."""
    if s3 is None or batch is None:
        s3, batch = _clients(cfg)
    root = f"{cfg.prefix.strip('/')}/{run_name}"

    files = files_needed(input_file, image_folder, timepoints)
    mb = sum(f.stat().st_size for f in files) / 1e6
    log(f"[cloud] uploading {len(files)} files ({mb:.0f} MB) to s3://{cfg.bucket}/{root}/")
    for f in files:
        s3.upload_file(str(f), cfg.bucket, f"{root}/images/{plate_dir.name}/{f.relative_to(plate_dir).as_posix()}")

    # inside the container the images sit under /data/images
    df = pd.read_excel(input_file)
    df["baseFolder"] = f"{WORKER_DATA}/images"
    staged = Path(results_dir).parent / "input_file_cloud.xlsx"
    df.to_excel(staged, index=False)
    s3.upload_file(str(staged), cfg.bucket, f"{root}/input_file.xlsx")
    s3.upload_file(str(config_file), cfg.bucket, f"{root}/config.csv")

    resources = [{"type": "VCPU", "value": str(cfg.vcpus)}, {"type": "MEMORY", "value": str(cfg.memory_mib)}]
    if cfg.gpus:
        resources.append({"type": "GPU", "value": str(cfg.gpus)})
    job = batch.submit_job(
        jobName=("hcsauto-" + "".join(c if c.isalnum() or c in "-_" else "-" for c in run_name))[:128],
        jobQueue=cfg.job_queue, jobDefinition=cfg.job_definition,
        containerOverrides={
            "command": ["python", "-m", "hcsauto.cloud_worker"],
            "environment": [{"name": "HCSAUTO_S3_URI", "value": f"s3://{cfg.bucket}/{root}"},
                            {"name": "HCSAUTO_COMMAND", "value": cfg.command}],
            "resourceRequirements": resources})
    job_id = job["jobId"]
    log(f"[cloud] submitted AWS Batch job {job_id}")

    deadline = time.time() + cfg.timeout_hours * 3600
    while True:
        desc = batch.describe_jobs(jobs=[job_id])["jobs"][0]
        if desc["status"] == "SUCCEEDED":
            break
        if desc["status"] == "FAILED":
            raise RuntimeError(f"Batch job {job_id} failed: {desc.get('statusReason', 'no reason given')}")
        if time.time() > deadline:
            batch.terminate_job(jobId=job_id, reason="hcs-lab-automation timeout")
            raise TimeoutError(f"Batch job {job_id} still {desc['status']} after {cfg.timeout_hours} h")
        time.sleep(cfg.poll_seconds)
    log(f"[cloud] job {job_id} succeeded; downloading results")

    results_dir = Path(results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)
    got = 0
    for key in list_keys(s3, cfg.bucket, f"{root}/results/"):
        name = key.rsplit("/", 1)[-1]
        if name.lower().endswith((".xlsx", ".csv", ".log")):
            s3.download_file(cfg.bucket, key, str(results_dir / name))
            got += name.lower().endswith((".xlsx", ".csv"))
    if not got:
        raise RuntimeError(f"Batch job {job_id} produced no results in s3://{cfg.bucket}/{root}/results/")
    return results_dir
