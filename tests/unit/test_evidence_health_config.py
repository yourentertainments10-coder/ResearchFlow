"""EVIDENCE_WEB_MAX_AGE_DAYS and EVIDENCE_OLD_UPLOAD_DAYS (ADR-043): positive whole days, default 30 / 180."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sie.config import Settings

DB = "postgresql://u:p@localhost/x"


def make():
    return Settings(_env_file=None, database_url=DB)


def test_defaults_are_30_and_180(monkeypatch):
    monkeypatch.delenv("EVIDENCE_WEB_MAX_AGE_DAYS", raising=False)
    monkeypatch.delenv("EVIDENCE_OLD_UPLOAD_DAYS", raising=False)
    s = make()
    assert (s.evidence_web_max_age_days, s.evidence_old_upload_days) == (30, 180)


def test_the_web_max_age_is_configurable(monkeypatch):
    monkeypatch.setenv("EVIDENCE_WEB_MAX_AGE_DAYS", "7")
    assert make().evidence_web_max_age_days == 7


@pytest.mark.parametrize("bad", ["0", "-5", "", "soon", "1.5"])
def test_a_value_that_could_mean_never_stale_is_refused(monkeypatch, bad):
    monkeypatch.setenv("EVIDENCE_WEB_MAX_AGE_DAYS", bad)
    with pytest.raises(ValidationError):
        make()
