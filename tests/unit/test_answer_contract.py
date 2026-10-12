"""ADR-041: the answer contract, the number lock and the layer rules. Pure: no database, no network."""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest
from pydantic import ValidationError

from sie.analytics import metrics
from sie.answer import (
    Answer,
    AnswerRejected,
    EvidenceRef,
    Figure,
    FigureError,
    Gap,
    GapKind,
    Layer,
    Refusal,
    RefusalCode,
    SourceKind,
    Statement,
    answer_sha256,
    canonical_json,
    figure_from_cell,
    figure_from_value,
    format_figure,
    operation_provenance,
    render_markdown,
    to_decimal,
    verify,
)

ROWS = [
    ("IND", "India", "Archery", "Archery", 1, "Men", "Gold"),
    ("KOR", "Korea", "Archery", "Archery", 1, "Men", "Silver"),
    ("CHN", "China", "Archery", "Archery", 1, "Men", "Bronze"),
    ("IND", "India", "Archery", "Archery", 2, "Women", "Gold"),
    ("IND", "India", "Archery", "Archery", 2, "Women", "Silver"),
    ("CHN", "China", "Boxing", "Boxing", 3, "Men", "Gold"),
    ("KOR", "Korea", "Boxing", "Boxing", 3, "Men", "Bronze"),
]
COLS = ["country_code", "country_name", "sport", "discipline_name", "event_id", "gender", "medal"]
SUMMARY = metrics.country_summary(pd.DataFrame(ROWS, columns=COLS))  # the real sports analytics
RETRIEVED = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)


def evidence(eid="E1"):
    return EvidenceRef(
        id=eid, url="https://example.org/report", title="A federation statement",
        retrieved_at=RETRIEVED, tier=2, quote="short excerpt",
    )  # fmt: skip


def good() -> Answer:
    """A full answer built from a real analytics table (no number typed by hand)."""
    ind_pts = figure_from_cell(
        "ind_points", SUMMARY, where={"country_code": "IND"}, column="Points",
        label="India points", source_kind=SourceKind.OPERATION, source_ref="abc123",
    )  # fmt: skip
    ind_gold = figure_from_cell(
        "ind_gold", SUMMARY, where={"country_code": "IND"}, column="Gold",
        label="India gold medals", source_kind=SourceKind.DATABASE, source_ref="q:country_gold",
    )  # fmt: skip
    share = figure_from_value(
        "ind_share", Decimal("0.5"), label="share", source_kind=SourceKind.OPERATION,
        source_ref="abc123", decimals=1, unit="%",
    )  # fmt: skip
    ext = figure_from_value(
        "ext_total", 12, label="reported total", source_kind=SourceKind.EVIDENCE, source_ref="E1"
    )
    return Answer(
        question="How did India do?",
        figures=(ind_pts, ind_gold, share, ext),
        evidence=(evidence(),),
        gaps=(Gap(id="g1", kind=GapKind.PARTIAL_DATA, message="Only one sport is loaded."),),
        statements=(
            Statement(layer=Layer.VERIFIED_FACT, text="India won {F:ind_gold} gold medals."),
            Statement(layer=Layer.DERIVED_ANALYSIS, text="India scores {F:ind_points} points, {F:ind_share} of all."),
            Statement(layer=Layer.INTERPRETATION, text="With {F:ind_gold} golds India leads the table."),
            Statement(layer=Layer.EXTERNAL_EVIDENCE, text="A source reports {F:ext_total} in total.", evidence=("E1",)),
            Statement(layer=Layer.LIMITATION, text="The data covers a single sport only.", gaps=("g1",)),
        ),
    )  # fmt: skip


def rejected(answer, code):
    report = verify(answer)
    assert code in report.codes(), report.violations
    with pytest.raises(AnswerRejected):
        render_markdown(answer)


def with_statements(a: Answer, *extra: Statement, drop_limitation=False) -> Answer:
    kept = [s for s in a.statements if not (drop_limitation and s.layer is Layer.LIMITATION)]
    return a.model_copy(update={"statements": tuple(kept) + extra})


# --- a valid answer ------------------------------------------------------------------------------


def test_a_good_answer_verifies_and_renders_every_number_from_its_figures():
    a = good()
    assert verify(a).ok
    out = render_markdown(a)
    assert "India won 2 gold medals." in out
    assert "India scores 8 points, 0.5% of all." in out
    assert "A source reports 12 in total. [1]" in out
    assert "## External evidence (cited, not verified)" in out
    assert (
        "[1] A federation statement, https://example.org/report (retrieved 2026-10-11, source tier 2)"
        in out
    )
    assert out.index("## Verified facts") < out.index("## Limitations")


def test_the_figures_come_from_the_real_analytics_table():
    assert SUMMARY.set_index("country_code").loc["IND", "Points"] == 8
    fig = next(f for f in good().figures if f.id == "ind_points")
    assert fig.value == Decimal(8)


# --- the number lock -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "typed",
    ["India won 3 medals.", "India won 2.5 medals.", "Total 1,568 medals.", "India won three medals.",
     "India finished first.", "A half of them.", "Dozens", "India won ٣ medals.", "India won ३ medals.",
     "India won ½ of them.", "Medal count²", "Ⅳ medals", "三 medals", "about twenty-one"],
)  # fmt: skip
def test_a_typed_number_is_rejected_in_any_form(typed):
    a = with_statements(good(), Statement(layer=Layer.LIMITATION, text=typed), drop_limitation=True)
    if typed == "Dozens":  # "Dozens" is not the word "dozen": not a number, so no literal_number
        assert "literal_number" not in verify(a).codes()
    else:
        rejected(a, "literal_number")


def test_replacing_any_placeholder_by_its_rendered_number_breaks_the_lock():
    a = good()
    figures = {f.id: f for f in a.figures}
    for i, s in enumerate(a.statements):
        for fid, fig in figures.items():
            token = "{F:" + fid + "}"
            if token not in s.text:
                continue
            typed = s.model_copy(update={"text": s.text.replace(token, format_figure(fig))})
            stmts = list(a.statements)
            stmts[i] = typed
            broken = a.model_copy(update={"statements": tuple(stmts)})
            assert "literal_number" in verify(broken).codes(), (i, fid)


def test_a_declared_literal_is_allowed_and_must_really_be_in_the_text():
    ok = with_statements(
        good(), Statement(layer=Layer.LIMITATION, text="Data is from 2026 only.", declared_literals=("2026",))
    )  # fmt: skip
    assert verify(ok).ok
    assert '"declared_literals":["2026"]' in canonical_json(ok)  # visible to an audit
    absent = with_statements(
        good(), Statement(layer=Layer.LIMITATION, text="Data is partial.", declared_literals=("2026",))
    )  # fmt: skip
    rejected(absent, "declared_literal_absent")
    only_one = with_statements(
        good(), Statement(layer=Layer.LIMITATION, text="From 2026 to 2027.", declared_literals=("2026",))
    )  # fmt: skip
    rejected(only_one, "literal_number")


def test_unknown_figure_and_stray_braces_are_rejected():
    rejected(
        with_statements(good(), Statement(layer=Layer.LIMITATION, text="See {F:nope}.")),
        "unknown_figure",
    )
    rejected(
        with_statements(good(), Statement(layer=Layer.LIMITATION, text="Odd {x} text.")),
        "bad_placeholder",
    )
    rejected(
        with_statements(good(), Statement(layer=Layer.LIMITATION, text="Odd {F:ind_gold")),
        "bad_placeholder",
    )


# --- layer rules ---------------------------------------------------------------------------------


def test_each_layer_may_only_cite_its_own_kinds_of_figures():
    a = good()
    swap = lambda text, layer: with_statements(a, Statement(layer=layer, text=text))  # noqa: E731
    rejected(swap("India has {F:ind_points} points.", Layer.VERIFIED_FACT), "layer_source_mismatch")
    rejected(swap("India has {F:ind_gold} golds.", Layer.DERIVED_ANALYSIS), "layer_source_mismatch")
    rejected(swap("Web says {F:ind_gold}.", Layer.EXTERNAL_EVIDENCE), "layer_source_mismatch")
    rejected(swap("India has {F:ext_total} total.", Layer.VERIFIED_FACT), "layer_source_mismatch")


def test_facts_analysis_and_interpretation_need_a_figure_and_evidence_needs_a_source():
    a = good()
    for layer in (Layer.VERIFIED_FACT, Layer.DERIVED_ANALYSIS, Layer.INTERPRETATION):
        rejected(with_statements(a, Statement(layer=layer, text="India leads.")), "no_basis")
    rejected(
        with_statements(a, Statement(layer=Layer.EXTERNAL_EVIDENCE, text="Reported.")), "no_basis"
    )
    rejected(
        with_statements(
            a, Statement(layer=Layer.VERIFIED_FACT, text="Gold {F:ind_gold}.", evidence=("E1",))
        ),
        "evidence_outside_layer",
    )
    rejected(
        with_statements(
            a, Statement(layer=Layer.EXTERNAL_EVIDENCE, text="X {F:ext_total}.", evidence=("E9",))
        ),
        "unknown_evidence",
    )


def test_a_gap_must_be_disclosed_and_every_answer_states_a_limitation():
    a = good()
    undisclosed = a.model_copy(
        update={"statements": tuple(s for s in a.statements if s.layer is not Layer.LIMITATION)}
    )
    report = verify(undisclosed)
    assert {"gap_not_disclosed", "no_limitations"} <= report.codes()
    no_gap_ref = with_statements(a, drop_limitation=True)
    no_gap_ref = with_statements(
        no_gap_ref, Statement(layer=Layer.LIMITATION, text="Only partial.")
    )
    rejected(no_gap_ref, "gap_not_disclosed")


def test_duplicate_ids_unused_figures_and_missing_evidence_are_rejected():
    a = good()
    rejected(a.model_copy(update={"figures": a.figures + (a.figures[0],)}), "duplicate_id")
    extra = figure_from_value(
        "spare", 1, label="spare", source_kind=SourceKind.DATABASE, source_ref="q"
    )
    rejected(a.model_copy(update={"figures": a.figures + (extra,)}), "unused_figure")
    orphan = figure_from_value(
        "orph", 1, label="o", source_kind=SourceKind.EVIDENCE, source_ref="E7"
    )
    rejected(
        with_statements(
            a.model_copy(update={"figures": a.figures + (orphan,)}),
            Statement(layer=Layer.EXTERNAL_EVIDENCE, text="Seen {F:orph}.", evidence=("E1",)),
        ),
        "figure_evidence_missing",
    )


def test_a_refusal_is_a_result_and_obeys_the_number_lock():
    r = Refusal(
        question="Who will win?",
        code=RefusalCode.INSUFFICIENT_DATA,
        reason="No verified data covers that.",
    )
    assert verify(r).ok
    assert render_markdown(r).startswith("**Cannot answer** (insufficient_data)")
    bad = Refusal(question="q", code=RefusalCode.NO_SOURCE, reason="Needs 2027 data.")
    rejected(bad, "literal_number")


# --- determinism ---------------------------------------------------------------------------------


def test_the_same_answer_gives_the_same_json_hash_and_markdown():
    assert canonical_json(good()) == canonical_json(good())
    assert answer_sha256(good()) == answer_sha256(good())
    assert render_markdown(good()) == render_markdown(good())
    assert canonical_json(good()).startswith('{"evidence":[')  # sorted keys, no spaces


def _fig(value):
    return figure_from_value("x", value, label="x", source_kind=SourceKind.DATABASE, source_ref="q")


def test_equal_numbers_written_differently_hash_the_same_and_a_changed_number_does_not():
    assert _fig("1.50").model_dump(mode="json") == _fig(Decimal("1.5")).model_dump(mode="json")
    assert _fig("100").model_dump(mode="json")["value"] == "100"
    a = good()
    first = a.figures[0].model_copy(update={"value": Decimal(9)})
    changed = a.model_copy(update={"figures": (first, *a.figures[1:])})
    assert answer_sha256(changed) != answer_sha256(a)


def test_a_naive_timestamp_is_refused():
    with pytest.raises(ValidationError):
        EvidenceRef(
            id="E1", url="https://x.org/a", title="t", retrieved_at=datetime(2026, 1, 1), tier=1
        )


# --- numbers and adapters ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "decimals", "unit", "shown"),
    [
        ("1568", 0, "", "1,568"), ("2.345", 2, "", "2.35"), ("2.5", 0, "", "3"), ("-2.5", 0, "", "-3"),
        ("-0.001", 2, "", "0.00"), ("12.3", 1, "%", "12.3%"), ("4", 0, "medals", "4 medals"),
        ("1234567.891", 1, "", "1,234,567.9"),
    ],
)  # fmt: skip
def test_formatting_is_fixed_and_half_up(value, decimals, unit, shown):
    fig = Figure(
        id="x",
        value=value,
        decimals=decimals,
        unit=unit,
        label="x",
        source_kind=SourceKind.DATABASE,
        source_ref="q",
    )
    assert format_figure(fig) == shown


def test_a_float_a_bool_or_a_non_number_is_not_a_figure():
    for bad in (1.5, True, "abc", float("nan")):
        with pytest.raises(ValidationError):
            Figure(id="x", value=bad, label="x", source_kind=SourceKind.DATABASE, source_ref="q")
    for bad in (float("nan"), float("inf"), None, "n/a"):
        with pytest.raises(FigureError):
            to_decimal(bad)
    assert to_decimal(0.1 + 0.2) == Decimal("0.3")  # 15 significant digits, as sie.datasets
    assert to_decimal(SUMMARY["Points"].iloc[0]) == Decimal(
        int(SUMMARY["Points"].iloc[0])
    )  # numpy scalar


def test_a_cell_lookup_needs_exactly_one_row():
    kw = {"label": "x", "source_kind": SourceKind.OPERATION, "source_ref": "r"}
    with pytest.raises(FigureError, match="0 rows"):
        figure_from_cell("a", SUMMARY, where={"country_code": "XXX"}, column="Gold", **kw)
    doubled = pd.concat([SUMMARY, SUMMARY])
    with pytest.raises(FigureError, match="2 rows"):
        figure_from_cell("a", doubled, where={"country_code": "IND"}, column="Gold", **kw)
    with pytest.raises(FigureError):
        figure_from_cell("a", SUMMARY, where={}, column="Gold", **kw)


def test_operation_provenance_reads_a_datasets_style_result():
    @dataclass
    class P:  # the fields sie.datasets.Provenance has (docs/DATASET_ANALYTICS_DESIGN.md)
        operation: str = "group_aggregate"
        parameters: dict = field(default_factory=lambda: {"by": "country", "agg": "sum"})
        input_sha256: str = "in1"
        result_sha256: str = "res1"

    @dataclass
    class R:
        provenance: P = field(default_factory=P)

    ref, prov = operation_provenance(R())
    assert (
        ref == "res1"
        and prov["parameters"] == "agg=sum,by=country"
        and prov["operation"] == "group_aggregate"
    )
    with pytest.raises(FigureError):
        operation_provenance(R(P(result_sha256="")))


# --- isolation (regression safeguard) ------------------------------------------------------------

SRC = Path(__file__).resolve().parents[2] / "src" / "sie"
SPORTS = ("pipeline", "db", "analytics", "sources", "ops", "web")


def _sie_imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
        elif isinstance(node, ast.Import):
            found |= {n.name for n in node.names}
    return {m for m in found if m == "sie" or m.startswith("sie.")}


def test_the_answer_package_imports_nothing_from_the_sports_or_database_code():
    allowed = {"sie.answer", "sie.answer.models", "sie.answer.numbers", "sie.answer.verify",
               "sie.answer.render", "sie.answer.adapters"}  # fmt: skip
    for path in (SRC / "answer").glob("*.py"):
        assert _sie_imports(path) <= allowed, (path.name, _sie_imports(path) - allowed)


def test_no_existing_module_imports_the_answer_package_yet():
    users = []
    for path in SRC.rglob("*.py"):
        if "answer" in path.relative_to(SRC).parts[:1]:
            continue
        if any(m == "sie.answer" or m.startswith("sie.answer.") for m in _sie_imports(path)):
            users.append(str(path.relative_to(SRC)))
    assert users == [], (
        "Phase 1 must not change existing behaviour; adopt the package in a later phase"
    )


def test_the_answer_package_has_no_new_dependency():
    for path in (SRC / "answer").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names = []
            if isinstance(node, ast.Import):
                names = [n.name.split(".")[0] for n in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module.split(".")[0]]
            for n in names:
                assert n in {"sie", "pydantic", "__future__", "re", "json", "hashlib", "math",
                             "unicodedata", "decimal", "datetime", "enum", "typing", "dataclasses",
                             "collections"}, (path.name, n)  # fmt: skip
