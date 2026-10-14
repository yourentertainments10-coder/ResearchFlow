"""The project/evidence CLI: owner-only, dry-run by default, explicit confirmation for destruction."""

from __future__ import annotations

from sqlalchemy import text
from typer.testing import CliRunner

from sie.cli import app

runner = CliRunner()


def env(monkeypatch, db_url):
    monkeypatch.setenv("DATABASE_URL", db_url.render_as_string(hide_password=False))


def n(engine, sql):
    with engine.connect() as c:
        return c.execute(text(sql)).scalar_one()


def test_create_list_upload_check_and_dry_run_purge(engine, db_url, monkeypatch, tmp_path):
    env(monkeypatch, db_url)
    assert runner.invoke(app, ["project", "create", "p1", "--name", "Pilot"]).exit_code == 0
    assert runner.invoke(app, ["project", "create", "Bad Slug", "--name", "x"]).exit_code == 2
    listed = runner.invoke(app, ["project", "list"])
    assert "p1\topen\tPilot" in listed.stdout

    f = tmp_path / "m.csv"
    f.write_bytes(b"a,b\n1,2\n")
    up = runner.invoke(app, ["evidence", "upload", "p1", str(f), "--by", "Asha"])
    assert up.exit_code == 0 and "new_version" in up.stdout
    assert "unchanged" in runner.invoke(app, ["evidence", "upload", "p1", str(f)]).stdout
    chk = runner.invoke(app, ["evidence", "check", "p1"])
    assert chk.exit_code == 0 and "files checked 1" in chk.stdout

    dry = runner.invoke(app, ["project", "purge", "p1"])
    assert dry.exit_code == 0 and "Dry run" in dry.stdout
    assert n(engine, "SELECT count(*) FROM evidence_sources") == 1

    refused = runner.invoke(
        app, ["project", "purge", "p1", "--execute", "--confirm", "nope", "--by", "Asha"]
    )
    assert refused.exit_code == 1 and n(engine, "SELECT count(*) FROM evidence_sources") == 1

    done = runner.invoke(
        app, ["project", "purge", "p1", "--execute", "--confirm", "p1", "--by", "Asha Rao"]
    )
    assert done.exit_code == 0 and "NOT erased from backups" in done.stdout
    assert n(engine, "SELECT count(*) FROM evidence_sources") == 0
    assert "purged" in runner.invoke(app, ["project", "list"]).stdout


def test_prune_defaults_to_a_dry_run_and_needs_the_token(engine, db_url, monkeypatch):
    env(monkeypatch, db_url)
    dry = runner.invoke(app, ["evidence", "prune"])
    assert dry.exit_code == 0 and "Dry run" in dry.stdout
    assert runner.invoke(app, ["evidence", "prune", "--execute"]).exit_code == 1
    assert (
        runner.invoke(app, ["evidence", "prune", "--execute", "--confirm", "prune"]).exit_code == 0
    )


def test_an_unknown_project_is_a_clean_error(engine, db_url, monkeypatch):
    env(monkeypatch, db_url)
    r = runner.invoke(app, ["evidence", "check", "missing"])
    assert r.exit_code == 2
