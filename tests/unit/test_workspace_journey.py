"""Journey acceptance (design slice 4): one scripted run of the whole CSV journey on a fresh, messy file,
then the negative cases, over real HTTP on 127.0.0.1. Evidence is described in docs/WORKSPACE_ACCEPTANCE.md.

The file is generated from a fixed seed, so every run sees the same bytes. Nothing here touches the
network beyond loopback, and nothing is written outside pytest's temp directory."""

from __future__ import annotations

import hashlib
import http.client
import io
import json
import random
import threading
import zipfile

import pytest

from sie.datasets import Limits
from sie.workspace import session as session_module
from sie.workspace.models import Verification
from sie.workspace_server import make_server

JSON = {"Content-Type": "application/json"}


def messy_customers() -> bytes:
    """262 rows: text money with thousands marks, blanks, 2 exact repeats, text dates, a target to predict."""
    rnd = random.Random(7)
    rows = ["id,region,plan,price,signed_up,age,churned"]
    for i in range(260):
        price = f'"{rnd.randint(1, 9) * 1000 + rnd.randint(0, 99):,}.50"' if i % 9 else ""
        age = str(rnd.randint(18, 70)) if i % 11 else ""
        plan = rnd.choice(["basic", "pro", "team"])
        churned = "yes" if (plan == "basic" and rnd.random() < 0.6) or rnd.random() < 0.1 else "no"
        region = rnd.choice(["north", "south", "east", "west"])
        day = f"{1 + i % 28:02d}/{1 + i % 12:02d}/2024"
        rows.append(f"{i},{region},{plan},{price},{day},{age},{churned}")
    rows += [rows[5], rows[6]]
    return ("\n".join(rows) + "\n").encode()


class Client:
    def __init__(self, server):
        self.port = server.server_address[1]

    def call(self, method, path, body=None, headers=None, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        h = {"Host": host or f"127.0.0.1:{self.port}", **(headers or {})}
        if body is not None and "Content-Length" not in h:
            h["Content-Length"] = str(len(body))
        conn.request(method, path, body, h)
        r = conn.getresponse()
        data = r.read()
        conn.close()
        return r.status, data, {k.lower(): v for k, v in r.getheaders()}

    def json(self, *a, **k):
        status, data, _ = self.call(*a, **k)
        return status, json.loads(data)

    def upload(self, data, name="customers.csv", **kw):
        return self.json("POST", f"/api/upload?name={name}", data, **kw)

    def ask(self, text):
        return self.json("POST", "/api/ask", json.dumps({"text": text}).encode(), JSON)

    def clean(self, step):
        return self.json("POST", "/api/clean", json.dumps({"step": step}).encode(), JSON)

    def model(self, **body):
        return self.json("POST", "/api/model", json.dumps(body).encode(), JSON)


@pytest.fixture
def client():
    server = make_server(0, limits=Limits(max_bytes=50_000), stream_limit=200_000)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield Client(server)
    server.shutdown()
    server.server_close()


def test_the_whole_journey_on_a_fresh_messy_file(client):
    data = messy_customers()
    # 1. upload and profile: the first look appears without being asked
    status, view = client.upload(data)
    assert status == 200
    assert view["source"]["rows"] == 262 and view["source"]["columns"] == 7
    assert view["source"]["sha256"] == hashlib.sha256(data).hexdigest()
    assert len(view["cards"]) >= 4 and view["analyses"] and view["findings"]
    assert view["cleaning"]["applied"] == []  # suggestions only: nothing changed yet
    offered = {s["id"] for s in view["cleaning"]["suggestions"]}
    assert "remove_duplicates" in offered and "fill_missing:price" in offered

    # 2. a plain-language request is planned into a typed step and answered
    status, out = client.ask("Compare age by region")
    assert status == 200 and out["step"]["kind"] == "plan", out["step"]
    asked = [a for a in out["state"]["analyses"] if a["origin"] == "asked"]
    assert asked and all(a["verification"]["status"] == "verified" for a in asked)

    # 3. cleaning, each step approved one at a time, each independently verified
    steps = [
        {"op": "remove_duplicates", "subset": None},
        {"op": "text_to_number", "column": "price", "decimal": ".", "thousands": ","},
        {"op": "fill_missing", "column": "price", "method": "median", "value": None},
        {"op": "fill_missing", "column": "age", "method": "median", "value": None},
        {"op": "text_to_date", "column": "signed_up", "format": "%d/%m/%Y"},
    ]
    for step in steps:
        status, out = client.clean(step)
        assert status == 200 and out["step"]["kind"] == "clean", out["step"]
        assert out["state"]["cleaning"]["verification"]["status"] == "verified"
    state = out["state"]
    assert state["source"]["rows"] == 260  # the two repeats are gone, nothing else dropped
    assert len(state["cleaning"]["applied"]) == len(steps)
    assert state["cleaning"]["rows_loaded"] == 262  # the original is still what was loaded

    # 4. the analyses were re-asked on the cleaned data and still verify
    assert all(a["verification"]["status"] == "verified" for a in state["analyses"])

    # 5. prediction: a baseline is shown, scores are on rows the models did not see, and verified
    status, out = client.model(target="churned")
    assert status == 200 and out["step"]["kind"] == "model", out["step"]
    report = out["state"]["models"][0]
    assert report["verification"]["status"] == "verified"
    assert report["evaluation"]["method"] == "holdout"
    assert report["evaluation"]["test_rows"] > 0
    assert "baseline" in out["step"]["message"].lower()

    # 6. the same on the latest rows by date
    status, out = client.model(target="churned", time="signed_up")
    assert status == 200 and out["step"]["kind"] == "model", out["step"]
    assert {m["evaluation"]["method"] for m in out["state"]["models"]} >= {
        "holdout",
        "time_holdout",
    }

    # 7. exports are real, consistent with the session, and repeatable byte for byte
    aid = next(a["id"] for a in out["state"]["analyses"] if a["origin"] == "asked")
    status, csv_bytes, h = client.call("GET", f"/api/export/{aid}.csv")
    assert status == 200 and h["content-type"].startswith("text/csv")
    status, xlsx, _ = client.call("GET", f"/api/export/{aid}.xlsx")
    assert status == 200 and zipfile.ZipFile(io.BytesIO(xlsx)).testzip() is None
    status, rep, h = client.call("GET", "/api/export/report.html")
    assert status == 200 and b"<script" not in rep
    assert b"Compare age by region" in rep and b"churned" in rep
    assert client.call("GET", "/api/export/report.html")[1] == rep
    assert client.call("GET", f"/api/export/{aid}.csv")[1] == csv_bytes

    # 8. undo goes back step by step; delete leaves nothing
    status, out = client.json("POST", "/api/clean/undo", b"{}", JSON)
    assert status == 200 and len(out["state"]["cleaning"]["applied"]) == len(steps) - 1
    assert client.call("DELETE", "/api/session")[0] in (200, 204)
    assert client.json("GET", "/api/state")[1] == {"loaded": False}


def test_same_file_same_steps_same_report(client):
    """Determinism: the same bytes and the same approved steps give an identical report twice."""
    reports = []
    for _ in range(2):
        client.upload(messy_customers())
        client.clean({"op": "remove_duplicates", "subset": None})
        client.model(target="churned")
        reports.append(client.call("GET", "/api/export/report.html")[1])
        client.call("DELETE", "/api/session")
    assert reports[0] == reports[1]


def test_unverified_results_are_not_exported(client, monkeypatch):
    monkeypatch.setattr(
        session_module,
        "verify_result",
        lambda ds, spec, table: Verification("unverified", 1, ["forced"]),
    )
    client.upload(messy_customers())
    _, out = client.ask("Compare age by region")
    asked = next(a for a in out["state"]["analyses"] if a["origin"] == "asked")
    assert asked["exportable"] is False and asked["table"] is None and asked["image"] is None
    assert client.call("GET", f"/api/export/{asked['id']}.csv")[0] == 409
    assert client.call("GET", f"/api/export/{asked['id']}.xlsx")[0] == 409
    report = client.call("GET", "/api/export/report.html")[1].decode()
    assert "Not included" in report and "forced" in report


def test_hostile_and_broken_input_is_said_plainly(client):
    # not a table at all
    assert client.upload(b"a,b\n1,2\x00\n", name="blob.csv")[0] == 422
    assert client.upload(b"", name="empty.csv")[0] == 422
    assert client.upload(b"a,b\n1,2,3\n", name="ragged.csv")[0] == 422
    assert client.json("GET", "/api/state")[1] == {"loaded": False}  # nothing half-loaded
    # markup in a name and in a request is text, never executed or echoed unescaped
    status, view = client.upload(
        b'"<img src=x onerror=1>",n\nr1,1\nr2,2\nr3,3\n', name="<script>.csv"
    )
    assert status == 200
    page = client.call("GET", "/api/export/report.html")[1]
    assert b"<img src=x" not in page and b"<script" not in page
    status, out = client.ask("Sum n by region; DROP TABLE users")
    assert status == 200 and out["step"]["kind"] in ("unsupported", "clarify")
    # a cleaning step that cannot apply is recorded and changes nothing
    before = client.json("GET", "/api/state")[1]["source"]["rows"]
    status, out = client.clean({"op": "drop_column", "column": "no_such_column"})
    assert out["step"]["kind"] == "clean_rejected"
    assert out["state"]["source"]["rows"] == before
    # a model that cannot be trusted is refused with a reason
    status, out = client.model(target="no_such_column")
    assert out["step"]["kind"] == "unsupported"
    status, out = client.model(target="n")  # three rows
    assert out["step"]["kind"] == "model_rejected"


def test_local_server_guards_hold_across_the_journey(client):
    data = messy_customers()
    assert client.upload(data, host="evil.example")[0] == 403
    assert client.upload(data, headers={"Origin": "http://evil.example"})[0] == 403
    status, _ = client.upload(data)
    assert status == 200
    for path, body in (("/api/ask", b'{"text":"x"}'), ("/api/clean", b"{}"), ("/api/model", b"{}")):
        status, _, _ = client.call("POST", path, body, {**JSON, "Origin": "http://evil.example"})
        assert status == 403
        assert client.call("POST", path, body, JSON, host="evil.example")[0] == 403
    assert client.call("GET", "/api/export/report.html", host="evil.example")[0] == 403
    # oversized upload is refused, the loaded session survives
    assert client.upload(b"a,b\n" + b"1,2\n" * 80_000, name="big.csv")[0] == 413
    assert client.json("GET", "/api/state")[1]["source"]["rows"] == 262
    # nothing outside the fixed routes
    for path in ("/etc/passwd", "/../secret", "/api/export/../state", "/api/unknown"):
        assert client.call("GET", path)[0] in (400, 404)
