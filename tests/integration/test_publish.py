"""6D: the export bundle is deterministic, safe to expose, and refuses to publish nothing."""

from __future__ import annotations

import csv
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text
from tests.integration.test_ops_foundation import csv_source
from typer.testing import CliRunner

from sie.cli import app
from sie.config import Settings
from sie.ops.publish import PublishError, publish, verify_bundle
from sie.pipeline.runner import run_ingest
from sie.reference import seed_reference

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 6, 10, 0, tzinfo=UTC)


@pytest.fixture()
def settings(db_url, tmp_path):
    shutil.copytree(ROOT / "data" / "reference", tmp_path / "reference")
    return Settings(
        _env_file=None,
        database_url=db_url.render_as_string(hide_password=False),
        data_dir=tmp_path,
        raw_store_backend="db",
    )


@pytest.fixture()
def loaded(engine, settings):
    with engine.begin() as c:
        seed_reference(c, settings.reference_dir)
    run_ingest(engine, csv_source(), settings, now=NOW)
    return engine


def test_bundle_contents_and_manifest(loaded, tmp_path):
    result = publish(loaded, "asiad-2026", tmp_path / "out", NOW)
    out = tmp_path / "out"
    assert sorted(p.name for p in out.iterdir()) == ["events.csv", "manifest.json", "medals.csv"]
    assert verify_bundle(out) == []
    with (out / "medals.csv").open(newline="") as f:
        rows = list(csv.DictReader(f))
    with loaded.connect() as c:
        expected = c.execute(text("SELECT count(*) FROM v_medal_facts")).scalar_one()
    assert len(rows) == expected == result.manifest["counts"]["medal_rows"] > 0
    assert result.manifest["data_as_of"] is not None
    assert result.manifest["competition"]["code"] == "asiad-2026"
    medals_in_event = [r["medal"] for r in rows if r["event"] == rows[0]["event"]]
    assert medals_in_event[0] == "Gold"  # Gold, Silver, Bronze order, not alphabetical


def test_same_data_gives_identical_bytes(loaded, tmp_path):
    a = publish(loaded, "asiad-2026", tmp_path / "a", NOW)
    b = publish(loaded, "asiad-2026", tmp_path / "b", NOW.replace(hour=23))
    for name in ("medals.csv", "events.csv"):
        assert (tmp_path / "a" / name).read_bytes() == (tmp_path / "b" / name).read_bytes()
    assert a.manifest["data_fingerprint"] == b.manifest["data_fingerprint"]
    assert a.manifest["exported_at"] != b.manifest["exported_at"]


def test_fingerprint_changes_when_data_changes(loaded, tmp_path):
    before = publish(loaded, "asiad-2026", tmp_path / "a", NOW).manifest["data_fingerprint"]
    with loaded.begin() as c:
        c.execute(
            text("UPDATE events SET is_disputed = true WHERE id = (SELECT min(id) FROM events)")
        )
    after = publish(loaded, "asiad-2026", tmp_path / "b", NOW).manifest
    assert after["data_fingerprint"] != before and after["counts"]["disputed_events"] >= 0


def test_nothing_internal_is_exported(loaded, tmp_path):
    publish(loaded, "asiad-2026", tmp_path / "out", NOW)
    blob = "".join(p.read_text() for p in (tmp_path / "out").iterdir()).lower()
    for forbidden in ("raw_version", "quarantine", "sha256:", "error_summary", "file://"):
        assert forbidden not in blob


def test_empty_or_unknown_competition_publishes_nothing(engine, tmp_path):
    with pytest.raises(PublishError, match="unknown competition"):
        publish(engine, "nope", tmp_path / "out", NOW)
    assert not (tmp_path / "out").exists()


def test_no_data_publishes_nothing_and_keeps_old_bundle(loaded, settings, tmp_path):
    out = tmp_path / "out"
    publish(loaded, "asiad-2026", out, NOW)
    before = {p.name: p.read_bytes() for p in out.iterdir()}
    with loaded.begin() as c:
        c.execute(text("UPDATE placings SET is_current = false, valid_to = valid_from"))
    with pytest.raises(PublishError, match="nothing to publish"):
        publish(loaded, "asiad-2026", out, NOW)
    assert {p.name: p.read_bytes() for p in out.iterdir()} == before


def test_tampered_bundle_is_detected(loaded, tmp_path):
    out = tmp_path / "out"
    publish(loaded, "asiad-2026", out, NOW)
    (out / "medals.csv").write_text("changed\n")
    assert verify_bundle(out) == ["medals.csv does not match its recorded hash"]
    (out / "events.csv").unlink()
    assert "events.csv is missing" in verify_bundle(out)


def test_cli_publish(loaded, settings, tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPETITION_ID", "asiad-2026")
    res = CliRunner().invoke(app, ["publish", "--out", str(tmp_path / "o")])
    assert res.exit_code == 0, res.output
    assert json.loads(res.stdout)["counts"]["medal_rows"] > 0
    assert verify_bundle(tmp_path / "o") == []
