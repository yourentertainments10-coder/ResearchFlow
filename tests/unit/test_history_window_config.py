"""DB_HISTORY_WINDOW_HOURS: owner-reported provider history window, in whole hours (never rounded to days)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from sie.config import Settings

ROOT = Path(__file__).resolve().parents[2]
DB = "postgresql://u:p@localhost/x"


def make(**kw):
    return Settings(_env_file=None, database_url=DB, **kw)


def test_unset_and_blank_both_mean_not_verified(monkeypatch):
    monkeypatch.delenv("DB_HISTORY_WINDOW_HOURS", raising=False)
    assert make().db_history_window_hours is None
    monkeypatch.setenv("DB_HISTORY_WINDOW_HOURS", "  ")
    assert make().db_history_window_hours is None


def test_six_hours_is_kept_as_six_not_rounded_to_a_day(monkeypatch):
    monkeypatch.setenv("DB_HISTORY_WINDOW_HOURS", "6")
    assert make().db_history_window_hours == 6


@pytest.mark.parametrize("bad", ["0.25", "six", "-1", "6h"])
def test_a_value_that_is_not_whole_non_negative_hours_is_refused(monkeypatch, bad):
    monkeypatch.setenv("DB_HISTORY_WINDOW_HOURS", bad)
    with pytest.raises(ValidationError):
        make()


def test_the_old_days_name_is_gone():
    assert "db_history_window_days" not in Settings.model_fields


def test_no_workflow_runs_a_purge_or_prune():
    """Purge is an owner command run by hand; the variable is therefore not mapped into any workflow."""
    for wf in (ROOT / ".github" / "workflows").glob("*.yml"):
        body = wf.read_text(encoding="utf-8")
        assert "project purge" not in body and "evidence prune" not in body, wf.name
