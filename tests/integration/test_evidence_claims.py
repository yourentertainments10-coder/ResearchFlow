"""Claims, reviews and verification (CSV and JSON), including every way a claim must NOT be confirmed."""

from __future__ import annotations

import gzip
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from sie.evidence import (
    ClaimInput,
    EvidenceError,
    NotFound,
    add_claim,
    claim_hash,
    create_project,
    get_claim,
    ingest_upload,
    list_claims,
    open_scope,
    review_claim,
    verify_claim,
)
from sie.evidence.verify import PENDING_XLSX, VERIFIER_VERSION

NOW = datetime(2026, 10, 12, 10, 0, tzinfo=UTC)
CSV = b"country,gold\nIndia,6\nJapan,8\n"
JSON = b'[{"country":"India","gold":6,"gdp":1.50}]'


@pytest.fixture()
def world(engine):
    with engine.begin() as c:
        create_project(c, slug="a", name="A", now=NOW)
    with engine.connect() as c:
        scope = open_scope(c, "a", for_write=True)
    return {
        "engine": engine,
        "scope": scope,
        "csv": ingest_upload(engine, scope, CSV, "m.csv", now=NOW),
        "json": ingest_upload(engine, scope, JSON, "m.json", now=NOW),
    }


def csv_claim(**over):
    base = {
        "subject": "India",
        "metric": "gold medals",
        "value_text": "6",
        "excerpt": "6",
        "locator_kind": "table_cell",
        "locator": {"row_id": 1, "column": "gold", "delimiter": ","},
        "extraction_method": "manual",
        "extractor_version": "t1",
        "value_numeric": Decimal("6"),
    }
    base.update(over)
    return ClaimInput(**base)


def add(world, source="csv", **over):
    with world["engine"].begin() as c:
        return add_claim(c, world["scope"], world[source].source_id, csv_claim(**over), now=NOW)


def verify(world, claim_id):
    with world["engine"].begin() as c:
        return verify_claim(c, world["scope"], claim_id, now=NOW)


def current(world, claim_id):
    with world["engine"].connect() as c:
        return get_claim(c, world["scope"], claim_id)


def reviews(world, claim_id):
    with world["engine"].connect() as c:
        return c.execute(
            text("SELECT status, actor, reason FROM claim_reviews WHERE claim_id = :c ORDER BY id"),
            {"c": claim_id},
        ).all()


# ---- recording claims ---------------------------------------------------------------------------


def test_a_new_claim_is_unverified_and_adding_it_again_is_a_no_op(world):
    first = add(world)
    again = add(world)
    assert first.created and not again.created and first.claim_id == again.claim_id
    assert first.claim_hash == again.claim_hash == claim_hash(csv_claim())
    assert current(world, first.claim_id).status == "unverified"
    assert reviews(world, first.claim_id) == []  # nothing is claimed about it yet


def test_the_hash_ignores_nothing_that_matters_and_everything_that_does_not(world):
    base = claim_hash(csv_claim())
    assert claim_hash(csv_claim(excerpt="6 ")) == base  # the quote is not part of the identity
    assert claim_hash(csv_claim(extractor_version="t2")) == base
    for change in (
        {"value_text": "7"},
        {"subject": "Japan"},
        {"metric": "silver"},
        {"period": "2026"},
        {"unit": "medals"},
        {"locator": {"row_id": 2, "column": "gold", "delimiter": ","}},
        {"value_numeric": Decimal("7")},
    ):
        assert claim_hash(csv_claim(**change)) != base, change
    assert claim_hash(csv_claim(value_numeric=Decimal("6.0"))) == base  # 6 and 6.0 are one number


@pytest.mark.parametrize(
    "over",
    [
        {"subject": ""},
        {"value_text": ""},
        {"excerpt": "x" * 501},
        {"extraction_method": "magic"},
        {"extractor_version": ""},
        {"value_numeric": 6.0},  # floats are refused
        {"value_numeric": Decimal("NaN")},
        {"locator": {"row_id": 1, "column": "gold"}},  # no delimiter
        {"value_source": "cached_formula_value"},  # only for xlsx cells
    ],
)
def test_invalid_claims_are_refused_and_write_nothing(world, over):
    with pytest.raises(EvidenceError), world["engine"].begin() as c:
        add_claim(c, world["scope"], world["csv"].source_id, csv_claim(**over), now=NOW)
    with world["engine"].connect() as c:
        assert c.execute(text("SELECT count(*) FROM claims")).scalar_one() == 0


def test_a_claim_on_a_source_that_is_not_in_the_project_is_refused(world):
    with pytest.raises(NotFound), world["engine"].begin() as c:
        add_claim(c, world["scope"], 10**9, csv_claim(), now=NOW)


def test_list_claims_shows_current_status(world):
    a = add(world).claim_id
    b = add(world, value_text="8", excerpt="8", value_numeric=Decimal("8"),
            locator={"row_id": 2, "column": "gold", "delimiter": ","}).claim_id  # fmt: skip
    verify(world, a)
    with world["engine"].connect() as c:
        got = {x.id: x.status for x in list_claims(c, world["scope"])}
        assert got == {a: "source_located", b: "unverified"}
        assert [x.id for x in list_claims(c, world["scope"], world["csv"].source_id)] == [a, b]
        assert list_claims(c, world["scope"], world["json"].source_id) == []


# ---- verification: the positive paths ----------------------------------------------------------


def test_a_matching_csv_claim_becomes_source_located_by_a_system_actor(world):
    claim = add(world).claim_id
    result = verify(world, claim)
    assert (result.status, result.written) == ("source_located", True)
    assert [c.name for c in result.checks] == [
        "excerpt_hash", "stored_bytes", "location", "value_text", "excerpt", "value_numeric",
    ]  # fmt: skip
    assert all(c.passed for c in result.checks)
    ((status, actor, reason),) = reviews(world, claim)
    assert (status, actor) == ("source_located", "system:verifier")
    assert (
        "does not establish that the value is true" in reason
    )  # the limit is in the record itself
    with world["engine"].connect() as c:
        stored = c.execute(
            text("SELECT verifier_version, checks FROM claim_reviews WHERE claim_id = :c"),
            {"c": claim},
        ).one()
    assert stored.verifier_version == VERIFIER_VERSION and len(stored.checks) == 6


def test_verifying_twice_does_not_append_a_duplicate_review(world):
    claim = add(world).claim_id
    verify(world, claim)
    second = verify(world, claim)
    assert not second.written and len(reviews(world, claim)) == 1


def test_a_matching_json_claim_keeps_the_literal(world):
    ok = add(world, "json", value_text="1.50", excerpt="1.50", value_numeric=Decimal("1.5"),
             locator_kind="json_pointer", locator={"pointer": "/0/gdp"}).claim_id  # fmt: skip
    assert verify(world, ok).status == "source_located"
    wrong = add(world, "json", value_text="1.5", excerpt="1.5", value_numeric=Decimal("1.5"),
                locator_kind="json_pointer", locator={"pointer": "/0/gdp"}).claim_id  # fmt: skip
    result = verify(world, wrong)
    assert (
        result.status == "unverified" and result.reason == "mismatch: value_text"
    )  # 1.5 is not 1.50


def test_a_declared_number_format_allows_a_formatted_cell(world, engine):
    src = ingest_upload(engine, world["scope"], b'v\n"1,234"\n', "f.csv", now=NOW)
    world["fmt"] = src
    locator = {
        "row_id": 1,
        "column": "v",
        "delimiter": ",",
        "number_format": {"thousands_separator": ","},
    }
    good = add(world, "fmt", value_text="1,234", excerpt="1,234", value_numeric=Decimal("1234"),
               locator=locator).claim_id  # fmt: skip
    assert verify(world, good).status == "source_located"
    undeclared = add(world, "fmt", value_text="1,234", excerpt="1,234", value_numeric=Decimal("1234"),
                     locator={"row_id": 1, "column": "v", "delimiter": ","}).claim_id  # fmt: skip
    result = verify(world, undeclared)
    assert result.status == "unverified" and result.reason == "mismatch: value_numeric"


# ---- verification: everything that must NOT be confirmed ---------------------------------------


@pytest.mark.parametrize(
    ("over", "reason"),
    [
        (
            {"value_text": "7", "excerpt": "7", "value_numeric": Decimal("7")},
            "mismatch: value_text",
        ),
        ({"excerpt": "six"}, "mismatch: excerpt"),
        ({"value_numeric": Decimal("7")}, "mismatch: value_numeric"),
        (
            {"locator": {"row_id": 9, "column": "gold", "delimiter": ","}},
            "not_located: row_not_found",
        ),
        (
            {"locator": {"row_id": 1, "column": "nope", "delimiter": ","}},
            "not_located: column_not_found",
        ),
        (
            {"locator": {"row_id": 1, "column": "gold", "delimiter": ";"}},
            "not_located: column_not_found",
        ),
    ],
)
def test_a_claim_that_does_not_match_its_source_stays_unverified_with_the_reason(
    world, over, reason
):
    claim = add(world, **over).claim_id
    result = verify(world, claim)
    assert (result.status, result.reason) == ("unverified", reason)
    assert current(world, claim).status == "unverified"
    assert all(s != "source_located" for s, _, _ in reviews(world, claim))


def test_xlsx_claims_are_stored_but_never_confirmed_until_the_datasets_loader_exists(world, engine):
    src = ingest_upload(
        engine, world["scope"], b"PK\x03\x04 not really a workbook", "w.xlsx", now=NOW
    )
    world["xlsx"] = src
    claim = add(world, "xlsx", value_text="42", excerpt="42", value_numeric=Decimal("42"),
                locator_kind="xlsx_cell",
                locator={"sheet": "Sheet1", "cell": "B7", "epoch": 1900}).claim_id  # fmt: skip
    info = current(world, claim)
    assert (
        "xlsx_cached_value_not_evaluated" in info.limitations
    )  # the formula limitation travels along
    result = verify(world, claim)
    assert (result.status, result.reason) == ("unverified", PENDING_XLSX)
    assert not any(c.passed for c in result.checks if c.name == "xlsx")
    assert current(world, claim).status == "unverified"


def test_a_formula_cached_value_claim_is_marked_as_a_cached_value(world, engine):
    src = ingest_upload(engine, world["scope"], b"x", "w2.xlsx", now=NOW)
    world["x2"] = src
    claim = add(world, "x2", value_text="9", excerpt="9", locator_kind="xlsx_cell",
                value_source="cached_formula_value",
                locator={"sheet": "S", "cell": "A1"}).claim_id  # fmt: skip
    info = current(world, claim)
    assert info.value_source == "cached_formula_value" and info.status == "unverified"


@pytest.mark.parametrize("kind", ["char_range", "page", "none"])
def test_other_locator_kinds_are_not_verifiable_in_this_phase(world, kind):
    locator = {"char_range": {"start": 0, "end": 3}, "page": {"page": 1}, "none": {}}[kind]
    claim = add(world, locator_kind=kind, locator=locator).claim_id
    result = verify(world, claim)
    assert result.status == "unverified" and "not_verifiable_in_this_phase" in result.reason


def test_an_excerpt_only_source_cannot_be_rechecked_against_bytes(world, engine):
    with engine.begin() as c:
        sid = c.execute(
            text(
                """INSERT INTO evidence_sources (project_id, kind, retention_mode, content_sha256, size_bytes,
                       title, requested_url, tier, retrieved_at, created_at)
                   VALUES (:p, 'web', 'excerpt', :h, 3, 't', 'https://e.org/a', 3, :t, :t) RETURNING id"""
            ),
            {"p": world["scope"].project_id, "h": "a" * 64, "t": NOW},
        ).scalar_one()
    world["web"] = type("S", (), {"source_id": sid})()
    claim = add(world, "web").claim_id
    result = verify(world, claim)
    assert result.status == "unverified" and result.reason.startswith("bytes_not_retained")
    assert "only the excerpt's own hash" in result.reason


def test_tampered_stored_bytes_fail_the_check_instead_of_being_trusted(world, engine):
    claim = add(world).claim_id
    with engine.begin() as c:
        c.execute(
            text("UPDATE raw_blobs SET content = :c"),
            {"c": gzip.compress(b"country,gold\nIndia,6\n")},
        )
    result = verify(world, claim)
    assert result.status == "unverified" and result.reason.startswith("stored bytes unusable")


def test_a_tampered_excerpt_hash_is_caught(world, engine):
    claim = add(world).claim_id
    with engine.begin() as c:
        c.execute(text("UPDATE claims SET excerpt_sha256 = :h"), {"h": "0" * 64})
    result = verify(world, claim)
    assert result.status == "unverified" and "excerpt no longer matches" in result.reason


def test_the_time_budget_is_enforced(world):
    claim = add(world).claim_id
    with world["engine"].begin() as c:
        result = verify_claim(c, world["scope"], claim, now=NOW, budget_seconds=-1.0)
    assert result.status == "unverified" and result.reason == "time_budget_exceeded"


# ---- reviews: who may say what -----------------------------------------------------------------


def review(world, claim, **kw):
    kw.setdefault("now", NOW)
    with world["engine"].begin() as c:
        return review_claim(c, world["scope"], claim, **kw)


def test_human_confirmation_means_a_named_person_not_truth(world):
    claim = add(world).claim_id
    review(
        world,
        claim,
        status="human_confirmed",
        reason="Checked against the printed table",
        actor="Asha Rao",
    )
    info = current(world, claim)
    assert info.status == "human_confirmed"
    ((status, actor, _),) = reviews(world, claim)
    assert (status, actor) == ("human_confirmed", "Asha Rao")


def test_people_cannot_claim_a_machine_check_and_machines_cannot_claim_a_human_one(world):
    claim = add(world).claim_id
    with pytest.raises(EvidenceError, match="verifier"):
        review(world, claim, status="source_located", reason="r", actor="Asha Rao")
    with pytest.raises(EvidenceError, match="named person"):
        review(world, claim, status="human_confirmed", reason="r", actor="system:verifier")
    with pytest.raises(EvidenceError):
        review(world, claim, status="verified", reason="r", actor="Asha Rao")
    with pytest.raises(EvidenceError):
        review(world, claim, status="disputed", reason="  ", actor="Asha Rao")
    assert reviews(world, claim) == []


def test_the_database_refuses_a_forged_status_even_around_the_api(world):
    claim = add(world).claim_id
    with pytest.raises(IntegrityError), world["engine"].begin() as c:
        c.execute(
            text(
                """INSERT INTO claim_reviews (project_id, claim_id, status, reason, actor, at)
                   VALUES (:p, :c, 'source_located', 'r', 'Asha Rao', :t)"""
            ),
            {"p": world["scope"].project_id, "c": claim, "t": NOW},
        )


@pytest.mark.parametrize("decision", ["human_confirmed", "disputed", "rejected"])
def test_a_recheck_never_overwrites_a_persons_decision(world, decision):
    claim = add(world).claim_id
    review(world, claim, status=decision, reason="my decision", actor="Asha Rao")
    result = verify(world, claim)
    assert result.status == "source_located" and not result.written
    assert current(world, claim).status == decision
    assert len(reviews(world, claim)) == 1


def test_a_later_review_replaces_the_current_status_but_history_stays(world):
    claim = add(world).claim_id
    verify(world, claim)
    review(
        world,
        claim,
        status="disputed",
        reason="the source contradicts a second source",
        actor="Asha Rao",
    )
    assert current(world, claim).status == "disputed"
    assert [s for s, _, _ in reviews(world, claim)] == ["source_located", "disputed"]


def test_claims_cannot_be_updated_or_deleted_by_the_pipeline_role(world):
    """Immutability is enforced by privileges (migration test), and the API offers no edit at all."""
    import sie.evidence as ev

    assert not [n for n in dir(ev) if n.startswith(("update_", "edit_", "delete_"))]
