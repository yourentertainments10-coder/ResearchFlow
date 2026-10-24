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
    check_project,
    create_project,
    detect_conflicts,
    evidence_ref_from_source,
    figure_from_claim,
    get_claim,
    get_source,
    ingest_upload,
    list_claims,
    list_conflicts,
    list_sources,
    open_scope,
    project_health,
    read_bytes,
    register_fetched_source,
    resolve_conflict,
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


def _b_conflict(w):
    """Give project B a second, disagreeing claim, so it has an open conflict of its own."""
    other = ClaimInput(
        subject="s", metric="m", value_text="2", excerpt="2", locator_kind="none", locator={},
        extraction_method="manual", extractor_version="t",
    )  # fmt: skip
    with w["engine"].begin() as c:
        add_claim(c, w["b"], w["b_src"].source_id, other, now=NOW)
        (conflict,) = detect_conflicts(c, w["b"], now=NOW)
    return conflict


def case_project_health(w):
    """A's health report counts only A's rows, even though B has a source, a claim and a conflict."""
    from datetime import timedelta

    _b_conflict(w)
    with w["engine"].connect() as c:
        r = project_health(c, w["a"], NOW, timedelta(days=30), timedelta(days=180))
    assert [i.source_id for i in r.sources] == [w["a_src"].source_id]
    assert sum(r.claims_by_status.values()) == 1 and r.open_conflict_ids == ()


def case_detect_conflicts(w):
    _b_conflict(w)  # B has a conflict; A's detection must neither see nor touch it
    with w["engine"].begin() as c:
        assert detect_conflicts(c, w["a"], now=NOW) == []
    with w["engine"].connect() as c:
        assert list_conflicts(c, w["a"]) == [] and len(list_conflicts(c, w["b"])) == 1


def case_list_conflicts(w):
    _b_conflict(w)
    with w["engine"].connect() as c:
        assert list_conflicts(c, w["a"]) == [] and list_conflicts(c, w["a"], "open") == []


def case_resolve_conflict(w):
    foreign = _b_conflict(w)
    kw = {"status": "dismissed", "note": "n", "resolved_by": "Asha", "now": NOW}
    with w["engine"].begin() as c, pytest.raises(NotFound) as err:
        resolve_conflict(c, w["a"], foreign, **kw)
    with w["engine"].begin() as c, pytest.raises(NotFound) as missing:
        resolve_conflict(c, w["a"], 10**9, **kw)
    assert str(err.value) == str(missing.value)
    with w["engine"].connect() as c:
        assert list_conflicts(c, w["b"])[0].status == "open"  # B's conflict is untouched


def case_evidence_ref_from_source(w):
    expected = missing_message(w, evidence_ref_from_source)
    with w["engine"].connect() as c, pytest.raises(NotFound) as err:
        evidence_ref_from_source(c, w["a"], w["b_src"].source_id)
    assert str(err.value) == expected


def case_figure_from_claim(w):
    _same_as_missing(
        w, lambda c, scope, i: figure_from_claim(c, scope, i, figure_id="f", label="l")
    )


def case_check_project(w):
    # A tampered B blob must never show up in A's report, and A's report is clean.
    with w["engine"].begin() as c:
        c.execute(text("UPDATE claims SET excerpt_sha256 = :h WHERE project_id = :p"),
                  {"h": "0" * 64, "p": w["b"].project_id})  # fmt: skip
    with w["engine"].connect() as c:
        a_report, b_report = check_project(c, w["a"]), check_project(c, w["b"])
    assert a_report.ok and a_report.sources == 1
    assert not b_report.ok


def case_register_fetched_source(w):
    before_b = snapshot_for_project(w, "b")
    r = register_fetched_source(
        w["engine"],
        w["a"],
        content=SECRET,
        requested_url="https://e.org/x",
        title="t",
        tier=2,
        now=NOW,
    )
    assert r.source_id != w["b_src"].source_id and r.created_source  # B's bytes are not an oracle
    assert snapshot_for_project(w, "b") == before_b


def _stored_for_b(w, tmp):
    from sie.evidence.collect import collect
    from sie.evidence.dataset_store import RowsPermission, save_dataset

    page = b"<table><tr><th>A</th></tr><tr><td>secret-b</td></tr></table>"
    c = collect(
        page,
        "text/html",
        url="https://b.example.gov.in/p",
        retrieved_at="2026-10-24T09:00:00+00:00",
    )
    root = tmp / "datasets"
    key = save_dataset(root, w["b"], c, now=NOW, permission=RowsPermission(True, "own_upload")).key
    return root, key


def _dataset_case(fn_name):
    def case(w):
        import tempfile
        from pathlib import Path

        from sie.evidence import dataset_store as ds

        with tempfile.TemporaryDirectory() as t:
            root, key = _stored_for_b(w, Path(t))
            fn = getattr(ds, fn_name)
            if fn_name == "list_datasets":
                assert fn(root, w["a"], now=NOW) == []
            elif fn_name == "find_cached":
                assert fn(root, w["a"], page_url="https://b.example.gov.in/p", now=NOW) == []
            elif fn_name == "save_dataset":
                from sie.evidence.collect import collect

                c = collect(
                    b"x\n1\n",
                    "text/csv",
                    url="https://b.example.gov.in/p",
                    retrieved_at="2026-10-24T09:00:00+00:00",
                )
                saved = fn(
                    root, w["a"], c, now=NOW, permission=ds.RowsPermission(True, "own_upload")
                )
                assert (root / f"p{w['a'].project_id}" / saved.key).exists()
                assert not (root / f"p{w['b'].project_id}" / saved.key).exists()
            else:
                with pytest.raises(NotFound) as err:
                    kwargs = {"now": NOW} if fn_name == "open_stored" else {}
                    fn(root, w["a"], key, **kwargs)
                with pytest.raises(NotFound) as missing:
                    kwargs = {"now": NOW} if fn_name == "open_stored" else {}
                    fn(root, w["a"], "0" * 12 + "-" + "0" * 8, **kwargs)
                assert str(err.value) == str(missing.value)

    return case


CROSS_PROJECT_CASES = {
    "sie.evidence.dataset_store.save_dataset": _dataset_case("save_dataset"),
    "sie.evidence.dataset_store.open_stored": _dataset_case("open_stored"),
    "sie.evidence.dataset_store.list_datasets": _dataset_case("list_datasets"),
    "sie.evidence.dataset_store.find_cached": _dataset_case("find_cached"),
    "sie.evidence.dataset_store.delete_dataset": _dataset_case("delete_dataset"),
    "sie.evidence.health.project_health": case_project_health,
    "sie.evidence.integrity.check_project": case_check_project,
    "sie.evidence.retention.register_fetched_source": case_register_fetched_source,
    "sie.evidence.conflicts.detect_conflicts": case_detect_conflicts,
    "sie.evidence.conflicts.list_conflicts": case_list_conflicts,
    "sie.evidence.conflicts.resolve_conflict": case_resolve_conflict,
    "sie.evidence.adapters.evidence_ref_from_source": case_evidence_ref_from_source,
    "sie.evidence.adapters.figure_from_claim": case_figure_from_claim,
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
