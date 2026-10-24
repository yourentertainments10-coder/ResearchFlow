"""Project health on real PostgreSQL (ADR-043): states, claims, conflicts, isolation, read-only, CLI."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from typer.testing import CliRunner

from sie.cli import app
from sie.evidence import (
    ClaimInput,
    SourcePolicy,
    add_claim,
    create_project,
    detect_conflicts,
    gaps_from_health,
    ingest_upload,
    open_scope,
    project_health,
    prune_expired,
    register_fetched_source,
    review_claim,
)

NOW = datetime(2026, 10, 15, 10, 0, tzinfo=UTC)
MAX_AGE = timedelta(days=30)
OLD_UPLOAD = timedelta(days=180)
POLICIES = {
    "example.org": SourcePolicy(
        host="example.org", terms_ref="CC BY 4.0", recorded_on=datetime(2026, 10, 1).date(),
        allows_full=True, licence="CC BY 4.0", notes="",
    )
}  # fmt: skip


def claim(value="1", locator_kind="none", locator=None):
    return ClaimInput(
        subject="s", metric="m", value_text=value, excerpt=value,
        locator_kind=locator_kind, locator=locator or {},
        extraction_method="manual", extractor_version="t",
    )  # fmt: skip


def health(engine, slug="a", now=NOW):
    with engine.connect() as c:
        return project_health(c, open_scope(c, slug), now, MAX_AGE, OLD_UPLOAD)


def counts(engine):
    with engine.connect() as c:
        return {
            t: c.execute(text(f"SELECT count(*) FROM {t}")).scalar_one()
            for t in (
                "evidence_sources",
                "claims",
                "claim_reviews",
                "claim_conflicts",
                "raw_versions",
            )
        }


def web(engine, scope, url, content, when, **kw):
    kw.setdefault("title", "page")
    return register_fetched_source(
        engine, scope, content=content, requested_url=url, tier=2, now=when, **kw
    )


@pytest.fixture()
def world(engine):
    with engine.begin() as c:
        for slug in ("a", "b", "empty"):
            create_project(c, slug=slug, name=slug, now=NOW)
    with engine.connect() as c:
        return {"engine": engine, "a": open_scope(c, "a", for_write=True),
                "b": open_scope(c, "b", for_write=True)}  # fmt: skip


def test_empty_project_reports_no_sources(world):
    r = health(world["engine"], "empty")
    assert r.no_sources and r.overall == "no_sources" and not r.attention_needed
    assert r.claims_by_status == {} and r.open_conflict_ids == ()


def test_states_for_a_mixed_project(world):
    e, a = world["engine"], world["a"]
    up = ingest_upload(e, a, b"a,b\n1,2\n", "old.csv", now=NOW - timedelta(days=400))
    fresh = web(e, a, "https://example.org/fresh", b"f", NOW - timedelta(days=2))
    stale = web(e, a, "https://example.org/stale", b"s", NOW - timedelta(days=45))
    expired = web(
        e, a, "https://example.org/exp", b"x", NOW - timedelta(days=10),
        retention_mode="full", policies=POLICIES, retention_days=5,
    )  # fmt: skip
    pruned = web(
        e, a, "https://example.org/pr", b"p", NOW - timedelta(days=10),
        retention_mode="full", policies=POLICIES, retention_days=5,
    )  # fmt: skip
    prune_expired(e, now=NOW, execute=True, confirm="prune")
    r = health(e)
    states = {i.source_id: str(i.state) for i in r.sources}
    assert states[pruned.source_id] == "pruned" and states[expired.source_id] == "pruned"
    assert states[up.source_id] == "upload" and states[fresh.source_id] == "current"
    assert states[stale.source_id] == "stale"
    assert next(i for i in r.sources if i.source_id == up.source_id).old_upload is True
    assert r.overall == "attention" and r.attention_needed


def test_expired_but_not_yet_pruned_is_expired_not_pruned(world):
    e, a = world["engine"], world["a"]
    s = web(
        e, a, "https://example.org/exp", b"x", NOW - timedelta(days=10),
        retention_mode="full", policies=POLICIES, retention_days=5,
    )  # fmt: skip
    r = health(e)
    item = next(i for i in r.sources if i.source_id == s.source_id)
    assert str(item.state) == "expired" and item.pruned_at is None


def test_old_upload_alone_is_ok(world):
    e, a = world["engine"], world["a"]
    ingest_upload(e, a, b"x\n1\n", "old.csv", now=NOW - timedelta(days=900))
    r = health(e)
    assert r.overall == "ok" and not r.attention_needed and r.sources[0].old_upload


def test_pruned_claims_conflicts_and_xlsx_are_counted(world):
    e, a = world["engine"], world["a"]
    s = web(
        e, a, "https://example.org/pr", b"p", NOW - timedelta(days=10),
        retention_mode="full", policies=POLICIES, retention_days=5,
    )  # fmt: skip
    up = ingest_upload(e, a, b"1\n", "u.csv", now=NOW)
    with e.begin() as c:
        add_claim(c, a, s.source_id, claim("1"), now=NOW)
        c1 = add_claim(c, a, up.source_id, claim("1"), now=NOW).claim_id
        add_claim(c, a, up.source_id, claim("2"), now=NOW)
        add_claim(
            c, a, up.source_id, claim("3", "xlsx_cell", {"sheet": "S", "cell": "A1"}), now=NOW
        )
        assert len(detect_conflicts(c, a, now=NOW)) >= 1
        review_claim(c, a, c1, status="disputed", reason="r", actor="Asha", now=NOW)
    prune_expired(e, now=NOW, execute=True, confirm="prune")
    r = health(e)
    assert r.claims_on_pruned_sources == 1 and r.xlsx_claims_pending == 1
    assert r.claims_by_status["disputed"] == 1 and sum(r.claims_by_status.values()) == 4
    assert r.open_conflict_ids and r.attention_needed
    ids = {g.id for g in gaps_from_health(r)}
    assert {"project_pruned_sources", "project_open_conflicts", "project_unverified_claims"} <= ids


def test_health_is_read_only_and_repeatable(world):
    e, a = world["engine"], world["a"]
    up = ingest_upload(e, a, b"1\n", "u.csv", now=NOW)
    with e.begin() as c:
        add_claim(c, a, up.source_id, claim("1"), now=NOW)
    before = counts(e)
    assert health(e).to_dict() == health(e).to_dict()
    assert counts(e) == before


def test_other_projects_never_appear(world):
    e, a, b = world["engine"], world["a"], world["b"]
    ingest_upload(e, a, b"a-data\n", "a.csv", now=NOW)
    sb = web(e, b, "https://example.org/b", b"b", NOW - timedelta(days=90))
    with e.begin() as c:
        add_claim(c, b, sb.source_id, claim("9"), now=NOW)
    r = health(e, "a")
    assert [i.kind for i in r.sources] == ["upload"] and r.claims_by_status == {}
    assert health(e, "b").overall == "attention"


def test_cli_health_exit_codes_and_json(world, monkeypatch, db_url):
    e, a = world["engine"], world["a"]
    monkeypatch.setenv("DATABASE_URL", db_url.render_as_string(hide_password=False))
    runner = CliRunner()
    empty = runner.invoke(app, ["project", "health", "empty"])
    assert empty.exit_code == 0 and "no sources" in empty.output
    ingest_upload(e, a, b"1\n", "u.csv", now=datetime.now(UTC) - timedelta(days=900))
    old_only = runner.invoke(app, ["project", "health", "a", "--json"])
    assert old_only.exit_code == 0
    assert json.loads(old_only.output)["sources"][0]["old_upload"] is True
    web(e, a, "https://example.org/s", b"s", datetime.now(UTC) - timedelta(days=60))
    assert runner.invoke(app, ["project", "health", "a"]).exit_code == 1
    assert runner.invoke(app, ["project", "health", "nope"]).exit_code == 2
