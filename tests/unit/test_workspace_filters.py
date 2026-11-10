"""Row filters ("only 2024", "exclude north"): the typed step, its checks, the plain-words grammar, the
session (approval, log, undo, replay, re-ask) and the server route. Every count is worked out by hand from
the small files below. See docs/WORKSPACE_FILTERS.md."""

from __future__ import annotations

import json
import threading

import pytest

from sie.datasets import Limits, LoadOptions, open_dataset, profile
from sie.datasets.models import OperationError
from sie.workspace import cleaning as cl
from sie.workspace import replay, start_session
from sie.workspace.clean_verify import verify_cleaning
from sie.workspace.filters import parse_filter_request
from sie.workspace_server import make_server

# city: X at a,c,e,g,i (5); Y at b,f,h,j (4); blank at d
# age: 20,_,30,40,_,50,22,26,28,34 ; fare: 10..18 then 300
CSV = (
    b"name,age,fare,city,cabin\n"
    b"a,20,10,X,\nb,,11,Y,\nc,30,12,X,\nd,40,13,,\ne,,14,X,\n"
    b"f,50,15,Y,\ng,22,16,X,\nh,26,17,Y,\ni,28,18,X,\nj,34,300,Y,\n"
)
DAYS = b"id,when,zip\n1,2024-01-15,007\n2,2024-06-30,7\n3,2025-01-01,010\n4,,\n5,2025-12-31,7\n"
SLASH = b"id,when\n1,15/01/2024\n2,30/06/2024\n3,01/01/2025\n"
SRC = "t.csv"


def dataset(data: bytes = CSV):
    return open_dataset(data, SRC, LoadOptions(), Limits())


def run(step, data: bytes = CSV):
    ds = dataset(data)
    out = cl.apply_steps(ds, [step])
    assert verify_cleaning(ds, [step], out.dataset, out.entries).status == "verified"
    return out


def rows(out, col="name"):
    return out.dataset.frame[col].tolist()


def f(**kw):
    return cl.FilterRows(**kw)


# -- the step --------------------------------------------------------------------------------------


def test_equals_in_and_their_negatives_on_text():
    eq = run(f(column="city", test="equals", values=["X"]))
    assert rows(eq) == ["a", "c", "e", "g", "i"]  # the blank city row is dropped
    e = eq.entries[0]
    assert (e["rows_before"], e["rows_after"], e["rows_removed"]) == (10, 5, 5)
    assert e["rows_without_value_removed"] == 1
    ne = run(f(column="city", test="not_equals", values=["X"]))
    assert rows(ne) == ["b", "d", "f", "h", "j"]  # an empty cell is not X, so it stays
    assert ne.entries[0]["rows_without_value_removed"] == 0
    assert len(rows(run(f(column="city", test="in", values=["X", "Y"])))) == 9
    assert rows(run(f(column="city", test="not_in", values=["X", "Y"]))) == ["d"]


def test_text_is_matched_exactly_capitals_included():
    with pytest.raises(OperationError, match="no row would remain"):
        run(f(column="city", test="equals", values=["x"]))


def test_between_on_numbers_either_end_optional_blanks_dropped():
    assert rows(run(f(column="fare", test="between", low="11", high="15"))) == list("bcdef")
    assert rows(run(f(column="fare", test="between", low="17"))) == ["h", "i", "j"]
    assert rows(run(f(column="fare", test="between", high="10"))) == ["a"]
    out = run(f(column="age", test="between", low="25", high="40"))
    assert rows(out) == ["c", "d", "h", "i", "j"]  # ages 30 40 26 28 34
    assert out.entries[0]["rows_without_value_removed"] == 2


def test_numbers_written_as_text_compare_as_numbers():
    assert rows(run(f(column="age", test="equals", values=["30"]))) == ["c"]
    assert rows(run(f(column="age", test="equals", values=["30.0"]))) == ["c"]
    assert rows(run(f(column="fare", test="in", values=["10", "300"]))) == ["a", "j"]


def test_dates_as_iso_text_and_as_real_dates():
    year = f(column="when", test="between", low="2024-01-01", high="2024-12-31")
    assert rows(run(year, DAYS), "id") == ["1", "2"]
    assert rows(run(f(column="when", test="equals", values=["2025-01-01"]), DAYS), "id") == ["3"]
    assert rows(run(f(column="when", test="not_equals", values=["2025-01-01"]), DAYS), "id") == [
        "1",
        "2",
        "4",
        "5",
    ]
    ds = dataset(SLASH)  # after text to date, the same filter works on real dates
    steps = [cl.TextToDate(column="when", format="%d/%m/%Y"), year]
    out = cl.apply_steps(ds, steps)
    assert out.dataset.frame["id"].tolist() == ["1", "2"]
    assert verify_cleaning(ds, steps, out.dataset, out.entries).status == "verified"


def test_a_value_with_a_leading_zero_is_an_identifier_not_a_number():
    assert rows(run(f(column="zip", test="equals", values=["7"]), DAYS), "id") == ["2", "5"]
    with pytest.raises(OperationError, match="holds text"):
        run(f(column="zip", test="between", low="1"), DAYS)


@pytest.mark.parametrize(
    ("step", "match"),
    [
        (dict(column="nope", test="equals", values=["x"]), "unknown column"),
        (dict(column="city", test="between", low="a"), "holds text"),
        (dict(column="fare", test="between", low="abc"), "must be a number"),
        (dict(column="fare", test="between", low="nan"), "finite"),
        (dict(column="fare", test="between", low="20", high="10"), "low is above high"),
        (dict(column="fare", test="equals", values=["x"]), "must be a number"),
        (dict(column="fare", test="between", low="1e999"), "finite"),
        (dict(column="fare", test="equals", values=["1000000"]), "no row would remain"),
    ],
)
def test_a_step_that_cannot_run_says_why_and_changes_nothing(step, match):
    ds = dataset()
    before = ds.frame.copy()
    with pytest.raises(OperationError, match=match):
        cl.apply_steps(ds, [f(**step)])
    assert ds.frame.equals(before)


def test_dates_must_be_written_year_month_day():
    for bad in ("2024", "15/01/2024", "2024-1-5", "2024-13-01"):
        with pytest.raises(OperationError, match="YYYY-MM-DD"):
            run(f(column="when", test="between", low=bad), DAYS)


@pytest.mark.parametrize(
    "bad",
    [
        {"op": "filter_rows", "column": "c", "test": "equals"},
        {"op": "filter_rows", "column": "c", "test": "equals", "values": ["a", "b"]},
        {"op": "filter_rows", "column": "c", "test": "in", "values": []},
        {"op": "filter_rows", "column": "c", "test": "in", "values": ["a"] * 51},
        {"op": "filter_rows", "column": "c", "test": "between"},
        {"op": "filter_rows", "column": "c", "test": "between", "low": "1", "values": ["1"]},
        {"op": "filter_rows", "column": "c", "test": "equals", "values": ["a"], "low": "1"},
        {"op": "filter_rows", "column": "c", "test": "like", "values": ["a"]},
        {"op": "filter_rows", "column": "c", "test": "equals", "values": ["a"], "sql": "x"},
        {"op": "filter_rows", "column": "", "test": "equals", "values": ["a"]},
        {"op": "filter_rows", "column": "c", "test": "equals", "values": [""]},
    ],
)
def test_a_badly_shaped_step_is_refused(bad):
    with pytest.raises(OperationError, match="not a cleaning step"):
        cl.parse_step(bad)


def test_a_filter_is_described_and_titled_in_words():
    out = run(f(column="city", test="equals", values=["X"]))
    text = cl.describe(out.entries[0], "0" * 64)
    assert text == (
        "Kept the 5 rows where city is X; 5 rows were removed. 1 of them had no city value at all."
    )
    assert cl.step_title(f(column="city", test="not_in", values=["X", "Y"])) == (
        "Keep rows where city is none of X, Y"
    )
    assert cl.step_title(f(column="fare", test="between", low="1", high="9")) == (
        "Keep rows where fare is from 1 to 9"
    )


def test_the_independent_check_catches_a_wrong_filter(monkeypatch):
    ds = dataset()
    step = f(column="city", test="equals", values=["X"])
    out = cl.apply_steps(ds, [step])
    out.dataset.frame.drop(index=out.dataset.frame.index[0], inplace=True)
    bad = verify_cleaning(ds, [step], out.dataset, out.entries)
    assert bad.status == "unverified"

    def keeps_everything(d, s):
        return d, cl._entry(s, len(d.frame), len(d.frame), rows_removed=0,
                            rows_without_value_removed=0, kind="text")  # fmt: skip

    monkeypatch.setattr(cl, "_RUN", {**cl._RUN, "filter_rows": keeps_everything})
    wrong = cl.apply_steps(ds, [step])
    assert verify_cleaning(ds, [step], wrong.dataset, wrong.entries).status == "unverified"


# -- plain words -----------------------------------------------------------------------------------


def read(text, data: bytes = CSV):
    ds = dataset(data)
    return parse_filter_request(text, ds, profile(ds))


@pytest.mark.parametrize(
    ("text", "step"),
    [
        ("only city is X", f(column="city", test="equals", values=["X"])),
        ("Only CITY = y", f(column="city", test="equals", values=["Y"])),
        ("keep only rows where city is not X", f(column="city", test="not_equals", values=["X"])),
        ("only city is X, Y", f(column="city", test="in", values=["X", "Y"])),
        ("only city is X or Y", f(column="city", test="in", values=["X", "Y"])),
        ("exclude rows where city is X", f(column="city", test="not_equals", values=["X"])),
        ("remove rows with city = Y", f(column="city", test="not_equals", values=["Y"])),
        ("exclude city is X", f(column="city", test="not_equals", values=["X"])),
        ("exclude X", f(column="city", test="not_equals", values=["X"])),
        ("hide x, y", f(column="city", test="not_in", values=["X", "Y"])),
        ("only Y", f(column="city", test="equals", values=["Y"])),
        ("only fare between 11 and 15", f(column="fare", test="between", low="11", high="15")),
        ("keep only fare from 11 to 15", f(column="fare", test="between", low="11", high="15")),
        ('only "city" is "X"', f(column="city", test="equals", values=["X"])),
    ],
)
def test_the_grammar_reads_into_typed_steps(text, step):
    assert read(text) == step


def test_a_year_needs_exactly_one_date_column():
    assert read("only 2024", DAYS) == f(
        column="when", test="between", low="2024-01-01", high="2024-12-31"
    )
    assert read("Keep only in 2025", DAYS).low == "2025-01-01"
    assert "no date column" in read("only 2024")
    two = b"a,b\n2024-01-01,2025-02-02\n2024-03-03,2025-04-04\n"
    assert "no single date column" in read("only 2024", two)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("only city is Z", "No row has city = 'Z'"),
        ("only nope is X", "no single column named 'nope'"),
        ("exclude rows where nope is X", "no single column named 'nope'"),
        ("only nope between 1 and 2", "no single column named 'nope'"),
        ("only Q", "No single text column holds 'Q'"),
        ("exclude 11", "No single text column holds '11'"),
    ],
)
def test_what_cannot_be_read_safely_is_said_plainly(text, message):
    assert message in read(text)


def test_a_value_in_two_columns_must_be_named():
    both = b"a,b\nx,x\ny,z\n"
    assert "more than one column" in read("only x", both)
    assert read("only a is x", both).column == "a"


def test_two_spellings_of_one_value_are_not_guessed():
    mixed = b"a\nNorth\nnorth\nsouth\n"
    assert "more than one spelling" in read("only a is NORTH", mixed)
    assert read("only a is north", mixed).values == ["north"]


@pytest.mark.parametrize(
    "text",
    [
        "Compare age by region",
        "Top 5 name by fare",
        "Show as line chart",
        "Predict fare",
        "Sum fare; DROP TABLE x",
        "",
        "   ",
        "remove column cabin",
        "drop rows",
        "what is only",
    ],
)
def test_other_requests_are_left_to_the_other_parsers(text):
    assert read(text) is None


def test_hostile_text_is_only_ever_a_message_or_a_typed_step():
    for text in (
        "only <img src=x onerror=1>",
        "exclude '; DROP TABLE users; --",
        "only " + "a" * 5000,
    ):
        got = read(text)
        assert isinstance(got, str) or got is None
    assert len(read("only " + ", ".join(f"v{i}" for i in range(60)))) < 400


# -- session ---------------------------------------------------------------------------------------


def test_a_request_in_words_is_one_approved_step_and_keeps_the_words():
    s = start_session(CSV, SRC)
    step = s.ask("only city is X")
    assert step.kind == "clean" and step.request == "only city is X"
    assert (
        step.message
        == "Kept the 5 rows where city is X; 5 rows were removed. 1 of them had no city value at all."
    )
    assert s.cleaning == [f(column="city", test="equals", values=["X"])]
    assert s.workspace.dataset.frame.shape[0] == 5
    assert s.workspace.cleaning_verification.status == "verified"
    assert s.workspace.original_rows == 10  # the loaded file is still what was loaded


def test_filters_stack_undo_and_reask_the_cards():
    s = start_session(CSV, SRC)
    s.ask("Average fare by city")
    s.ask("only city is X")
    assert [i.planned.id for i in s.items] == ["r2"] and s.items[0].exportable
    s.ask("only fare between 11 and 17")
    assert s.workspace.dataset.frame["name"].tolist() == ["c", "e", "g"]
    undone = s.undo_clean()
    assert undone.kind == "clean_undo" and s.workspace.dataset.frame.shape[0] == 5
    s.undo_clean()
    assert s.workspace.dataset.frame.shape[0] == 10 and s.cleaning == []


def test_a_filter_that_would_empty_the_data_is_recorded_and_changes_nothing():
    s = start_session(CSV, SRC)
    s.ask("only city is X")
    step = s.ask("only fare between 100 and 200")
    assert step.kind == "clean_rejected" and "no row would remain" in step.message
    assert s.workspace.dataset.frame.shape[0] == 5


def test_a_refused_request_is_recorded_as_unsupported_not_applied():
    s = start_session(CSV, SRC)
    step = s.ask("only city is Z")
    assert step.kind == "unsupported" and s.cleaning == []


def test_a_session_with_filters_replays_to_the_same_numbers():
    s = start_session(CSV, SRC)
    s.ask("Average fare by city")
    s.ask("exclude Y")
    s.ask("only fare between 11 and 17")
    s.ask("Compare fare by name")
    again = replay(CSV, SRC, s.history())
    assert [x.replay_key() for x in again.steps] == [x.replay_key() for x in s.steps]
    assert (
        again.workspace.dataset.frame["name"].tolist() == s.workspace.dataset.frame["name"].tolist()
    )


def test_models_and_report_see_the_filtered_rows_only():
    from sie.workspace.report import build_report

    s = start_session(CSV, SRC)
    s.ask("only city is X")
    report = build_report(s)
    text = report if isinstance(report, str) else report.decode()
    assert "Kept the 5 rows where city is X" in text
    assert "only city is X" in text


# -- server ----------------------------------------------------------------------------------------


def test_the_ask_route_applies_a_filter_and_the_page_state_follows():
    import http.client

    server = make_server(0, limits=Limits(max_bytes=50_000), stream_limit=200_000)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]

    def call(method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        c.request(method, path, body, {"Host": f"127.0.0.1:{port}", **(headers or {})})
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, json.loads(data)

    try:
        assert (
            call("POST", "/api/upload?name=t.csv", CSV, {"Content-Length": str(len(CSV))})[0] == 200
        )
        body = json.dumps({"text": "only city is X"}).encode()
        status, out = call(
            "POST",
            "/api/ask",
            body,
            {"Content-Type": "application/json", "Content-Length": str(len(body))},
        )
        assert status == 200 and out["step"]["kind"] == "clean"
        assert out["state"]["source"]["rows"] == 5
        assert out["state"]["cleaning"]["applied"][0]["text"].startswith("Kept the 5 rows")
        assert out["state"]["cleaning"]["verification"]["status"] == "verified"
        body = json.dumps({"text": "only city is Z"}).encode()
        _, out = call(
            "POST",
            "/api/ask",
            body,
            {"Content-Type": "application/json", "Content-Length": str(len(body))},
        )
        assert out["step"]["kind"] == "unsupported" and out["state"]["source"]["rows"] == 5
    finally:
        server.shutdown()
        server.server_close()
