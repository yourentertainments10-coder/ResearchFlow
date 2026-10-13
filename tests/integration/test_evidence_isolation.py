"""Project isolation. Mandatory: every public scoped function must have a cross-project case here.

``SCOPED_API`` is filled by the ``@scoped_api`` decorator. ``test_every_scoped_function_has_a_case``
fails when someone adds a public function without registering a case below, so isolation cannot be
forgotten by omission. Each case is run as project A holding project B's ids, and must either return
only A's data or raise the same ``NotFound`` as for an id that does not exist.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import text

import sie.evidence  # noqa: F401  (imports register every scoped function)
from sie.evidence import (
    SCOPED_API,
    ClaimInput,
    NotFound,
    add_claim,
    create_project,
    get_claim,
    get_source,
    ingest_upload,
    list_claims,
    list_sources,
    open_scope,
    read_bytes,
    review_claim,
    verify_claim,
)

NOW = datetime(2026, 10, 12, 10, 0, tzinfo=UTC)
SECRET = b"project B's private numbers\n99,100\n"
CLAIM = ClaimInput(
    subject="s", metric="m", value_text="1", excerpt="1", locator_kind="none", locator={},
    extraction_method="manual", extractor_version="t",
)  # fmt: skip


def snapshot(engine):
    """Every row of every ledger and raw table, so a case can prove it changed nothing."""
    with engine.connect() as c:
        return {
            t: c.execute(text(f"SELECT count(*) FROM {t}")).scalar_one()
            for t in (
                "raw_documents", "raw_versions", "raw_blobs", "raw_fetches", "project_documents",
                "evidence_sources", "claims", "claim_reviews", "claim_conflicts",
            )
        }  # fmt: skip


@pytest.fixture()
def world(engine):
    with engine.begin() as c:
        create_project(c, slug="a", name="A", now=NOW)
        create_project(c, slug="b", name="B", now=NOW)
    with engine.connect() as c:
        a, b = open_scope(c, "a", for_write=True), open_scope(c, "b", for_write=True)
    a_src = ingest_upload(engine, a, b"project A data", "a.csv", now=NOW)
    b_src = ingest_upload(engine, b, SECRET, "b.csv", now=NOW)
    with engine.begin() as c:
        a_claim = add_claim(c, a, a_src.source_id, CLAIM, now=NOW).claim_id
        b_claim = add_claim(c, b, b_src.source_id, CLAIM, now=NOW).claim_id
    return {
        "engine": engine, "a": a, "b": b, "a_src": a_src, "b_src": b_src,
        "a_claim": a_claim, "b_claim": b_claim,
    }  # fmt: skip


def missing_message(world, fn):
    """The text raised for an id that exists nowhere, to compare against the cross-project text."""
    with world["engine"].connect() as c, pytest.raises(NotFound) as err:
        fn(c, world["a"], 10**9)
    return str(err.value)


# --- one case per scoped function: (callable(world)) -------------------------------------------------


def case_ingest_upload(w):
    before_b = snapshot_for_project(w, "b")
    again = ingest_upload(w["engine"], w["a"], SECRET, "stolen.csv", now=NOW)  # A uploads B's bytes
    assert again.source_id != w["b_src"].source_id  # a separate copy, never B's source
    assert again.outcome == "new_version"  # and no hint that B already held these bytes
    assert snapshot_for_project(w, "b") == before_b


def case_list_sources(w):
    with w["engine"].connect() as c:
        ids = {s.id for s in list_sources(c, w["a"])}
    assert ids == {w["a_src"].source_id} and w["b_src"].source_id not in ids


def case_get_source(w):
    expected = missing_message(w, get_source)
    with w["engine"].connect() as c, pytest.raises(NotFound) as err:
        get_source(c, w["a"], w["b_src"].source_id)
    assert str(err.value) == expected  # identical to "does not exist": no existence oracle


def case_read_bytes(w):
    expected = missing_message(w, read_bytes)
    with w["engine"].connect() as c, pytest.raises(NotFound) as err:
        read_bytes(c, w["a"], w["b_src"].source_id)
    assert str(err.value) == expected


def _same_as_missing(w, call):
    """Run call(conn, scope, id) for B's id and for a non-existent id; the errors must be identical."""
    with w["engine"].connect() as c, pytest.raises(NotFound) as missing:
        call(c, w["a"], 10**9)
    with w["engine"].connect() as c, pytest.raises(NotFound) as foreign:
        call(c, w["a"], w["b_claim"])
    assert str(foreign.value) == str(missing.value)


def case_add_claim(w):
    before = snapshot(w["engine"])
    with w["engine"].begin() as c, pytest.raises(NotFound) as err:
        add_claim(c, w["a"], w["b_src"].source_id, CLAIM, now=NOW)  # B's source id, A's scope
    with w["engine"].begin() as c, pytest.raises(NotFound) as missing:
        add_claim(c, w["a"], 10**9, CLAIM, now=NOW)
    assert str(err.value) == str(missing.value)
    assert snapshot(w["engine"]) == before


def case_get_claim(w):
    _same_as_missing(w, get_claim)


def case_list_claims(w):
    with w["engine"].connect() as c:
        ids = {x.id for x in list_claims(c, w["a"])}
        assert ids == {w["a_claim"]} and w["b_claim"] not in ids
        with pytest.raises(NotFound):
            list_claims(c, w["a"], w["b_src"].source_id)


def case_review_claim(w):
    before = snapshot(w["engine"])

    def call(c, scope, claim_id):
        review_claim(c, scope, claim_id, status="disputed", reason="r", actor="Asha", now=NOW)

    with w["engine"].begin() as c, pytest.raises(NotFound):
        call(c, w["a"], w["b_claim"])
    assert snapshot(w["engine"]) == before


def case_verify_claim(w):
    before = snapshot(w["engine"])
    _same_as_missing(w, lambda c, scope, i: verify_claim(c, scope, i, now=NOW))
    assert snapshot(w["engine"]) == before


CROSS_PROJECT_CASES = {
    "sie.evidence.claims.add_claim": case_add_claim,
    "sie.evidence.claims.get_claim": case_get_claim,
    "sie.evidence.claims.list_claims": case_list_claims,
    "sie.evidence.claims.review_claim": case_review_claim,
    "sie.evidence.verify.verify_claim": case_verify_claim,
    "sie.evidence.ingest.ingest_upload": case_ingest_upload,
    "sie.evidence.sources.list_sources": case_list_sources,
    "sie.evidence.sources.get_source": case_get_source,
    "sie.evidence.sources.read_bytes": case_read_bytes,
}


def snapshot_for_project(w, slug):
    with w["engine"].connect() as c:
        pid = w[slug].project_id
        return {
            "sources": c.execute(
                text(
                    "SELECT id, content_sha256 FROM evidence_sources WHERE project_id = :p ORDER BY id"
                ),
                {"p": pid},
            ).all(),
            "documents": c.execute(
                text(
                    "SELECT raw_document_id FROM project_documents WHERE project_id = :p ORDER BY 1"
                ),
                {"p": pid},
            ).all(),
        }


def test_every_scoped_function_has_a_case():
    assert set(SCOPED_API) == set(CROSS_PROJECT_CASES), (
        "a public scoped function was added or removed without updating the cross-project cases"
    )


@pytest.mark.parametrize("name", sorted(CROSS_PROJECT_CASES))
def test_cross_project_access_is_refused_or_filtered(world, name):
    CROSS_PROJECT_CASES[name](world)


def test_same_bytes_in_two_projects_are_two_independent_stored_copies(world):
    """A and B uploaded different bytes in the fixture; now both hold the same bytes."""
    a_same = ingest_upload(world["engine"], world["a"], SECRET, "same.csv", now=NOW)
    assert (
        a_same.outcome == "new_version"
    )  # not 'unchanged': B's identical bytes are invisible to A
    with world["engine"].connect() as c:
        rows = c.execute(
            text(
                """SELECT d.url, v.id, b.version_id FROM raw_documents d
                   JOIN raw_versions v ON v.document_id = d.id
                   JOIN raw_blobs b ON b.version_id = v.id
                   WHERE v.sha256 = :h ORDER BY d.id"""
            ),
            {"h": a_same.sha256},
        ).all()
    assert len(rows) == 2 and rows[0].url != rows[1].url and rows[0].id != rows[1].id
    assert {f"rf-project/{world[s].project_id}/upload/{a_same.sha256}" for s in "ab"} == {
        r.url for r in rows
    }


def test_no_public_function_accepts_a_raw_id_or_a_bare_hash():
    import inspect

    forbidden = {
        "raw_version_id",
        "raw_document_id",
        "document_id",
        "version_id",
        "sha256",
        "content_sha256",
    }
    for key, fn in SCOPED_API.items():
        names = list(inspect.signature(fn).parameters)
        params = set(names)
        assert not params & forbidden, (
            f"{key} takes {params & forbidden}: callers must use source ids"
        )
        assert names[1] == "scope", f"{key} must take the scope as its second parameter"
