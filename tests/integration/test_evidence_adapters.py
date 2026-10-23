"""Evidence to answers: what may become a Figure, what must be disclosed, and what number-lock is not.

The number lock (sie.answer) checks that every number in an answer came from a declared figure. It never
checks the figure is true. These tests pin what the ledger adds on top, and that it stays honest about it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text

from sie.answer import (
    Answer,
    AnswerRejected,
    Layer,
    SourceKind,
    Statement,
    assert_verified,
    render_markdown,
    verify,
)
from sie.evidence import (
    ClaimInput,
    ClaimNotUsable,
    EvidenceError,
    NotFound,
    add_claim,
    assert_single_project,
    create_project,
    detect_conflicts,
    evidence_ref_from_source,
    figure_from_claim,
    ingest_upload,
    list_conflicts,
    open_scope,
    resolve_conflict,
    review_claim,
    verify_claim,
)
from sie.evidence import conflicts as conflicts_module

NOW = datetime(2026, 10, 12, 10, 0, tzinfo=UTC)
CSV = b"country,gold\nIndia,6\nJapan,8\n"


@pytest.fixture()
def world(engine):
    with engine.begin() as c:
        create_project(c, slug="a", name="A", now=NOW)
    with engine.connect() as c:
        scope = open_scope(c, "a", for_write=True)
    upload = ingest_upload(engine, scope, CSV, "m.csv", now=NOW)
    with engine.begin() as c:
        web = c.execute(
            text(
                """INSERT INTO evidence_sources (project_id, kind, retention_mode, content_sha256, size_bytes,
                       title, requested_url, final_url, tier, retrieved_at, created_at)
                   VALUES (:p, 'web', 'excerpt', :h, 3, 'Games results', 'https://e.org/a',
                           'https://e.org/final', 2, :t, :t) RETURNING id"""
            ),
            {"p": scope.project_id, "h": "a" * 64, "t": NOW},
        ).scalar_one()
    return {"engine": engine, "scope": scope, "upload": upload.source_id, "web": web}


def claim(world, source, value="6", **over):
    base = {
        "subject": "India",
        "metric": "gold medals",
        "value_text": value,
        "excerpt": value,
        "locator_kind": "table_cell",
        "locator": {"row_id": 1, "column": "gold", "delimiter": ","},
        "extraction_method": "manual",
        "extractor_version": "t",
        "value_numeric": Decimal(value) if value.replace(".", "").isdigit() else None,
    }
    base.update(over)
    with world["engine"].begin() as c:
        return add_claim(c, world["scope"], world[source], ClaimInput(**base), now=NOW).claim_id


def review(world, claim_id, status, actor="Asha Rao"):
    with world["engine"].begin() as c:
        review_claim(c, world["scope"], claim_id, status=status, reason="r", actor=actor, now=NOW)


def figure(world, claim_id, fid="f1"):
    with world["engine"].connect() as c:
        return figure_from_claim(c, world["scope"], claim_id, figure_id=fid, label="Gold medals")


LIMIT_TEXT = "This rests on a single source and was not independently checked."


def answer_for(cf, layer, text_="The source reports {F:f1} gold medals.", disclose=True):
    """An answer citing the figure; the contract wants each gap listed by a limitation statement."""
    return Answer(
        question="How many gold medals?",
        figures=(cf.figure,),
        evidence=(cf.evidence,) if cf.evidence else (),
        gaps=cf.gaps,
        statements=(
            Statement(layer=layer, text=text_, evidence=(cf.evidence.id,) if cf.evidence else ()),
            Statement(
                layer=Layer.LIMITATION,
                text=LIMIT_TEXT,
                gaps=tuple(g.id for g in cf.gaps) if disclose else (),
            ),
        ),
    )


# ---- what may become a figure ------------------------------------------------------------------


def test_a_person_confirmed_web_claim_becomes_an_evidence_figure_with_full_provenance(world):
    cid = claim(world, "web")
    review(world, cid, "human_confirmed")
    cf = figure(world, cid)
    assert cf.figure.source_kind is SourceKind.EVIDENCE
    assert (
        cf.figure.source_ref == cf.evidence.id
    )  # the contract needs an evidence figure to name its ref
    assert cf.figure.provenance["claim_id"] == str(cid)
    prov = cf.figure.provenance
    assert (
        prov["project_id"] == str(world["scope"].project_id) and prov["status"] == "human_confirmed"
    )
    assert prov["attestation"] == "as stated in the source; not independently verified"
    assert len(prov["claim_hash"]) == 64 and prov["source_sha256"] == "a" * 64
    ref = cf.evidence
    assert (ref.url, ref.title, ref.tier) == (
        "https://e.org/final",
        "Games results",
        2,
    )  # final URL
    assert ref.retrieved_at == NOW and ref.quote == "6"
    assert any(
        "bytes are not stored" in line for line in cf.limitations
    )  # excerpt-only is disclosed


def test_an_evidence_answer_verifies_and_renders_but_only_as_external_evidence(world):
    cid = claim(world, "web")
    review(world, cid, "human_confirmed")
    cf = figure(world, cid)
    answer = answer_for(cf, Layer.EXTERNAL_EVIDENCE)
    assert verify(answer).ok
    assert "6" in render_markdown(answer)
    # The same figure shown as a verified fact is refused by the contract: evidence is never "verified".
    bad = answer_for(cf, Layer.VERIFIED_FACT)
    assert "layer_source_mismatch" in verify(bad).codes()
    with pytest.raises(AnswerRejected):
        render_markdown(bad)


def test_a_source_located_upload_claim_is_a_user_file_figure_not_a_database_fact(world):
    cid = claim(world, "upload")
    with world["engine"].begin() as c:
        assert verify_claim(c, world["scope"], cid, now=NOW).status == "source_located"
    cf = figure(world, cid)
    assert cf.figure.source_kind is SourceKind.USER_FILE and cf.evidence is None
    assert verify(answer_for(cf, Layer.DERIVED_ANALYSIS)).ok
    assert verify(answer_for(cf, Layer.VERIFIED_FACT)).codes() == {"layer_source_mismatch"}
    with pytest.raises(EvidenceError, match="user_file"), world["engine"].connect() as c:
        evidence_ref_from_source(c, world["scope"], world["upload"])


def test_number_lock_is_still_not_factual_verification(world):
    """A confirmed-but-wrong figure sails through the lock: the lock proves provenance, not truth."""
    cid = claim(world, "web", value="999")  # a nonsense medal count a person confirmed
    review(world, cid, "human_confirmed")
    answer = answer_for(figure(world, cid), Layer.EXTERNAL_EVIDENCE)
    assert verify(answer).ok  # honest: this is what the lock does and does not do
    assert "as stated in the source" in answer.figures[0].provenance["attestation"]
    typed = Answer(  # but a number typed by hand is still rejected
        question="q", figures=answer.figures, evidence=answer.evidence,
        statements=(Statement(layer=Layer.EXTERNAL_EVIDENCE, text="It says 999 medals.",
                              evidence=(answer.evidence[0].id,)),
                    Statement(layer=Layer.LIMITATION, text=LIMIT_TEXT)),
    )  # fmt: skip
    assert "literal_number" in verify(typed).codes()


@pytest.mark.parametrize(
    ("setup", "code"),
    [
        (None, "not_confirmed"),  # unverified
        ("disputed", "disputed"),
        ("rejected", "rejected"),
    ],
)
def test_unconfirmed_disputed_and_rejected_claims_cannot_become_figures(world, setup, code):
    cid = claim(world, "web")
    if setup:
        review(world, cid, setup)
    with pytest.raises(ClaimNotUsable) as err:
        figure(world, cid)
    assert err.value.code == code


def test_a_claim_confirmed_then_disputed_stops_being_usable(world):
    cid = claim(world, "web")
    review(world, cid, "human_confirmed")
    assert figure(world, cid).figure.value == Decimal("6")
    review(world, cid, "disputed")
    with pytest.raises(ClaimNotUsable):
        figure(world, cid)


def test_a_non_numeric_claim_has_no_figure(world):
    cid = claim(world, "web", value="six", value_numeric=None)
    review(world, cid, "human_confirmed")
    with pytest.raises(ClaimNotUsable) as err:
        figure(world, cid)
    assert err.value.code == "no_numeric_value"


def test_a_cached_formula_value_carries_a_gap_the_answer_must_disclose(world, engine):
    x = ingest_upload(engine, world["scope"], b"x", "w.xlsx", now=NOW)
    world["x"] = x.source_id
    cid = claim(world, "x", value="9", locator_kind="xlsx_cell", value_source="cached_formula_value",
                locator={"sheet": "S", "cell": "A1"})  # fmt: skip
    review(world, cid, "human_confirmed")
    cf = figure(world, cid)
    assert [g.kind.value for g in cf.gaps] == ["unverified"]
    assert "formulas were not evaluated" in cf.gaps[0].message
    assert verify(answer_for(cf, Layer.DERIVED_ANALYSIS)).ok
    assert (
        "gap_not_disclosed"
        in verify(answer_for(cf, Layer.DERIVED_ANALYSIS, disclose=False)).codes()
    )


# ---- conflicts ---------------------------------------------------------------------------------


def two_conflicting(world):
    a = claim(world, "web")  # India gold 6
    b = claim(world, "upload", value="7", locator={"row_id": 2, "column": "gold", "delimiter": ","})
    return a, b


def test_disagreeing_claims_become_one_open_conflict_and_detection_is_idempotent(world):
    a, b = two_conflicting(world)
    with world["engine"].begin() as c:
        first = detect_conflicts(c, world["scope"], now=NOW)
        second = detect_conflicts(c, world["scope"], now=NOW)
        found = list_conflicts(c, world["scope"], "open")
    assert len(first) == 1 and second == []
    assert (found[0].claim_a_id, found[0].claim_b_id, found[0].kind) == (a, b, "value_differs")


def test_agreeing_rejected_or_differently_scoped_claims_are_not_conflicts(world):
    claim(world, "web")
    claim(
        world,
        "upload",
        value="6.0",
        locator={"row_id": 1, "column": "gold", "delimiter": ",", "number_format": {}},
    )
    rejected = claim(
        world, "upload", value="7", locator={"row_id": 2, "column": "gold", "delimiter": ","}
    )
    review(world, rejected, "rejected")
    claim(
        world,
        "upload",
        value="8",
        period="2022",
        locator={"row_id": 2, "column": "country", "delimiter": ","},
    )
    with world["engine"].begin() as c:
        assert detect_conflicts(c, world["scope"], now=NOW) == []


def test_an_open_conflict_forces_a_gap_and_the_contract_enforces_its_disclosure(world):
    a, b = two_conflicting(world)
    review(world, a, "human_confirmed")
    with world["engine"].begin() as c:
        detect_conflicts(c, world["scope"], now=NOW)
    cf = figure(world, a)
    assert [g.kind.value for g in cf.gaps] == ["conflict"]
    assert cf.gaps[0].id.startswith("gap_conflict_")
    assert verify(answer_for(cf, Layer.EXTERNAL_EVIDENCE)).ok
    hidden = answer_for(cf, Layer.EXTERNAL_EVIDENCE, disclose=False)
    assert "gap_not_disclosed" in verify(hidden).codes()
    with pytest.raises(AnswerRejected):
        assert_verified(hidden)
    with pytest.raises(AnswerRejected):
        render_markdown(hidden)  # an answer that hides the conflict never renders


def test_resolution_needs_a_named_person_and_a_note_and_works_once(world):
    two_conflicting(world)
    with world["engine"].begin() as c:
        (cid,) = detect_conflicts(c, world["scope"], now=NOW)

    def resolve(**kw):
        kw = {
            "status": "explained",
            "note": "different tables",
            "resolved_by": "Asha Rao",
            "now": NOW,
            **kw,
        }
        with world["engine"].begin() as c:
            return resolve_conflict(c, world["scope"], cid, **kw)

    for bad in (
        {"note": " "},
        {"resolved_by": "system:x"},
        {"status": "open"},
        {"status": "magic"},
    ):
        with pytest.raises(EvidenceError):
            resolve(**bad)
    done = resolve()
    assert (done.status, done.resolved_by, done.note) == (
        "explained",
        "Asha Rao",
        "different tables",
    )
    with pytest.raises(NotFound):
        resolve()  # already resolved


def test_explained_conflicts_keep_both_claims_usable_and_gap_free(world):
    a, b = two_conflicting(world)
    for cid in (a, b):
        review(world, cid, "human_confirmed")
    with world["engine"].begin() as c:
        (conflict,) = detect_conflicts(c, world["scope"], now=NOW)
        resolve_conflict(
            c, world["scope"], conflict, status="explained", note="n", resolved_by="Asha", now=NOW
        )
    assert figure(world, a).gaps == () and figure(world, b).gaps == ()


def test_a_resolved_conflict_refuses_the_losing_claim_and_nothing_else(world):
    a, b = two_conflicting(world)
    for cid in (a, b):
        review(world, cid, "human_confirmed")
    with world["engine"].begin() as c:
        (conflict,) = detect_conflicts(c, world["scope"], now=NOW)
        resolve_conflict(
            c,
            world["scope"],
            conflict,
            status="resolved_a",
            note="a is the table",
            resolved_by="Asha",
            now=NOW,
        )
    assert figure(world, a).gaps == ()
    with pytest.raises(ClaimNotUsable) as err:
        figure(world, b)
    assert err.value.code == "lost_conflict"
    with world["engine"].connect() as c:  # the claims themselves were never edited
        assert c.execute(text("SELECT count(*) FROM claims")).scalar_one() == 2


def test_a_huge_group_is_refused_instead_of_trusted(world, monkeypatch):
    monkeypatch.setattr(conflicts_module, "MAX_GROUP", 2)
    for i in range(3):
        claim(
            world,
            "upload",
            value=str(i),
            locator={
                "row_id": 1,
                "column": "gold",
                "delimiter": ",",
                "number_format": {"decimal_separator": "." if i else "."},
            }
            if i
            else {"row_id": 1, "column": "gold", "delimiter": ","},
        )
    with world["engine"].begin() as c, pytest.raises(EvidenceError, match="review them"):
        detect_conflicts(c, world["scope"], now=NOW)


# ---- one project per answer --------------------------------------------------------------------


def test_an_answer_cannot_mix_evidence_from_two_projects(world, engine):
    cid = claim(world, "web")
    review(world, cid, "human_confirmed")
    mine = figure(world, cid)
    with engine.begin() as c:
        create_project(c, slug="b", name="B", now=NOW)
    with engine.connect() as c:
        other_scope = open_scope(c, "b", for_write=True)
    other_upload = ingest_upload(engine, other_scope, CSV, "m.csv", now=NOW)
    with engine.begin() as c:
        oc = add_claim(
            c, other_scope, other_upload.source_id,
            ClaimInput(subject="x", metric="m", value_text="1", excerpt="1", locator_kind="none",
                       locator={}, extraction_method="manual", extractor_version="t",
                       value_numeric=Decimal("1")),
            now=NOW,
        ).claim_id  # fmt: skip
        review_claim(c, other_scope, oc, status="human_confirmed", reason="r", actor="Bo", now=NOW)
    with engine.connect() as c:
        theirs = figure_from_claim(c, other_scope, oc, figure_id="f2", label="Other")
    mixed = Answer(
        question="q",
        figures=(mine.figure, theirs.figure),
        evidence=(mine.evidence,),
        statements=(
            Statement(
                layer=Layer.EXTERNAL_EVIDENCE,
                text="{F:f1} and {F:f2}",
                evidence=(mine.evidence.id,),
            ),
            Statement(layer=Layer.LIMITATION, text="Neither source was independently checked."),
        ),
    )
    with pytest.raises(EvidenceError, match="different projects"):
        assert_single_project(mixed)
    assert (
        assert_single_project(answer_for(mine, Layer.EXTERNAL_EVIDENCE))
        == world["scope"].project_id
    )
