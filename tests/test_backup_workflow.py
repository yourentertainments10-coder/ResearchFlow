"""The public repo must never publish a plaintext dump: static checks on backup.yml."""

from __future__ import annotations

from pathlib import Path

import yaml

WF = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "backup.yml"


def test_backup_workflow_encrypts_and_guards():
    text = WF.read_text()
    wf = yaml.safe_load(text)
    steps = wf["jobs"]["backup"]["steps"]
    runs = "\n".join(s.get("run", "") for s in steps)
    assert "--require-encryption" in runs
    assert "BACKUP_AGE_RECIPIENT" in wf["jobs"]["backup"]["env"]
    uploads = [s for s in steps if str(s.get("uses", "")).startswith("actions/upload-artifact")]
    assert len(uploads) == 1 and uploads[0]["with"]["name"] == "sie-backup-encrypted"
    names = [s.get("name", "") for s in steps]
    assert names.index(next(n for n in names if n.startswith("Guard"))) < steps.index(uploads[0])
    guard = next(s for s in steps if s.get("name", "").startswith("Guard"))
    assert "PGDMP" in guard["run"] and "AGE-SECRET-KEY-" in guard["run"]
    assert "secrets.BACKUP" not in text  # the private key never lives in GitHub
