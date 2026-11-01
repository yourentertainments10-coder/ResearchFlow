"""Session: history of requests, specs and result hashes; deterministic replay. Numbers are hand-computed
from SALES (tests/unit/test_workspace_core.py)."""

from __future__ import annotations

import json

import pytest
from test_workspace_core import SALES

from sie.datasets import CleanPlan
from sie.workspace import ReplayMismatch, replay, start_session
from sie.workspace import session as session_module

REQUESTS = [
    "Compare revenue by region",
    "Show as table",
    "Sum revenue by month",
    "Show as line chart",
    "Average revenue by product",
    "Top 2 product by revenue",
    "Sum sales by month",  # unsupported: no such column
    "Sum revenue",  # clarification
    "Show as line chart",  # unsupported on a category result
]


def run(data: bytes = SALES, requests=REQUESTS):
    s = start_session(data, "sales.csv")
    for r in requests:
        s.ask(r)
    return s


def table(session, analysis_id):
    return next(i for i in session.items if i.planned.id == analysis_id).result.table


def test_numbers_are_the_hand_computed_ones():
    s = run()
    by_region = table(s, "r1")
    assert dict(zip(by_region["region"], by_region["revenue:sum"], strict=True)) == {
        "South": 110, "North": 70, "(null)": 40, "East": 10,
    }  # fmt: skip
    months = table(s, "r3")
    assert list(months["period"]) == ["2025-01", "2025-02", "2025-03", "2025-04"]
    assert list(months["revenue:sum"])[0:2] == [50, 50] and list(months["revenue:sum"])[3] == 130
    means = table(s, "r5")  # A: (20+50+20)/3 (blank revenue excluded) ; B: (30+10+40+60)/4
    assert dict(zip(means["product"], means["revenue:mean"], strict=True)) == {"A": 30, "B": 35}
    top = table(s, "r6")
    assert list(top["product"]) == ["B", "A"] and list(top["revenue:sum"]) == [140, 90]


def test_every_step_is_recorded_in_order():
    s = run()
    assert [st.index for st in s.steps] == list(range(1, len(REQUESTS) + 1))
    assert [st.kind for st in s.steps] == [
        "plan", "chart_change", "plan", "chart_change", "plan", "plan",
        "unsupported", "clarification", "unsupported",
    ]  # fmt: skip
    assert [st.request for st in s.steps] == REQUESTS
    assert all(st.verification == "verified" for st in s.steps if st.spec)
    assert all(st.result_sha256 is None for st in s.steps if not st.spec)


def test_chart_change_reuses_the_result_and_its_hash():
    s = run()
    first, redrawn = s.steps[0], s.steps[1]
    assert redrawn.result_sha256 == first.result_sha256 and redrawn.spec == first.spec
    assert (first.chart, redrawn.chart) == ("bar", "table")
    assert table(s, "r1") is table(s, "r2")  # same object: nothing recomputed
    item = next(i for i in s.items if i.planned.id == "r2")
    assert item.svg is None  # a table has no picture
    line = next(i for i in s.items if i.planned.id == "r4")
    assert line.svg and line.svg.startswith("<svg")


def test_result_hash_comes_from_the_engine():
    s = run()
    item = s.items[0]
    assert s.steps[0].result_sha256 == item.result.provenance.result_sha256 != ""


def test_failed_requests_change_nothing():
    s = run(requests=["Sum revenue by region"])
    before = (len(s.items), s._current)
    for bad in ("Sum sales by month", "Sum revenue", "DROP TABLE x", "Show as line chart"):
        s.ask(bad)
    assert (len(s.items), s._current) == before
    assert s.ask("Show as table").kind == "chart_change"  # the earlier plan is still current


def test_hostile_requests_are_recorded_as_text_only():
    s = start_session(SALES, "sales.csv")
    st = s.ask("Sum revenue by region; DROP TABLE sales\x00")
    assert st.kind == "unsupported" and st.spec is None
    assert "\x00" not in json.dumps(s.history())


def test_history_is_json_with_no_time_or_paths():
    h = run().history()
    text = json.dumps(h, sort_keys=True)
    assert json.loads(text) == h
    for banned in ("/tmp", "T00:", "uuid", "timestamp", "created"):
        assert banned not in text
    assert set(h) == {"version", "source_sha256", "cleaning", "steps"}


def test_same_input_and_requests_give_the_same_hashes():
    a, b = run(), run()
    assert a.history() == b.history()
    assert a.sha256() == b.sha256()
    assert [s.result_sha256 for s in a.steps] == [s.result_sha256 for s in b.steps]
    assert run(requests=REQUESTS[:3]).sha256() != a.sha256()


def test_replay_reproduces_the_history():
    original = run()
    history = json.loads(json.dumps(original.history()))  # as stored and read back
    again = replay(SALES, "sales.csv", history)
    assert again.history() == original.history() and again.sha256() == original.sha256()


def test_replay_with_cleaning_plan():
    plan = CleanPlan(steps=[], dedupe=True) if "dedupe" in CleanPlan.model_fields else None
    s = start_session(SALES, "sales.csv", plan=plan)
    s.ask("Sum revenue by region")
    again = replay(SALES, "sales.csv", s.history(), plan=plan)
    assert again.sha256() == s.sha256()


def test_replay_detects_a_changed_file():
    history = run().history()
    changed = SALES.replace(b"South,B,2025-04-10,6,60", b"South,B,2025-04-10,6,61")
    with pytest.raises(ReplayMismatch) as err:
        replay(changed, "sales.csv", history)
    msgs = " | ".join(err.value.differences)
    assert "source file hash" in msgs and "result_sha256" in msgs


def test_replay_detects_a_tampered_history():
    history = json.loads(json.dumps(run().history()))
    history["steps"][0]["result_sha256"] = "0" * 64
    with pytest.raises(ReplayMismatch) as err:
        replay(SALES, "sales.csv", history)
    assert err.value.differences == ["step 1: result_sha256 differs from the recorded one"]
    history = json.loads(json.dumps(run().history()))
    history["steps"][2]["spec"]["freq"] = "year"
    with pytest.raises(ReplayMismatch):
        replay(SALES, "sales.csv", history)


@pytest.mark.parametrize("bad", [None, [], {}, {"version": 99}, {"version": 1, "steps": "x"}])
def test_replay_rejects_malformed_history(bad):
    with pytest.raises(ReplayMismatch):
        replay(SALES, "sales.csv", bad)


def test_replay_ignores_recorded_specs_when_choosing_what_to_run():
    # a history cannot smuggle an operation: only request text is re-asked, specs are only compared
    history = json.loads(json.dumps(run().history()))
    history["steps"][0]["spec"] = {"op": "describe"}
    with pytest.raises(ReplayMismatch):
        replay(SALES, "sales.csv", history)


def test_session_module_has_no_network_or_eval():
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(session_module))
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
    assert not imported & {"os", "subprocess", "socket", "urllib", "requests", "httpx"}
