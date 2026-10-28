"""Phase 4c: fetched bytes become a table with provenance; nothing is guessed or filled in."""

from __future__ import annotations

import hashlib

import pytest

from sie.evidence import EvidenceError
from sie.evidence.collect import AmbiguousTable, NoTable, collect, html_tables, row_provenance

URL = "https://results.example.gov.in/files/2019/lok-sabha.html"
WHEN = "2026-10-24T10:00:00+00:00"

PAGE = b"""<html><body><script>var t = '<table>';</script>
<table><caption>Seats by party, 2019</caption>
<tr><th>Party</th><th>Seats</th><th>Vote share</th></tr>
<tr><td>BJP</td><td>303</td><td>37.4%</td></tr>
<tr><td>INC</td><td>52</td><td>19.5%</td></tr>
<tr><td>Other<br>parties</td><td></td><td>43.1%</td></tr>
</table></body></html>"""


def test_a_single_html_table_becomes_a_dataset_with_provenance():
    c = collect(PAGE, "text/html; charset=utf-8", url=URL, retrieved_at=WHEN)
    ds = c.dataset
    assert ds.columns == ["Party", "Seats", "Vote share"] and ds.rows == 3
    assert (
        ds.frame.loc[1, "Party"] == "BJP" and ds.frame.loc[1, "Seats"] == "303"
    )  # text exactly as shown
    assert ds.frame.loc[3, "Party"] == "Other parties"  # <br> becomes a space
    assert ds.frame.loc[3, "Seats"] is None  # an empty cell is missing, not zero
    assert (ds.source.kind, ds.source.url, ds.source.retrieved_at) == ("fetched", URL, WHEN)
    assert (
        c.page_sha256 == hashlib.sha256(PAGE).hexdigest()
        and c.table_caption == "Seats by party, 2019"
    )


def test_row_provenance_names_url_time_hash_table_and_row():
    c = collect(PAGE, "text/html", url=URL, retrieved_at=WHEN)
    assert row_provenance(c, 2) == {
        "url": URL, "retrieved_at": WHEN, "page_sha256": hashlib.sha256(PAGE).hexdigest(),
        "table_index": 0, "table_caption": "Seats by party, 2019", "row_id": 2,
    }  # fmt: skip
    with pytest.raises(EvidenceError):
        row_provenance(c, 99)


def test_script_text_is_not_read_as_a_table():
    assert len(html_tables(PAGE)) == 1


TWO = b"""<table><tr><th>A</th></tr><tr><td>1</td></tr></table>
<table><caption>Second</caption><tr><th>B</th></tr><tr><td>2</td></tr></table>"""


def test_two_usable_tables_require_a_choice_and_never_a_guess():
    with pytest.raises(AmbiguousTable) as e:
        collect(TWO, "text/html")
    assert [t["index"] for t in e.value.choices] == [0, 1]
    c = collect(TWO, "text/html", table_index=1)
    assert c.dataset.columns == ["B"] and c.table_caption == "Second" and c.table_index == 1


@pytest.mark.parametrize(
    ("html", "reason"),
    [
        (b"<table><tr><th>A</th><th>B</th></tr><tr><td colspan=2>x</td></tr></table>", "merged cells"),
        (b"<table><tr><th>A</th></tr><tr><td rowspan=2>x</td></tr><tr><td>y</td></tr></table>", "merged cells"),
        (b"<table><tr><td>A</td></tr><tr><td>1</td></tr></table>", "no header row"),
        (b"<table><tr><th>A</th><th>A</th></tr><tr><td>1</td><td>2</td></tr></table>", "duplicate"),
        (b"<table><tr><th>A</th><th></th></tr><tr><td>1</td><td>2</td></tr></table>", "empty column header"),
        (b"<table><tr><th>A</th><th>B</th></tr><tr><td>1</td></tr></table>", "different number"),
        (b"<table><tr><th>A</th></tr></table>", "no data rows"),
        (b"<table></table>", "no rows"),
        (b"<table><tr><th>A</th></tr><tr><td><table><tr><td>z</td></tr></table></td></tr></table>", "nested"),
        (b"<table><tr><th>A</th></tr><tr><td colspan=x>1</td></tr></table>", "invalid"),
    ],
)  # fmt: skip
def test_a_table_that_cannot_be_read_exactly_is_refused_with_its_reason(html, reason):
    with pytest.raises(NoTable) as e:
        collect(html, "text/html")
    assert any(reason in (t["refusal"] or "") for t in e.value.skipped)


def test_an_unusable_table_beside_a_good_one_is_reported_not_silently_dropped():
    page = b"<table><tr><th>A</th></tr><tr><td colspan=2>x</td></tr></table>" + TWO.split(b"\n")[1]
    c = collect(page, "text/html")
    assert c.dataset.columns == ["B"]
    assert len(c.skipped_tables) == 1 and "merged cells" in c.skipped_tables[0]["refusal"]
    assert c.manifest()["skipped_tables"] == list(c.skipped_tables)


def test_a_page_with_no_table_and_an_unknown_index_are_clean_errors():
    with pytest.raises(NoTable):
        collect(b"<html><p>no tables</p></html>", "text/html")
    with pytest.raises(EvidenceError):
        collect(TWO, "text/html", table_index=7)
    with pytest.raises(NoTable):
        collect(b"<table><tr><td>x</td></tr></table>" + TWO, "text/html", table_index=0)


def test_csv_and_json_go_through_the_datasets_loader_unchanged():
    csv = collect(
        b"party,seats\nBJP,303\nINC,52\n",
        "text/csv",
        url="https://x.gov.in/r.csv",
        retrieved_at=WHEN,
    )
    assert csv.dataset.columns == ["party", "seats"] and csv.dataset.frame.loc[1, "seats"] == "303"
    assert csv.dataset.source.kind == "fetched" and csv.dataset.source.sha256 == csv.page_sha256
    js = collect(
        b'[{"party":"BJP","seats":303},{"party":"INC","seats":52}]',
        "application/json",
        url="https://x.gov.in/d",
    )
    assert js.dataset.frame.loc[1, "seats"] == 303 and js.dataset.source.name.endswith(".json")


def test_other_content_types_and_bad_data_are_refused():
    with pytest.raises(EvidenceError):
        collect(b"%PDF", "application/pdf")
    with pytest.raises(Exception):  # noqa: B017  (the datasets loader's own error type)
        collect(b"not json", "application/json")


def test_collecting_is_deterministic():
    a = collect(PAGE, "text/html", url=URL, retrieved_at=WHEN)
    b = collect(PAGE, "text/html", url=URL, retrieved_at=WHEN)
    assert a.dataset.source == b.dataset.source and a.manifest() == b.manifest()
