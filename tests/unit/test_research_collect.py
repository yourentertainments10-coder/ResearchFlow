"""Phase 4c: fetched pages become tables with provenance; nothing is invented or silently dropped."""

from __future__ import annotations

import json

import pytest

from research_fakes import NOW, RESULTS_CSV
from sie.datasets.provenance import sha256_bytes
from sie.evidence.errors import EvidenceError
from sie.evidence.web import FetchedPage
from sie.research.collect import NothingCollected, collect_page, coverage, find_conflicts

URL = "https://data.example.gov.uk/election/results"


def page(body, ctype="text/csv", url=URL):
    data = body.encode() if isinstance(body, str) else body
    return FetchedPage(url, url, 200, ctype, data, NOW, NOW)


def test_csv_becomes_one_table_with_row_level_provenance():
    (t,) = collect_page(page(RESULTS_CSV))
    assert t.rows == 4 and t.flags == () and t.label == "csv"
    prov = t.provenance_frame()
    assert list(prov.index) == [1, 2, 3, 4]  # the 1-based row ids of the source
    assert set(prov["source_url"]) == {URL} and set(prov["raw_sha256"]) == {
        sha256_bytes(RESULTS_CSV.encode())
    }
    assert set(prov["retrieved_at"]) == {NOW.isoformat()}
    assert t.dataset.source.kind == "fetched" and t.dataset.source.url == URL


def test_ragged_csv_rows_are_rejected_and_flagged_not_dropped_silently():
    (t,) = collect_page(page("a,b\n1,2\n3\n4,5\n"))
    assert t.rows == 2 and len(t.dataset.rejects) == 1
    assert "rejected_rows:1" in t.flags
    assert coverage(t)["rejected_rows"] == 1  # rows in the file = 3 = 2 kept + 1 rejected


def test_missing_values_are_counted_not_filled():
    (t,) = collect_page(page("party,seats\nA,10\nB,\n"))
    assert coverage(t)["missing_values_by_column"] == {"seats": 1}
    assert t.dataset.frame["seats"].isna().sum() == 1


def test_json_list_of_records():
    body = json.dumps([{"party": "A", "seats": 3}, {"party": "B", "seats": 4}])
    (t,) = collect_page(page(body, "application/json"))
    assert t.rows == 2 and t.label == "json" and t.flags == ()


def test_a_json_wrapper_with_one_list_is_unwrapped_and_flagged():
    body = json.dumps({"help": "x", "result": {"records": [{"party": "A", "seats": 3}]}})
    (t,) = collect_page(page(body, "application/json"))
    assert t.rows == 1 and any(f.startswith("unwrapped_json:$.result.records") for f in t.flags)
    assert t.raw_sha256 == sha256_bytes(body.encode())  # the page hash, not the derived bytes
    assert t.source_bytes != body.encode()


def test_an_ambiguous_json_wrapper_is_refused_not_guessed():
    body = json.dumps({"records": [{"a": 1}], "results": [{"b": 2}]})
    with pytest.raises(NothingCollected, match="cannot tell"):
        collect_page(page(body, "application/json"))


@pytest.mark.parametrize("body", ['{"a": 1}', "[1, 2]", '[{"a": {"nested": 1}}]', "not json", "[]"])
def test_unusable_json_is_refused_with_a_reason(body):
    with pytest.raises(NothingCollected):
        collect_page(page(body, "application/json"))


HTML = """<html><body><table><tr><td>layout only</td></tr></table>
<table><caption>Results</caption>
<tr><th>Party</th><th>Seats</th></tr>
<tr><td>Alpha</td><td>300</td></tr>
<tr><td>Beta</td><td></td></tr>
</table></body></html>"""


def test_an_html_table_is_read_and_a_refused_table_beside_it_is_disclosed():
    (t,) = collect_page(page(HTML, "text/html"))
    assert t.label == "table-1" and t.rows == 2
    assert list(t.dataset.frame.columns) == ["Party", "Seats"]
    assert any(f.startswith("table_refused:0:has no header row") for f in t.flags)
    assert coverage(t)["missing_values_by_column"] == {"Seats": 1}  # the empty cell stays missing


@pytest.mark.parametrize(
    ("html", "reason"),
    [
        (
            "<table><tr><th colspan='2'>x</th></tr><tr><td>1</td><td>2</td></tr></table>",
            "merged cells",
        ),
        ("<table><tr><th>a</th><th>a</th></tr><tr><td>1</td><td>2</td></tr></table>", "duplicate"),
        (
            "<table><tr><th>a</th><th></th></tr><tr><td>1</td><td>2</td></tr></table>",
            "empty column header",
        ),
        (
            "<table><tr><th>a</th><th>b</th></tr><tr><td>1</td></tr></table>",
            "different number of cells",
        ),
        (
            "<table><tr><td>a</td><td>b</td></tr><tr><td>1</td><td>2</td></tr></table>",
            "no header row",
        ),
    ],
)
def test_an_unreadable_html_table_is_refused_with_its_reason_never_repaired(html, reason):
    with pytest.raises(NothingCollected, match=reason):
        collect_page(page(html, "text/html"))


def test_html_without_a_usable_table_is_an_explicit_nothing():
    with pytest.raises(NothingCollected, match="no usable HTML table"):
        collect_page(page("<html><p>Results: Alpha won.</p></html>", "text/html"))


def test_unsupported_content_type_is_refused():
    with pytest.raises(NothingCollected, match="cannot be collected"):
        collect_page(page("x", "application/pdf"))


def test_plain_text_is_read_only_when_the_address_says_csv():
    (t,) = collect_page(page("a,b\n1,2\n", "text/plain", url="https://x.example.gov.uk/f.csv"))
    assert t.rows == 1
    with pytest.raises(NothingCollected):
        collect_page(page("a,b\n1,2\n", "text/plain", url="https://x.example.gov.uk/f.txt"))


def test_every_html_row_reaches_the_table():
    body = (
        "<table><tr><th>a</th><th>b</th></tr>"
        + "".join(f"<tr><td>{i}</td><td>{i}</td></tr>" for i in range(50))
        + "</table>"
    )
    (t,) = collect_page(page(body, "text/html"))
    assert t.rows == 50 and t.dataset.frame.index.tolist() == list(range(1, 51))


def conflict_tables():
    one = collect_page(
        page("party,seats\nAlpha,300\nBeta,150\n", url="https://a.example.gov.uk/x")
    )[0]
    two = collect_page(
        page("party,seats\nALPHA,300\nBeta,151\nGamma,9\n", url="https://b.example.gov.uk/y")
    )[0]
    return one, two


def test_conflicting_values_are_listed_with_both_sources_and_none_is_chosen():
    one, two = conflict_tables()
    conflicts = find_conflicts([one, two], ["party"], ["seats"])
    assert (
        len(conflicts) == 1
    )  # only Beta differs; keys compare as given, so ALPHA is a different key
    row = conflicts.iloc[0]
    assert {row["value_a"], row["value_b"]} == {"150", "151"}
    assert "a.example.gov.uk" in row["source_a"] and "b.example.gov.uk" in row["source_b"]


def test_agreeing_values_and_missing_values_are_not_conflicts():
    a = collect_page(page("k,v\nx,1\ny,\n", url="https://a.example.gov.uk/x"))[0]
    b = collect_page(page("k,v\nx,1\ny,2\n", url="https://b.example.gov.uk/y"))[0]
    assert len(find_conflicts([a, b], ["k"], ["v"])) == 0  # a missing value is not a rival value


def test_a_conflict_inside_one_table_is_found_too():
    t = collect_page(page("k,v\nx,1\nx,2\n"))[0]
    assert len(find_conflicts([t], ["k"], ["v"])) == 1


def test_conflict_detection_names_a_missing_column():
    t = collect_page(page("k,v\nx,1\n"))[0]
    with pytest.raises(EvidenceError, match="no column"):
        find_conflicts([t], ["k"], ["nope"])
