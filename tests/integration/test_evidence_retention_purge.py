"""Retention policy, registering fetched sources, owner-only purge and prune, and the integrity check."""

from __future__ import annotations

import shutil
import threading
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from sie.config import Settings
from sie.db.roles import PIPELINE, apply_roles
from sie.evidence import (
    BytesNotRetained,
    ClaimInput,
    EvidenceError,
    NotFound,
    OwnerOnly,
    PolicyRefused,
    ProjectClosed,
    PurgeRefused,
    SourcePolicy,
    UploadRejected,
    add_claim,
    check_project,
    create_project,
    get_source,
    ingest_upload,
    list_sources,
    load_policies,
    normalise_url,
    open_scope,
    policy_for,
    prune_expired,
    purge_project,
    read_bytes,
    register_fetched_source,
    verify_claim,
)
from sie.sources.manual import parser as manual

NOW = datetime(2026, 10, 12, 10, 0, tzinfo=UTC)
PAGE = b"<html>Medal table: India 6 gold</html>"
URL = "https://stats.example.org/medals?year=2026"
POLICY = SourcePolicy(
    host="example.org",
    terms_ref="CC BY 4.0 notice on /about",
    recorded_on=datetime(2026, 10, 1).date(),
    allows_full=True,
    licence="CC BY 4.0",
    notes="",
)
POLICIES = {"example.org": POLICY}


def count(engine, sql, **params):
    with engine.connect() as c:
        return c.execute(text(sql), params).scalar_one()


@pytest.fixture()
def world(engine):
    with engine.begin() as c:
        for slug in ("a", "b"):
            create_project(c, slug=slug, name=slug.upper(), now=NOW)
    with engine.connect() as c:
        return {
            "engine": engine,
            "a": open_scope(c, "a", for_write=True),
            "b": open_scope(c, "b", for_write=True),
        }


def register(world, project="a", content=PAGE, url=URL, **kw):
    kw.setdefault("title", "Medal table")
    kw.setdefault("tier", 2)
    kw.setdefault("now", NOW)
    return register_fetched_source(
        world["engine"], world[project], content=content, requested_url=url, **kw
    )


# ---- policy file -------------------------------------------------------------------------------

HEADER = "host,terms_ref,recorded_on,allows_full,licence,notes\n"


def test_the_committed_policy_file_lists_no_host_so_nothing_may_be_kept_in_full():
    root = Path(__file__).resolve().parents[2]
    assert load_policies(root / "data" / "evidence" / "source_policies.csv") == {}


def test_a_missing_policy_file_means_no_host_is_allowed(tmp_path):
    assert load_policies(tmp_path / "nope.csv") == {}


def test_a_valid_policy_file_loads_and_matches_parent_domains_but_not_tlds(tmp_path):
    f = tmp_path / "p.csv"
    f.write_text(
        HEADER + "example.org,CC BY 4.0 on /about,2026-10-01,true,CC BY 4.0,checked by owner\n"
    )
    policies = load_policies(f)
    assert policies["example.org"].allows_full and policies["example.org"].recorded_on.year == 2026
    assert policy_for(policies, "https://stats.example.org/x").host == "example.org"
    assert policy_for(policies, "https://example.org/x").host == "example.org"
    assert policy_for(policies, "https://other.org/x") is None
    assert policy_for({"org": POLICY}, "https://a.org/") is None  # a bare TLD never matches


@pytest.mark.parametrize(
    "body",
    [
        "host,terms\nexample.org,x\n",  # wrong columns
        HEADER + ",t,2026-10-01,true,,\n",  # no host
        HEADER + "example.org,t,2026-10-01,maybe,,\n",  # not true/false
        HEADER + "example.org,,2026-10-01,true,,\n",  # allows full with no recorded terms
        HEADER + "example.org,t,01/10/2026,true,,\n",  # bad date
        HEADER
        + "example.org,t,2026-10-01,true,,\nexample.org,t,2026-10-01,false,,\n",  # duplicate host
    ],
)
def test_a_bad_policy_file_is_refused_loudly(tmp_path, body):
    f = tmp_path / "p.csv"
    f.write_text(body)
    with pytest.raises(EvidenceError):
        load_policies(f)


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        (
            "HTTPS://Stats.Example.ORG/Medals?year=2026#top",
            "https://stats.example.org/Medals?year=2026",
        ),
        ("http://example.org", "http://example.org/"),
        ("https://example.org:8443/x", "https://example.org:8443/x"),
        (
            "https://example.org/x?utm_source=a",
            "https://example.org/x?utm_source=a",
        ),  # query kept as is
    ],
)
def test_urls_are_normalised_without_touching_the_query(raw, clean):
    assert normalise_url(raw) == clean


@pytest.mark.parametrize(
    "raw",
    [
        "ftp://example.org/x",
        "file:///etc/passwd",
        "https://u:p@example.org/x",
        "example.org",
        "https://",
        "x" * 2001,
    ],
)
def test_unsafe_or_malformed_source_urls_are_refused(raw):
    with pytest.raises(EvidenceError):
        normalise_url(raw)


# ---- registering fetched sources ----------------------------------------------------------------


def test_the_default_keeps_an_excerpt_source_and_stores_no_bytes(world):
    r = register(
        world,
        http_status=200,
        content_type="text/html",
        final_url="https://stats.example.org/final",
    )
    assert (r.outcome, r.created_source, r.document_id, r.raw_version_id) == (
        "new_version",
        True,
        None,
        None,
    )
    for table in ("raw_documents", "raw_versions", "raw_blobs", "raw_fetches", "project_documents"):
        assert count(world["engine"], f"SELECT count(*) FROM {table}") == 0
    with world["engine"].connect() as c:
        s = get_source(c, world["a"], r.source_id)
        assert (s.kind, s.retention_mode, s.size_bytes, s.http_status, s.tier) == (
            "web",
            "excerpt",
            len(PAGE),
            200,
            2,
        )
        assert (
            s.final_url == "https://stats.example.org/final"
            and s.retain_until is None
            and s.terms_ref is None
        )
        with pytest.raises(BytesNotRetained):
            read_bytes(c, world["a"], r.source_id)


def test_registering_the_same_content_twice_is_idempotent_and_new_content_is_new(world):
    first, again = register(world), register(world)
    assert (
        again.source_id == first.source_id
        and not again.created_source
        and again.outcome == "unchanged"
    )
    other = register(world, content=PAGE + b"!")
    assert other.source_id != first.source_id
    assert count(world["engine"], "SELECT count(*) FROM evidence_sources") == 2


def test_full_retention_is_refused_without_a_recorded_policy(world):
    with pytest.raises(PolicyRefused):
        register(world, retention_mode="full", policies={})
    with pytest.raises(PolicyRefused):
        register(
            world,
            retention_mode="full",
            policies={
                "example.org": SourcePolicy("example.org", "t", POLICY.recorded_on, False, "", "")
            },
        )
    with pytest.raises(PolicyRefused):
        register(world, url="https://elsewhere.net/x", retention_mode="full", policies=POLICIES)
    assert count(world["engine"], "SELECT count(*) FROM evidence_sources") == 0
    assert count(world["engine"], "SELECT count(*) FROM raw_documents") == 0


def test_full_retention_with_a_policy_stores_a_namespaced_copy_for_90_days(world):
    r = register(world, retention_mode="full", policies=POLICIES)
    pid = world["a"].project_id
    with world["engine"].connect() as c:
        doc = c.execute(text("SELECT source, url FROM raw_documents")).one()
        s = get_source(c, world["a"], r.source_id)
        assert read_bytes(c, world["a"], r.source_id) == PAGE
    assert tuple(doc) == ("web", f"rf-project/{pid}/web/{normalise_url(URL)}")
    assert s.retention_mode == "full" and s.retain_until == NOW + timedelta(days=90)
    assert "CC BY 4.0 notice" in s.terms_ref and "recorded 2026-10-01" in s.terms_ref


def test_the_retention_period_is_configurable_by_setting_and_by_argument(world, monkeypatch):
    monkeypatch.setenv("EVIDENCE_FULL_RETENTION_DAYS", "30")
    a = register(world, retention_mode="full", policies=POLICIES)
    b = register(
        world, content=PAGE + b"2", retention_mode="full", policies=POLICIES, retention_days=7
    )
    with world["engine"].connect() as c:
        assert get_source(c, world["a"], a.source_id).retain_until == NOW + timedelta(days=30)
        assert get_source(c, world["a"], b.source_id).retain_until == NOW + timedelta(days=7)
    with pytest.raises(EvidenceError):
        register(world, content=b"x", retention_mode="full", policies=POLICIES, retention_days=0)


def test_new_content_at_the_same_url_is_version_two_of_the_same_document(world):
    one = register(world, retention_mode="full", policies=POLICIES)
    two = register(world, content=PAGE + b"v2", retention_mode="full", policies=POLICIES)
    again = register(world, content=PAGE + b"v2", retention_mode="full", policies=POLICIES)
    assert one.document_id == two.document_id and two.source_id != one.source_id
    assert again.source_id == two.source_id and again.outcome == "unchanged"
    assert count(world["engine"], "SELECT max(version_no) FROM raw_versions") == 2


def test_two_projects_registering_the_same_page_hold_independent_copies(world):
    a = register(world, "a", retention_mode="full", policies=POLICIES)
    b = register(world, "b", retention_mode="full", policies=POLICIES)
    assert a.document_id != b.document_id and a.raw_version_id != b.raw_version_id
    assert (a.outcome, b.outcome) == ("new_version", "new_version")
    with world["engine"].connect() as c, pytest.raises(NotFound):
        get_source(c, world["a"], b.source_id)


@pytest.mark.parametrize(
    ("over", "exc"),
    [
        ({"content": b""}, UploadRejected),
        ({"content": "text"}, UploadRejected),
        ({"tier": 5}, EvidenceError),
        ({"tier": 0}, EvidenceError),
        ({"title": "  "}, EvidenceError),
        ({"retention_mode": "forever"}, EvidenceError),
        ({"max_bytes": 3}, UploadRejected),
    ],
)
def test_invalid_registrations_write_nothing(world, over, exc):
    with pytest.raises(exc):
        register(world, **over)
    assert count(world["engine"], "SELECT count(*) FROM evidence_sources") == 0


def test_concurrent_registrations_of_one_full_url_number_versions_without_gaps(world):
    barrier = threading.Barrier(6)
    results, errors = [], []

    def go(i):
        barrier.wait()
        try:
            results.append(
                register(world, content=f"v{i}".encode(), retention_mode="full", policies=POLICIES)
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=go, args=(i,)) for i in range(6)]
    [t.start() for t in threads]
    [t.join(timeout=60) for t in threads]
    assert not errors, errors
    assert sorted(
        r[0] for r in world["engine"].connect().execute(text("SELECT version_no FROM raw_versions"))
    ) == [1, 2, 3, 4, 5, 6]
    assert len({r.source_id for r in results}) == 6


# ---- roles --------------------------------------------------------------------------------------


@pytest.fixture()
def pipeline_engine(engine):
    with engine.begin() as c:
        apply_roles(c, reader_password="reader pass 1", pipeline_password="pipeline pass 2")
    eng = create_engine(engine.url.set(username=PIPELINE, password="pipeline pass 2"))
    yield eng
    eng.dispose()


def test_purge_and_prune_refuse_the_pipeline_role_even_as_a_dry_run(world, pipeline_engine):
    with pytest.raises(OwnerOnly):
        purge_project(pipeline_engine, "a", now=NOW)
    with pytest.raises(OwnerOnly):
        purge_project(pipeline_engine, "a", now=NOW, execute=True, confirm="a", purged_by="Asha")
    with pytest.raises(OwnerOnly):
        prune_expired(pipeline_engine, now=NOW)
    assert count(world["engine"], "SELECT count(*) FROM projects WHERE purged_at IS NOT NULL") == 0


def test_the_pipeline_role_can_still_ingest_evidence(world, pipeline_engine):
    r = ingest_upload(pipeline_engine, world["a"], b"through the pipeline role", "f.csv", now=NOW)
    assert r.outcome == "new_version"


# ---- purge --------------------------------------------------------------------------------------


def populate(world, project="a", suffix=b""):
    scope = world[project]
    up = ingest_upload(
        world["engine"], scope, b"country,gold\nIndia,6\n" + suffix, "m.csv", now=NOW
    )
    full = register(world, project, retention_mode="full", policies=POLICIES, content=PAGE + suffix)
    excerpt = register(
        world, project, content=PAGE + b"x" + suffix, url="https://stats.example.org/other"
    )
    with world["engine"].begin() as c:
        cid = add_claim(
            c, scope, up.source_id,
            ClaimInput(subject="India", metric="gold", value_text="6", excerpt="6", locator_kind="table_cell",
                       locator={"row_id": 1, "column": "gold", "delimiter": ","}, extraction_method="manual",
                       extractor_version="t", value_numeric=Decimal("6")),
            now=NOW,
        ).claim_id  # fmt: skip
        verify_claim(c, scope, cid, now=NOW)
    return {"up": up, "full": full, "excerpt": excerpt, "claim": cid}


TABLES = (
    "raw_documents", "raw_versions", "raw_blobs", "raw_fetches", "project_documents", "evidence_sources",
    "claims", "claim_reviews", "claim_conflicts",
)  # fmt: skip


def table_counts(engine):
    return {t: count(engine, f"SELECT count(*) FROM {t}") for t in TABLES}


def test_a_dry_run_is_the_default_and_changes_nothing(world):
    populate(world)
    before = table_counts(world["engine"])
    report = purge_project(world["engine"], "a", now=NOW)
    assert not report.executed and table_counts(world["engine"]) == before
    assert report.counts["claims"] == 1 and report.counts["raw_documents"] == 2
    assert report.lines[-1] == "Dry run: nothing was changed."
    assert count(world["engine"], "SELECT count(*) FROM projects WHERE purged_at IS NOT NULL") == 0


@pytest.mark.parametrize(
    "kw",
    [
        {"execute": True},  # no confirmation
        {"execute": True, "confirm": "b", "purged_by": "Asha"},  # wrong slug
        {"execute": True, "confirm": "a"},  # no named person
        {"execute": True, "confirm": "a", "purged_by": "system:cron"},
    ],
)
def test_a_destructive_purge_needs_the_slug_and_a_named_person(world, kw):
    populate(world)
    before = table_counts(world["engine"])
    with pytest.raises(PurgeRefused):
        purge_project(world["engine"], "a", now=NOW, **kw)
    assert table_counts(world["engine"]) == before


def test_purge_deletes_exactly_one_projects_data_and_leaves_the_other_intact(world):
    populate(world, "a")
    keep = populate(world, "b", b"-b")
    b_before = {k: count(world["engine"], f"SELECT count(*) FROM {k} WHERE project_id = :p", p=world["b"].project_id)
                for k in ("evidence_sources", "claims", "claim_reviews", "project_documents")}  # fmt: skip
    report = purge_project(
        world["engine"], "a", now=NOW, execute=True, confirm="a", purged_by="Asha Rao"
    )
    assert report.executed and report.tombstone_id
    pid = world["a"].project_id
    for t in (
        "evidence_sources",
        "claims",
        "claim_reviews",
        "claim_conflicts",
        "project_documents",
    ):
        assert count(world["engine"], f"SELECT count(*) FROM {t} WHERE project_id = :p", p=pid) == 0
    assert (
        count(
            world["engine"],
            "SELECT count(*) FROM raw_documents WHERE url LIKE :u",
            u=f"rf-project/{pid}/%",
        )
        == 0
    )
    assert (
        count(world["engine"], "SELECT count(*) FROM raw_blobs") == 2
    )  # only B's two full copies remain
    assert {k: count(world["engine"], f"SELECT count(*) FROM {k} WHERE project_id = :p", p=world["b"].project_id)
            for k in b_before} == b_before  # fmt: skip
    with world["engine"].connect() as c:  # B still reads back intact bytes
        assert read_bytes(c, world["b"], keep["up"].source_id).startswith(b"country,gold")
        assert check_project(c, world["b"]).ok


def test_purge_leaves_a_tombstone_and_closes_the_project(world):
    populate(world)
    report = purge_project(
        world["engine"], "a", now=NOW, execute=True, confirm="a", purged_by="Asha Rao"
    )
    with world["engine"].connect() as c:
        t = c.execute(text("SELECT * FROM purge_tombstones")).one()
        p = c.execute(
            text("SELECT slug, name, owner_ref, purged_at FROM projects WHERE id = :p"),
            {"p": t.project_id},
        ).one()
        with pytest.raises(Exception, match="purged"):
            open_scope(c, f"purged-{t.project_id}")
    assert (
        t.purged_by,
        t.backup_retention_days,
        t.db_history_verified,
        t.db_history_window_hours,
    ) == ("Asha Rao", 14, False, None)
    assert t.row_counts["claims"] == 1 and t.row_counts["raw_documents"] == 2
    assert (
        p.slug == f"purged-{t.project_id}"
        and p.name == "[purged]"
        and p.owner_ref is None
        and p.purged_at == NOW
    )
    assert report.tombstone_id == t.id
    with pytest.raises(NotFound):  # the old slug is gone
        purge_project(world["engine"], "a", now=NOW)
    with pytest.raises(PurgeRefused, match="already purged"):
        purge_project(world["engine"], f"purged-{t.project_id}", now=NOW)
    with pytest.raises(ProjectClosed):
        ingest_upload(world["engine"], world["a"], b"late", "f", now=NOW)


def test_the_report_never_claims_erasure_and_is_explicit_about_backups_and_history(world):
    populate(world)
    report = purge_project(
        world["engine"], "a", now=NOW, execute=True, confirm="a", purged_by="Asha Rao"
    )
    text_ = "\n".join(report.lines)
    assert text_.startswith("Deleted from the live database:")
    assert "NOT erased from backups" in text_ and "up to 14 days (until about 2026-10-26)" in text_
    assert "point-in-time history window is NOT VERIFIED" in text_
    assert "Restoring a backup taken before this purge brings the data back" in text_
    stripped = text_.replace("NOT erased", "")
    assert "erase" not in stripped.lower()  # no sentence says the data is erased
    with world["engine"].connect() as c:
        note = c.execute(text("SELECT note FROM purge_tombstones")).scalar_one()
    assert "NOT erased from backups" in note and "NOT VERIFIED" in note


def test_a_verified_history_window_is_reported_as_such_but_still_not_erasure(world, monkeypatch):
    populate(world)
    monkeypatch.setenv("DB_HISTORY_WINDOW_HOURS", "6")
    monkeypatch.setenv("BACKUP_RETENTION_DAYS", "30")
    report = purge_project(
        world["engine"], "a", now=NOW, execute=True, confirm="a", purged_by="Asha Rao"
    )
    joined = "\n".join(report.lines)
    assert "reported by the owner as 6 hours" in joined and "up to 30 days" in joined
    with world["engine"].connect() as c:
        t = c.execute(
            text(
                "SELECT db_history_verified, db_history_window_hours, backup_retention_days FROM purge_tombstones"
            )
        ).one()
    assert tuple(t) == (True, 6, 30)
    assert "NOT erased from provider history" in joined


def test_purge_refuses_when_something_else_still_references_the_raw_data(world):
    p = populate(world)
    with world["engine"].begin() as c:
        c.execute(
            text(
                "INSERT INTO quarantine (raw_version_id, reason, created_at) VALUES (:v, 'x', :t)"
            ),
            {"v": p["up"].raw_version_id, "t": NOW},
        )
    before = table_counts(world["engine"])
    with pytest.raises(PurgeRefused, match="quarantine"):
        purge_project(world["engine"], "a", now=NOW, execute=True, confirm="a", purged_by="Asha")
    assert table_counts(world["engine"]) == before  # nothing was deleted


def test_purge_refuses_a_document_another_project_also_references(world):
    p = populate(world)
    with world["engine"].begin() as c:
        c.execute(
            text(
                "INSERT INTO project_documents (project_id, raw_document_id, role, added_at) VALUES (:b, :d, 'upload', :t)"
            ),
            {"b": world["b"].project_id, "d": p["up"].document_id, "t": NOW},
        )
    with pytest.raises(PurgeRefused, match="another project"):
        purge_project(world["engine"], "a", now=NOW, execute=True, confirm="a", purged_by="Asha")


def test_purge_refuses_a_document_that_is_not_namespaced_to_the_project(world):
    p = populate(world)
    with world["engine"].begin() as c:
        c.execute(
            text("UPDATE raw_documents SET url = 'official/shared' WHERE id = :d"),
            {"d": p["up"].document_id},
        )
    with pytest.raises(PurgeRefused, match="not namespaced"):
        purge_project(world["engine"], "a", now=NOW, execute=True, confirm="a", purged_by="Asha")


def test_purge_waits_for_an_in_flight_ingest_and_then_deletes_what_it_wrote(world):
    state = {}

    def slow(point):
        if point == "stored":
            state["stored"] = True
            time.sleep(0.8)  # the ingest holds the project row FOR SHARE for this long

    t = threading.Thread(
        target=lambda: state.update(
            r=ingest_upload(world["engine"], world["a"], b"in flight", "f", now=NOW, hook=slow)
        )
    )
    t.start()
    while not state.get("stored"):
        time.sleep(0.01)
    report = purge_project(
        world["engine"], "a", now=NOW, execute=True, confirm="a", purged_by="Asha"
    )
    t.join(timeout=30)
    assert state["r"].outcome == "new_version"  # the ingest committed first
    assert report.counts["raw_documents"] == 1  # and the purge, having waited, saw and deleted it
    assert table_counts(world["engine"])["raw_documents"] == 0


def test_purge_does_not_touch_the_sports_data(engine, tmp_path):
    from sie.pipeline.runner import SourceInput, run_ingest
    from sie.reference import seed_reference

    root = Path(__file__).resolve().parents[2]
    shutil.copytree(root / "data" / "reference", tmp_path / "reference")
    settings = Settings(
        _env_file=None, database_url=engine.url.render_as_string(hide_password=False),
        data_dir=tmp_path, raw_store_backend="db",
    )  # fmt: skip
    with engine.begin() as c:
        seed_reference(c, settings.reference_dir)
        create_project(c, slug="a", name="A", now=NOW)
    run_ingest(
        engine,
        SourceInput(
            source="manual", url="file://manual/golden.csv",
            content=(root / "tests/fixtures/manual/golden.csv").read_bytes(), content_type="text/csv",
            extension="csv", parse=lambda raw: manual.parse_manual_csv(raw, max_rows=1000, max_cell_chars=200),
        ),
        settings,
    )  # fmt: skip
    with engine.connect() as c:
        scope = open_scope(c, "a", for_write=True)
    ingest_upload(engine, scope, b"x,y\n1,2\n", "f.csv", now=NOW)
    sports = {
        t: count(engine, f"SELECT count(*) FROM {t}")
        for t in ("placings", "events", "source_observations", "ingest_runs")
    }
    raw_official = count(engine, "SELECT count(*) FROM raw_documents WHERE source <> 'upload'")
    purge_project(engine, "a", now=NOW, execute=True, confirm="a", purged_by="Asha")
    assert {t: count(engine, f"SELECT count(*) FROM {t}") for t in sports} == sports
    assert count(engine, "SELECT count(*) FROM raw_documents") == raw_official
    assert count(engine, "SELECT count(*) FROM v_medal_facts") > 0


# ---- prune --------------------------------------------------------------------------------------


def aged(world):
    """A full web copy registered 100 days ago, and a fresh one."""
    old = register(world, retention_mode="full", policies=POLICIES, now=NOW - timedelta(days=100))
    fresh = register(
        world, content=PAGE + b"new", retention_mode="full", policies=POLICIES, now=NOW
    )
    with world["engine"].begin() as c:
        cid = add_claim(
            c, world["a"], old.source_id,
            ClaimInput(subject="India", metric="gold", value_text="6", excerpt="6 gold", locator_kind="char_range",
                       locator={"start": 0, "end": 5}, extraction_method="manual", extractor_version="t"),
            now=NOW,
        ).claim_id  # fmt: skip
    return old, fresh, cid


def test_prune_is_a_dry_run_by_default_and_lists_only_expired_copies(world):
    old, fresh, _ = aged(world)
    report = prune_expired(world["engine"], now=NOW)
    assert not report.executed and [c["source_id"] for c in report.candidates] == [old.source_id]
    assert (
        "Dry run" in report.lines[0]
        and count(world["engine"], "SELECT count(*) FROM raw_blobs") == 2
    )


def test_a_destructive_prune_needs_the_confirmation_token(world):
    aged(world)
    with pytest.raises(PurgeRefused):
        prune_expired(world["engine"], now=NOW, execute=True)
    with pytest.raises(PurgeRefused):
        prune_expired(world["engine"], now=NOW, execute=True, confirm="yes")
    assert count(world["engine"], "SELECT count(*) FROM raw_blobs") == 2


def test_prune_drops_only_the_bytes_and_keeps_the_source_and_its_claims(world):
    old, fresh, cid = aged(world)
    report = prune_expired(world["engine"], now=NOW, execute=True, confirm="prune")
    assert report.pruned == 1 and not report.blocked
    with world["engine"].connect() as c:
        s = get_source(c, world["a"], old.source_id)
        assert (s.retention_mode, s.pruned_at, s.size_bytes) == ("metadata", NOW, len(PAGE))
        with pytest.raises(BytesNotRetained):
            read_bytes(c, world["a"], old.source_id)
        assert (
            read_bytes(c, world["a"], fresh.source_id) == PAGE + b"new"
        )  # the unexpired copy is untouched
    assert count(world["engine"], "SELECT count(*) FROM claims") == 1
    assert count(world["engine"], "SELECT count(*) FROM raw_blobs") == 1
    assert (
        count(world["engine"], "SELECT count(*) FROM raw_versions") == 2
    )  # the version row stays as a tombstone
    assert (
        prune_expired(world["engine"], now=NOW, execute=True, confirm="prune").pruned == 0
    )  # idempotent
    with world["engine"].begin() as c:
        result = verify_claim(c, world["a"], cid, now=NOW)
    assert result.status == "unverified"  # a pruned source can no longer back a re-check


def test_prune_leaves_a_source_alone_when_it_would_collide_with_an_excerpt_row(world):
    old, _, _ = aged(world)
    register(world)  # an excerpt-only source with the same URL and content
    report = prune_expired(world["engine"], now=NOW, execute=True, confirm="prune")
    assert report.pruned == 0 and len(report.blocked) == 1
    assert count(world["engine"], "SELECT count(*) FROM raw_blobs") == 2


def test_prune_skips_a_purged_project(world):
    aged(world)
    purge_project(world["engine"], "a", now=NOW, execute=True, confirm="a", purged_by="Asha")
    assert prune_expired(world["engine"], now=NOW, execute=True, confirm="prune").pruned == 0


# ---- integrity ----------------------------------------------------------------------------------


def test_the_integrity_check_is_clean_then_finds_tampering_and_counts_unretained_sources(world):
    p = populate(world)
    with world["engine"].connect() as c:
        report = check_project(c, world["a"])
    assert (
        report.ok
        and report.files_checked == 2
        and report.bytes_not_retained == 1
        and report.claims_ok == 1
    )
    import gzip

    with world["engine"].begin() as c:
        c.execute(
            text("UPDATE raw_blobs SET content = :c WHERE version_id = :v"),
            {"c": gzip.compress(b"tampered"), "v": p["up"].raw_version_id},
        )
        c.execute(text("UPDATE claims SET excerpt_sha256 = :h"), {"h": "0" * 64})
    with world["engine"].connect() as c:
        bad = check_project(c, world["a"])
    assert not bad.ok and len(bad.problems) == 2
    assert any("no longer match" in x for x in bad.problems) and any(
        "excerpt" in x for x in bad.problems
    )


def test_the_integrity_check_flags_a_document_that_is_not_namespaced_to_the_project(world):
    p = populate(world)
    with world["engine"].begin() as c:
        c.execute(
            text("UPDATE raw_documents SET url = 'elsewhere' WHERE id = :d"),
            {"d": p["up"].document_id},
        )
    with world["engine"].connect() as c:
        assert any("not namespaced" in x for x in check_project(c, world["a"]).problems)


def test_listing_sources_shows_what_was_registered(world):
    populate(world)
    with world["engine"].connect() as c:
        kinds = sorted((s.kind, s.retention_mode) for s in list_sources(c, world["a"]))
    assert kinds == [("upload", "full"), ("web", "excerpt"), ("web", "full")]
