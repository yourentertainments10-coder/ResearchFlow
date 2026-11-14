"""Second review of the Titanic run: key figures that are ids, a repeated chart under another name, a
"Top 3" that lost its name, a "rows" column that over-stated what an average used, steps that change
nothing, requests such as "count by" and "rate of", cluster text, and removing results."""

from __future__ import annotations

import http.client
import json
import random
import threading

import pytest

from sie.workspace import start_session
from sie.workspace.session import replay
from sie.workspace_server import make_server


def titanic(n: int = 600) -> bytes:
    r = random.Random(3)
    rows = ["PassengerId,Survived,Pclass,Name,Sex,Age,Fare,Embarked"]
    for i in range(1, n + 1):
        sex = r.choice(["male", "female"])
        surv = int(r.random() < (0.74 if sex == "female" else 0.19))
        age = "" if r.random() < 0.2 else str(r.randint(1, 80))
        emb = r.choice(["S", "S", "C", "Q", ""])
        rows.append(
            f'{i},{surv},{r.choice([1, 2, 3, 3])},"P, Mr. {i}",{sex},{age},{round(r.random() * 80 + 5, 2)},{emb}'
        )
    return ("\n".join(rows) + "\n").encode()


def test_key_figures_never_describe_an_id_column():
    s = start_session(titanic(), "t.csv")
    labels = [c.label for c in s.workspace.cards]
    assert not any("PassengerId" in x for x in labels)
    assert any("Survived" in x or "Fare" in x or "Age" in x for x in labels)


def test_an_average_says_how_many_rows_it_really_used():
    s = start_session(titanic(), "t.csv")
    step = s.ask("Average Age by Sex")
    item = s.items[-1]
    t = item.result.table
    assert list(t.columns) == ["Sex", "rows", "rows with Age", "Age:mean"], step.message
    assert item.verification.status == "verified"
    import pandas as pd

    frame = pd.read_csv(__import__("io").BytesIO(titanic()))
    for _, row in t.iterrows():
        part = frame[frame["Sex"] == row["Sex"]]
        assert row["rows"] == len(part) and row["rows with Age"] == int(part["Age"].notna().sum())
        assert row["rows with Age"] < row["rows"]


def test_a_wrong_used_row_count_is_caught_by_the_independent_check():
    from sie.workspace.verify import verify_result

    s = start_session(titanic(), "t.csv")
    s.ask("Average Age by Sex")
    item = s.items[-1]
    assert (
        verify_result(s.workspace.dataset, item.planned.spec, item.result.table).status
        == "verified"
    )
    bad = item.result.table.copy()
    bad.loc[0, "rows with Age"] = int(bad.loc[0, "rows with Age"]) + 1
    assert verify_result(s.workspace.dataset, item.planned.spec, bad).status == "unverified"


def test_a_column_without_blanks_gets_no_extra_column():
    s = start_session(titanic(), "t.csv")
    s.ask("Average Fare by Sex")
    assert list(s.items[-1].result.table.columns) == ["Sex", "rows", "Fare:mean"]


def test_compare_does_not_repeat_the_starter_chart_under_another_spec():
    s = start_session(titanic(), "t.csv")
    before = len(s.workspace.items)
    step = s.ask("Compare Survived by Sex")
    assert step.kind == "already_shown" and not s.items
    assert len(s.workspace.items) == before


def test_a_cut_off_top_list_is_not_the_same_as_the_full_list():
    s = start_session(titanic(), "t.csv")
    step = s.ask("Top 1 Sex by Survived")
    assert step.kind == "plan" and len(s.items) == 1


def test_top_n_keeps_its_name_when_the_empty_group_makes_it_a_real_top_list():
    s = start_session(titanic(), "t.csv")
    step = s.ask("Top 3 Embarked by Fare")
    assert step.kind == "plan" and step.title == "Top 3 Embarked by sum of Fare"
    assert any("shown as (null)" in n for n in step.notes)
    assert len(s.items[-1].result.table) == 3 and s.items[-1].verification.status == "verified"


def test_top_n_with_fewer_real_groups_is_still_called_a_full_list():
    s = start_session(titanic(), "t.csv")
    step = s.ask("Top 5 Sex by Fare")
    assert step.title == "Sum of Fare by Sex"


@pytest.mark.parametrize(
    ("column", "method"), [("Name", "median"), ("Sex", "mode"), ("Survived", "median")]
)
def test_a_fill_that_changes_nothing_is_refused_and_not_logged(column, method):
    s = start_session(titanic(), "t.csv")
    n = len(s.cleaning)
    step = s.clean({"op": "fill_missing", "column": column, "method": method})
    assert step.kind == "clean_rejected" and "would not change any value" in step.message
    assert len(s.cleaning) == n and s.workspace.cleaning_steps == []


def test_removing_duplicates_when_there_are_none_is_refused():
    s = start_session(titanic(), "t.csv")
    assert s.clean({"op": "remove_duplicates"}).kind == "clean_rejected"
    assert s.cleaning == []


def test_a_real_fill_still_applies():
    s = start_session(titanic(), "t.csv")
    assert s.clean({"op": "fill_missing", "column": "Age", "method": "median"}).kind == "clean"


def test_outlier_count_after_a_fill_comes_with_the_reason():
    s = start_session(titanic(), "t.csv")
    s.clean({"op": "fill_missing", "column": "Age", "method": "median"})
    texts = [x.reason for x in s.suggestions() if x.step.op == "flag_outliers"]
    for t in texts:
        if "Age" in t:
            assert "filled with one typical value" in t


def test_count_by_reads_as_the_starter_card_or_a_new_verified_table():
    s = start_session(titanic(), "t.csv")
    assert s.ask("count by Sex").kind == "already_shown"
    step = s.ask("count by Pclass")
    assert step.kind == "plan" and step.title == "Number of rows by Pclass"
    assert s.items[-1].verification.status == "verified"
    assert s.ask("How many rows by Embarked").kind == "already_shown"


def test_a_value_count_of_a_whole_number_column_is_checked_and_a_wrong_one_is_caught():
    from sie.workspace.verify import verify_result

    s = start_session(titanic(), "t.csv")
    s.ask("count by Pclass")
    item = s.items[-1]
    bad = item.result.table.copy()
    bad.loc[0, "count"] = int(bad.loc[0, "count"]) + 1
    assert verify_result(s.workspace.dataset, item.planned.spec, bad).status == "unverified"


def test_rate_of_a_zero_one_column_is_the_average_and_says_so():
    s = start_session(titanic(), "t.csv")
    step = s.ask("Rate of Survived by Sex")
    assert step.kind == "plan" and step.title == "Share of Survived by Sex"
    assert any("share of rows with 1" in n for n in step.notes)
    t = s.items[-1].result.table
    assert 0 <= float(str(t.iloc[0]["Survived:mean"]).replace(",", "")) <= 1
    assert s.items[-1].verification.status == "verified"
    assert s.ask("Survived rate by Pclass").kind == "plan"


@pytest.mark.parametrize(
    "text", ["survival rate by Sex", "rate of Fare by Sex", "rate of Nope by Sex"]
)
def test_a_rate_that_cannot_be_read_is_said_plainly(text):
    s = start_session(titanic(), "t.csv")
    step = s.ask(text)
    assert step.kind == "unsupported" and not s.items


def test_the_top_finding_reads_naturally():
    s = start_session(titanic(), "t.csv")
    texts = [f.text for f in s.workspace.findings]
    assert not any("The highest Sex is" in t for t in texts)
    assert any("Within Sex, the group" in t for t in texts)


def test_cluster_groups_say_which_text_value_is_most_common():
    pytest.importorskip("sklearn")
    s = start_session(titanic(), "t.csv")
    from sie.workspace import modeling as md

    rep, _ = md.run_model(s.workspace.dataset, s.workspace.profile, md.ModelSpec(None))
    assert rep.clusters and all(p["common"] for p in rep.clusters["profiles"])
    assert any("Sex" in p["common"] for p in rep.clusters["profiles"])


def test_removing_a_result_and_clearing_are_recorded_and_replay_the_same():
    data = titanic()
    s = start_session(data, "t.csv")
    s.ask("Average Age by Sex")
    s.ask("Median Fare by Pclass")
    first, second = (i.planned.id for i in s.items)
    step = s.remove(first)
    assert step.kind == "removed" and [i.planned.id for i in s.items] == [second]
    assert s.remove(first).kind == "remove_rejected"  # already gone
    assert s.remove("a1").kind == "remove_rejected"  # a starter card stays
    assert s.remove("r1; DROP").kind == "remove_rejected"
    s.ask("Maximum Fare by Sex")
    cleared = s.clear_results()
    assert cleared.kind == "cleared" and s.items == [] and s.models == []
    again = replay(data, "t.csv", s.history())
    assert again.items == [] and len(again.steps) == len(s.steps)


def test_a_removed_result_is_not_asked_again_after_cleaning():
    s = start_session(titanic(), "t.csv")
    s.ask("Average Age by Sex")
    s.remove(s.items[-1].planned.id)
    s.clean({"op": "fill_missing", "column": "Age", "method": "median"})
    assert s.items == []


def test_a_remaining_result_is_still_asked_again_after_cleaning():
    s = start_session(titanic(), "t.csv")
    s.ask("Average Age by Sex")
    s.ask("Median Fare by Pclass")
    s.remove(s.items[0].planned.id)
    s.clean({"op": "fill_missing", "column": "Age", "method": "median"})
    assert [i.planned.title for i in s.items] == ["Median of Fare by Pclass"]


# -- HTTP -----------------------------------------------------------------------------------------


@pytest.fixture
def server():
    srv = make_server(0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def call(port, method, path, body=None, headers=None, host=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    h = {"Host": host or f"127.0.0.1:{port}", **(headers or {})}
    if body is not None:
        h["Content-Length"] = str(len(body))
    conn.request(method, path, body, h)
    r = conn.getresponse()
    data = r.read()
    conn.close()
    return r.status, (json.loads(data) if data[:1] in (b"{", b"[") else data)


JSON = {"Content-Type": "application/json"}


def test_remove_and_clear_over_http_with_the_same_guards(server):
    assert call(server, "POST", "/api/remove", b'{"id":"r1"}', JSON)[0] == 404  # nothing loaded
    call(server, "POST", "/api/upload?name=t.csv", titanic())
    status, out = call(
        server, "POST", "/api/ask", json.dumps({"text": "Average Age by Sex"}).encode(), JSON
    )
    rid = out["step"]["analysis_id"]
    assert call(server, "POST", "/api/remove", b"{}", JSON)[0] == 400
    assert call(server, "POST", "/api/remove", b'{"id": 5}', JSON)[0] == 400
    assert (
        call(server, "POST", "/api/remove", b'{"id":"r1"}', {"Content-Type": "text/plain"})[0]
        == 415
    )
    evil = {**JSON, "Origin": "http://evil.example"}
    assert call(server, "POST", "/api/remove", json.dumps({"id": rid}).encode(), evil)[0] == 403
    assert call(server, "POST", "/api/clear", b"{}", JSON, host="evil.example")[0] == 403
    status, out = call(server, "POST", "/api/remove", json.dumps({"id": rid}).encode(), JSON)
    assert status == 200 and out["step"]["kind"] == "removed"
    assert not [a for a in out["state"]["analyses"] if a["origin"] == "asked"]
    call(server, "POST", "/api/ask", json.dumps({"text": "Median Fare by Pclass"}).encode(), JSON)
    status, out = call(server, "POST", "/api/clear", b"{}", JSON)
    assert status == 200 and out["step"]["kind"] == "cleared"
    assert not [a for a in out["state"]["analyses"] if a["origin"] == "asked"]
