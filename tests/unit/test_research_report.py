"""Analysis, the evidence-linked answer, coverage disclosure and the chart. Numbers come from operations."""

from __future__ import annotations

from datetime import timedelta

import pandas as pd
import pytest

from research_fakes import NOW, RESULTS_CSV
from sie.answer import verify
from sie.evidence.errors import EvidenceError
from sie.evidence.web import FetchedPage
from sie.research.collect import collect_page, find_conflicts
from sie.research.report import AnalysisItem, analyze, build_report, plan_from_dict, svg_bar_chart

URL = "https://data.example.gov.uk/election/results.csv"
PLAN = plan_from_dict({"parse_number": [{"column": "seats"}]})
SPEC = {"op": "group_aggregate", "by": ["party"], "metrics": ["seats:sum"], "sort": "seats:sum"}


def table(body=RESULTS_CSV, url=URL):
    return collect_page(FetchedPage(url, url, 200, "text/csv", body.encode(), NOW, NOW))[0]


def report(body=RESULTS_CSV, **kw):
    t = table(body)
    result = analyze(t, SPEC, PLAN)
    item = AnalysisItem("seats by party", result, "party", "seats:sum")
    return build_report("How many seats did each party win?", [t], [item], now=NOW, **kw)


def test_the_answer_passes_the_number_lock_and_figures_match_the_arithmetic():
    r = report()
    assert verify(r.answer).ok
    values = {f.label.split(" for ")[1]: str(f.value) for f in r.answer.figures}
    assert values == {"Alpha": "310", "Beta": "150", "Gamma": "90"}  # 300 + 10, hand-added
    assert "Alpha: 310" in r.markdown and "Beta: 150" in r.markdown


def test_provenance_and_sources_are_listed_with_url_time_and_hash():
    r = report()
    assert URL in r.markdown and "retrieved 2026-10-24" in r.markdown
    assert r.coverage[0]["raw_sha256"] in r.markdown
    assert r.answer.evidence[0].url == URL and r.answer.evidence[0].tier == 4


def test_a_complete_table_has_only_the_standing_limitation_and_no_false_warning():
    r = report()
    assert [g.kind.value for g in r.answer.gaps] == []
    assert "free public sources" in r.markdown


def test_missing_and_rejected_rows_are_disclosed_as_gaps():
    r = report("party,seats,votes\nAlpha,300,1\nBeta,,2\nGamma\nDelta,5,3\n")
    kinds = {g.kind.value for g in r.answer.gaps}
    assert {"missing_data", "excluded_rows", "partial_data"} <= kinds
    assert "1 rows of csv" in r.markdown and "seats (1 rows)" in r.markdown


def test_conflicts_stale_cache_and_failed_providers_become_gaps():
    a, b = table(), table("party,seats\nAlpha,299\n", "https://b.example.gov.uk/x.csv")
    conflicts = find_conflicts([a, b], ["party"], ["seats"])
    # raw text compare: "300" vs "299" differ for Alpha (the Alpha rows 300 and 10 also differ in a)
    r = build_report(
        "q", [a], [AnalysisItem("seats", analyze(a, SPEC, PLAN), "party", "seats:sum")], now=NOW,
        conflicts=conflicts, stale_urls=[URL], provider_notes=["data.gov.uk: quota_exhausted"],
    )  # fmt: skip
    kinds = {g.kind.value for g in r.answer.gaps}
    assert {"conflict", "stale", "missing_data"} <= kinds and verify(r.answer).ok
    assert "## Conflicts (none resolved)" in r.markdown


def test_digits_in_source_names_are_declared_labels_not_results():
    r = report("party,seats\nLok Sabha 2019,5\n")
    assert verify(r.answer).ok and "Lok Sabha 2019: 5" in r.markdown


def test_a_non_numeric_value_column_is_refused_not_guessed():
    t = table()
    result = analyze(t, SPEC, PLAN)
    with pytest.raises(Exception):  # noqa: B017  FigureError from the answer contract
        build_report("q", [t], [AnalysisItem("x", result, "party", "party")], now=NOW)


def test_the_same_inputs_give_the_same_report_bytes():
    assert report().markdown == report().markdown
    assert report().svg == report().svg


def test_a_report_needs_tables_and_an_analysis():
    with pytest.raises(EvidenceError):
        build_report("q", [], [], now=NOW)


def test_svg_chart_is_escaped_deterministic_and_bounded():
    labels = [f"<b>{i}</b>&" for i in range(30)]
    svg = svg_bar_chart('T"<x>', labels, [float(i + 1) for i in range(30)])
    assert "<b>" not in svg and "&lt;b&gt;" in svg and svg.count("<rect") == 15
    assert svg == svg_bar_chart('T"<x>', labels, [float(i + 1) for i in range(30)])
    with pytest.raises(EvidenceError):
        svg_bar_chart("t", [], [])


def test_an_invalid_cleaning_plan_is_a_clear_error():
    with pytest.raises(EvidenceError, match="invalid cleaning plan"):
        plan_from_dict({"parse_number": [{"nope": 1}]})


def test_follow_up_operations_reuse_the_same_table_without_new_data():
    t = table()
    first = analyze(t, SPEC, PLAN)
    second = analyze(t, {"op": "top_n", "by": "party", "metric": "seats:sum", "n": 1}, PLAN)
    assert (
        first.provenance.input_sha256 == second.provenance.input_sha256 == t.dataset.source.sha256
    )
    assert str(second.table.iloc[0]["party"]) == "Alpha"


def test_age_of_the_data_is_stated():
    r = report()
    assert (NOW - timedelta(0)).strftime("%Y-%m-%d %H:%M UTC") in r.markdown
    assert isinstance(r.coverage[0]["rows"], int) and pd is not None
