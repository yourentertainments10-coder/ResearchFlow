"""Simultaneous uploads: real threads, real connections, a barrier so the writers genuinely overlap.

The explicit advisory lock in ``ingest_upload`` is the guarantee. ``test_store_raw_serialises_*`` pins
the behaviour of the existing ``store_raw`` that the design relies on as a second layer: the upsert on
``raw_documents`` row-locks the document. If someone changes that statement so it no longer locks, this
test fails and the race it would reopen is visible before it ships.
"""

from __future__ import annotations

import threading
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from sie.evidence import (
    NotFound,
    ProjectClosed,
    create_project,
    get_source,
    ingest_upload,
    open_scope,
)
from sie.pipeline.raw import store_raw

NOW = datetime(2026, 10, 12, 10, 0, tzinfo=UTC)
THREADS = 8
ROUNDS = 5


def count(engine, sql, **params):
    with engine.connect() as c:
        return c.execute(text(sql), params).scalar_one()


def run_threads(targets):
    """Start every callable at once; return (results, errors) in no particular order."""
    barrier = threading.Barrier(len(targets))
    results, errors = [], []

    def wrap(fn):
        def go():
            barrier.wait()
            try:
                results.append(fn())
            except Exception as exc:  # noqa: BLE001 - the test inspects every failure
                errors.append(exc)

        return go

    threads = [threading.Thread(target=wrap(t)) for t in targets]
    [t.start() for t in threads]
    [t.join(timeout=60) for t in threads]
    assert not any(t.is_alive() for t in threads), "a writer hung"
    return results, errors


def slow_hook(point):
    if point == "locked":  # hold the lock a moment so the other writers really queue behind it
        time.sleep(0.05)


@pytest.fixture()
def scopes(engine):
    out = {}
    with engine.begin() as c:
        for slug in ("alpha", "beta"):
            create_project(c, slug=slug, name=slug.title(), now=NOW)
    with engine.connect() as c:
        for slug in ("alpha", "beta"):
            out[slug] = open_scope(c, slug, for_write=True)
    return out


def test_same_bytes_same_project_many_threads_leave_one_copy(engine, scopes):
    for r in range(ROUNDS):
        data = f"round {r}\n1,2\n".encode()
        results, errors = run_threads(
            [
                lambda d=data: ingest_upload(
                    engine, scopes["alpha"], d, "f.csv", now=NOW, hook=slow_hook
                )
            ]
            * THREADS
        )
        assert not errors, errors
        assert len({x.source_id for x in results}) == 1
        assert Counter(x.outcome for x in results) == {"new_version": 1, "unchanged": THREADS - 1}
        assert sum(x.created_source for x in results) == 1
    for table in (
        "raw_documents",
        "raw_versions",
        "raw_blobs",
        "project_documents",
        "evidence_sources",
    ):
        assert count(engine, f"SELECT count(*) FROM {table}") == ROUNDS
    assert count(engine, "SELECT count(*) FROM raw_fetches") == ROUNDS * THREADS


def test_different_bytes_many_threads_make_distinct_sources(engine, scopes):
    results, errors = run_threads(
        [
            (lambda i=i: ingest_upload(engine, scopes["alpha"], f"file {i}".encode(), "f", now=NOW))
            for i in range(THREADS)
        ]
    )
    assert not errors, errors
    assert len({x.source_id for x in results}) == THREADS
    assert count(engine, "SELECT count(*) FROM raw_versions") == THREADS
    assert count(engine, "SELECT count(*) FROM raw_versions WHERE version_no <> 1") == 0


def test_same_bytes_into_two_projects_stay_independent(engine, scopes):
    data = b"identical bytes"
    targets = [
        (lambda s=slug: ingest_upload(engine, scopes[s], data, "f", now=NOW, hook=slow_hook))
        for slug in ("alpha", "beta")
        for _ in range(4)
    ]
    results, errors = run_threads(targets)
    assert not errors, errors
    by_project = Counter()
    for x in results:
        with engine.connect() as c:
            owner = c.execute(
                text("SELECT project_id FROM evidence_sources WHERE id = :i"), {"i": x.source_id}
            ).scalar_one()
        by_project[(owner, x.outcome)] += 1
    a, b = scopes["alpha"].project_id, scopes["beta"].project_id
    # each project saw exactly one first-time upload: the other project's copy never made it "unchanged"
    assert by_project == {
        (a, "new_version"): 1,
        (a, "unchanged"): 3,
        (b, "new_version"): 1,
        (b, "unchanged"): 3,
    }
    assert count(engine, "SELECT count(*) FROM raw_versions") == 2
    assert count(engine, "SELECT count(*) FROM raw_blobs") == 2


def test_a_failure_between_the_raw_write_and_the_ledger_write_leaves_nothing(engine, scopes):
    def boom(point):
        if point == "stored":
            raise RuntimeError("crash after the bytes were written")

    with pytest.raises(RuntimeError):
        ingest_upload(engine, scopes["alpha"], b"data", "f", now=NOW, hook=boom)
    for table in (
        "raw_documents", "raw_versions", "raw_blobs", "raw_fetches", "project_documents",
        "evidence_sources",
    ):  # fmt: skip
        assert count(engine, f"SELECT count(*) FROM {table}") == 0


def test_an_ingest_waits_for_a_concurrent_purge_style_lock_then_refuses(engine, scopes):
    """A writer holding the project row FOR UPDATE (what a purge does) makes ingest wait, not interleave."""
    other = create_engine(engine.url)
    blocker = other.connect()
    tx = blocker.begin()
    blocker.execute(
        text("SELECT id FROM projects WHERE id = :p FOR UPDATE"), {"p": scopes["alpha"].project_id}
    )
    outcome: dict = {}

    def attempt():
        try:
            outcome["r"] = ingest_upload(engine, scopes["alpha"], b"late", "f", now=NOW)
        except Exception as exc:  # noqa: BLE001
            outcome["e"] = exc

    t = threading.Thread(target=attempt)
    t.start()
    time.sleep(0.5)
    assert t.is_alive() and not outcome  # blocked behind the project lock, nothing written yet
    blocker.execute(
        text("UPDATE projects SET purged_at = :t WHERE id = :p"),
        {"t": NOW, "p": scopes["alpha"].project_id},
    )
    tx.commit()
    t.join(timeout=30)
    blocker.close()
    other.dispose()
    assert isinstance(outcome.get("e"), ProjectClosed), outcome
    assert count(engine, "SELECT count(*) FROM raw_documents") == 0


def test_the_advisory_lock_key_is_per_document_so_other_documents_do_not_wait(engine, scopes):
    """Two different documents proceed in parallel: the lock is not a global bottleneck."""
    started = {}

    def hook_for(name):
        def hook(point):
            if point == "locked":
                started[name] = time.monotonic()
                time.sleep(0.4)

        return hook

    results, errors = run_threads(
        [
            lambda: ingest_upload(
                engine, scopes["alpha"], b"doc one", "a", now=NOW, hook=hook_for("a")
            ),
            lambda: ingest_upload(
                engine, scopes["alpha"], b"doc two", "b", now=NOW, hook=hook_for("b")
            ),
        ]
    )
    assert not errors and len(results) == 2
    assert (
        abs(started["a"] - started["b"]) < 0.3
    )  # both held their (different) locks at the same time


def test_store_raw_serialises_concurrent_new_versions_of_one_url(engine):
    """Pins what the design relies on: same source and URL, different content, contiguous versions."""

    def write(i):
        def go():
            with engine.begin() as conn:
                return store_raw(
                    conn, source="web", url="https://e.org/page", content=f"v{i}".encode(),
                    content_type="text/plain", extension="txt", now=NOW, run_id=None,
                    backend="db", raw_dir=Path("/nonexistent"),
                )  # fmt: skip

        return go

    results, errors = run_threads([write(i) for i in range(THREADS)])
    assert not errors, errors
    assert sorted(r.version_no for r in results) == list(range(1, THREADS + 1))
    assert count(engine, "SELECT count(*) FROM raw_documents") == 1


def test_a_closed_or_missing_project_never_leaks_through_a_race(engine, scopes):
    """Reading another project's source id still raises the plain NotFound."""
    r = ingest_upload(engine, scopes["alpha"], b"mine", "f", now=NOW)
    with engine.connect() as c, pytest.raises(NotFound):
        get_source(c, scopes["beta"], r.source_id)
