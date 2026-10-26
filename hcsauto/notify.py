"""Tell people when a run finishes: email (SMTP) and/or Microsoft Teams (incoming webhook).

notify.yaml (secrets come from environment variables, never from the file):

    email:
      smtp_host: smtp.office365.com
      smtp_port: 587
      sender: lab-automation@example.ac.uk
      to: [me@example.ac.uk, pi@example.ac.uk]
      username_env: HCSAUTO_SMTP_USER
      password_env: HCSAUTO_SMTP_PASSWORD
      attach_deck: true
    teams:
      webhook_env: HCSAUTO_TEAMS_WEBHOOK
    only_on: [ok, flagged, excluded, rejected, failed]     # which run outcomes notify
"""
from __future__ import annotations

import json
import os
import smtplib
import urllib.request
from email.message import EmailMessage
from pathlib import Path

import yaml

ICON = {"ok": "✅", "flagged": "⚠️", "excluded": "⚠️", "rejected": "⛔", "failed": "❌"}


def load(path: Path | str) -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}


def compose(run_name: str, status: str, summary: list[str], outputs: dict) -> tuple[str, str]:
    subject = f"{ICON.get(status, '')} {run_name}: {status.upper()}".strip()
    files = [f"  {k}: {v}" for k, v in outputs.items() if v and k in ("excel", "prism", "slides", "summary")]
    body = "\n".join([subject, "", *[f"• {s}" for s in summary], "", "Outputs:", *files, "",
                      "— sent by hcs-lab-automation"])
    return subject, body


def send_email(cfg: dict, subject: str, body: str, attachment: Path | None = None) -> None:
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, cfg["sender"], ", ".join(cfg["to"])
    msg.set_content(body)
    if attachment and Path(attachment).exists():
        msg.add_attachment(Path(attachment).read_bytes(), maintype="application",
                           subtype="vnd.openxmlformats-officedocument.presentationml.presentation",
                           filename=Path(attachment).name)
    with smtplib.SMTP(cfg["smtp_host"], int(cfg.get("smtp_port", 587)), timeout=30) as s:
        s.starttls()
        user, pw = os.environ.get(cfg.get("username_env", "")), os.environ.get(cfg.get("password_env", ""))
        if user and pw:
            s.login(user, pw)
        s.send_message(msg)


def send_teams(cfg: dict, subject: str, summary: list[str]) -> None:
    url = os.environ.get(cfg.get("webhook_env", ""))
    if not url:
        raise RuntimeError(f"Teams webhook env var {cfg.get('webhook_env')} is not set")
    card = {"type": "message", "attachments": [{
        "contentType": "application/vnd.microsoft.card.adaptive",
        "content": {"type": "AdaptiveCard", "version": "1.4", "body": [
            {"type": "TextBlock", "text": subject, "weight": "Bolder", "size": "Medium", "wrap": True},
            *[{"type": "TextBlock", "text": f"• {s}", "wrap": True} for s in summary]]}}]}
    req = urllib.request.Request(url, data=json.dumps(card).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        if r.status >= 300:
            raise RuntimeError(f"Teams webhook returned HTTP {r.status}")


def notify(cfg: dict, run_name: str, status: str, summary: list[str], outputs: dict,
           log=print) -> list[str]:
    """Send every configured notification. Failures are logged, never crash the run."""
    if status not in cfg.get("only_on", list(ICON)):
        return []
    subject, body = compose(run_name, status, summary, outputs)
    sent = []
    for channel, fn in (("email", lambda c: send_email(c, subject, body,
                                                        outputs.get("slides") if c.get("attach_deck") else None)),
                        ("teams", lambda c: send_teams(c, subject, summary))):
        if channel in cfg:
            try:
                fn(cfg[channel])
                sent.append(channel)
            except Exception as e:                       # a mail outage must not lose the results
                log(f"[notify] {channel} failed: {e}")
    return sent
