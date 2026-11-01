"""Workspace server and report: routes, loopback and origin guards, size caps, hostile names and requests,
export refusal for unverified analyses, XSS in column names and values, nothing written to disk.
Real HTTP on 127.0.0.1 with a free port; no outside network."""

from __future__ import annotations

import http.client
import io
import json
import threading
import zipfile

import pytest
from test_workspace_core import SALES

from sie.datasets import Limits
from sie.workspace import report as report_module
from sie.workspace import session as session_module
from sie.workspace.models import Verification
from sie.workspace_server import (
    MAX_REQUEST_BYTES,
    make_server,
    page_bytes,
    prepare_page,
    safe_label,
)


class Client:
    def __init__(self, server):
        self.server = server
        self.port = server.server_address[1]

    def call(self, method, path, body=None, headers=None, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
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

    def upload(self, data=SALES, name="sales.csv"):
        return self.json("POST", f"/api/upload?name={name}", data)

    def ask(self, text):
        return self.json(
            "POST",
            "/api/ask",
            json.dumps({"text": text}).encode(),
            {"Content-Type": "application/json"},
        )


@pytest.fixture
def client():
    server = make_server(0, limits=Limits(max_bytes=50_000))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield Client(server)
    server.shutdown()
    server.server_close()


def test_binds_loopback_only(client):
    assert client.server.server_address[0] == "127.0.0.1"


def test_page_and_security_headers(client):
    status, body, h = client.call("GET", "/")
    assert status == 200 and b"<title>Analysis workspace</title>" in body
    csp = h["content-security-policy"]
    assert (
        "default-src 'none'" in csp
        and "script-src 'sha256-" in csp
        and "'unsafe-inline'" not in csp
    )
    assert "frame-ancestors 'none'" in csp and "connect-src 'self'" in csp
    assert h["x-content-type-options"] == "nosniff" and h["cache-control"] == "no-store"
    assert page_bytes()[1] == csp


def test_page_has_no_external_reference_and_respects_reduced_motion():
    html = page_bytes()[0].decode()
    assert "http://" not in html and "https://" not in html
    assert "prefers-reduced-motion" in html and "prefers-color-scheme" in html
    assert "innerHTML" not in html and "eval(" not in html and "document.write" not in html


@pytest.mark.parametrize("host", ["evil.example", "127.0.0.1", "localhost", "127.0.0.1:1", "[::1]"])
def test_other_hosts_are_refused(client, host):
    assert client.call("GET", "/", host=host)[0] == 403
    assert (
        client.call("POST", "/api/ask", b"{}", {"Content-Type": "application/json"}, host=host)[0]
        == 403
    )


def test_localhost_name_is_accepted(client):
    assert client.call("GET", "/", host=f"localhost:{client.port}")[0] == 200


def test_cross_origin_writes_are_refused(client):
    for origin in ("http://evil.example", "http://127.0.0.1:1", "null"):
        status, _, _ = client.call("POST", "/api/upload", SALES, {"Origin": origin})
        assert status == 403
        assert client.call("DELETE", "/api/session", headers={"Origin": origin})[0] == 403
    assert client.json("GET", "/api/state")[1] == {
        "loaded": False
    }  # nothing was loaded by those calls


def test_same_origin_write_is_accepted(client):
    origin = f"http://127.0.0.1:{client.port}"
    assert client.call("POST", "/api/upload?name=a.csv", SALES, {"Origin": origin})[0] == 200


def test_no_file_means_not_loaded_or_404_not_a_crash(client):
    assert client.json("GET", "/api/state") == (200, {"loaded": False})
    assert client.call("GET", "/api/export/report.html")[0] == 404
    assert client.call("GET", "/api/export/a1.csv")[0] == 404
    assert client.ask("Sum revenue by region")[0] == 404


def test_upload_state_ask_and_history(client):
    status, state = client.upload()
    assert status == 200 and state["source"]["rows"] == 8 and state["source"]["name"] == "sales.csv"
    assert {c["label"] for c in state["cards"]} >= {"Rows", "Columns"}
    assert all(a["origin"] == "starter" for a in state["analyses"])
    status, out = client.ask("Compare revenue by region")
    assert status == 200 and out["step"]["kind"] == "plan"
    asked = [a for a in out["state"]["analyses"] if a["origin"] == "asked"]
    assert len(asked) == 1 and asked[0]["request"] == "Compare revenue by region"
    table = asked[0]["table"]
    assert table["columns"][0] == "region"
    assert dict(zip((r[0] for r in table["rows"]), (r[-1] for r in table["rows"]), strict=True)) == {
        "South": "110", "North": "70", "(null)": "40", "East": "10",
    }  # fmt: skip
    assert asked[0]["image"].startswith("data:image/svg+xml;base64,")
    assert client.json("GET", "/api/state")[1]["session_sha256"] == out["state"]["session_sha256"]


def test_clarification_and_unsupported_come_back_as_steps(client):
    client.upload()
    _, out = client.ask("Sum revenue")
    assert out["step"]["kind"] == "clarification" and out["step"]["options"]
    for option in out["step"]["options"]:
        assert client.ask(option)[1]["step"]["kind"] == "plan"
    assert client.ask("DROP TABLE x; --")[1]["step"]["kind"] == "unsupported"


def test_exports_match_the_session(client):
    client.upload()
    _, out = client.ask("Compare revenue by region")
    aid = next(a["id"] for a in out["state"]["analyses"] if a["origin"] == "asked")
    status, csv_bytes, h = client.call("GET", f"/api/export/{aid}.csv")
    assert status == 200 and h["content-type"].startswith("text/csv")
    assert csv_bytes.decode().splitlines()[0].startswith("region,")
    status, xlsx, h = client.call("GET", f"/api/export/{aid}.xlsx")
    assert status == 200 and zipfile.ZipFile(io.BytesIO(xlsx)).testzip() is None
    status, svg, h = client.call("GET", f"/api/export/{aid}.svg")
    assert status == 200 and h["content-type"] == "image/svg+xml" and svg.startswith(b"<svg")
    assert "attachment" in h["content-disposition"]
    status, rep, h = client.call("GET", "/api/export/report.html")
    assert status == 200 and b"Compare revenue by region" in rep and b"<script" not in rep
    # byte-identical on a second request: deterministic
    assert client.call("GET", f"/api/export/{aid}.csv")[1] == csv_bytes
    assert client.call("GET", "/api/export/report.html")[1] == rep


def test_table_analysis_has_no_svg_export(client):
    client.upload()
    client.ask("Compare revenue by region")
    _, out = client.ask("Show as table")
    aid = [a["id"] for a in out["state"]["analyses"] if a["origin"] == "asked"][-1]
    assert client.call("GET", f"/api/export/{aid}.svg")[0] == 404
    assert client.call("GET", f"/api/export/{aid}.csv")[0] == 200


def test_unverified_analysis_is_neither_shown_nor_exported(client, monkeypatch):
    monkeypatch.setattr(
        session_module,
        "verify_result",
        lambda ds, spec, table: Verification("unverified", 1, ["forced"]),
    )
    client.upload()
    _, out = client.ask("Compare revenue by region")
    asked = [a for a in out["state"]["analyses"] if a["origin"] == "asked"][0]
    assert asked["exportable"] is False and asked["table"] is None and asked["image"] is None
    assert client.call("GET", f"/api/export/{asked['id']}.csv")[0] == 409
    assert client.call("GET", f"/api/export/{asked['id']}.xlsx")[0] == 409
    rep = client.call("GET", "/api/export/report.html")[1].decode()
    assert "Not included" in rep and "forced" in rep  # the reason is shown, the numbers are not
    assert '<td class="n">110</td>' not in rep


@pytest.mark.parametrize(
    "path",
    ["/api/export/../../etc/passwd.csv", "/api/export/a1.exe", "/api/export/%2e%2e/x.csv",
     "/etc/passwd", "/static/../app.py", "/api/export/", "/api/export/a1.csv/", "//evil.example/x",
     "/api/export/" + "a" * 40 + ".csv"],
)  # fmt: skip
def test_only_fixed_routes_exist(client, path):
    client.upload()
    assert client.call("GET", path)[0] in (404, 400)


def test_no_path_is_ever_read_from_a_request(client, tmp_path):
    secret = tmp_path / "secret.csv"
    secret.write_bytes(b"a,b\n1,2\n")
    status, state = client.upload(name=str(secret).replace("/", "%2F"))
    assert (
        status == 200 and state["source"]["name"] == "secret.csv"
    )  # only the last part, as a label


def test_safe_label():
    assert safe_label("../../etc/passwd") == "passwd"
    assert safe_label("C:\\Users\\me\\x.csv") == "x.csv"
    assert (
        safe_label("<script>a</script>.csv") == "script_.csv"
    )  # the last path part, markup characters replaced
    assert "<" not in safe_label("<b>x</b>.csv") and ">" not in safe_label("<b>x</b>.csv")
    assert safe_label("") == "upload.csv" and len(safe_label("x" * 500)) == 120
    assert safe_label("a\x00b\nc.csv").isprintable()


def test_upload_size_cap_and_bad_files(client):
    big = b"a,b\n" + b"1,2\n" * 20_000  # over the 50 000 byte test cap
    assert client.upload(big)[0] == 413
    assert client.upload(b"a,b\n1,2,3\n")[0] == 422
    assert client.upload(b"")[0] == 422
    assert client.upload(b"a,b\n1,2\x00\n")[0] == 422
    status, body, _ = client.call("POST", "/api/upload", None, {"Content-Length": "abc"})
    assert status == 411
    assert client.json("GET", "/api/state")[1] == {
        "loaded": False
    }  # failed uploads leave no session


def test_failed_upload_keeps_the_previous_session(client):
    client.upload()
    assert client.upload(b"a,b\n1,2,3\n")[0] == 422
    assert client.json("GET", "/api/state")[1]["source"]["name"] == "sales.csv"


def test_ask_input_validation(client):
    client.upload()
    json_h = {"Content-Type": "application/json"}
    assert client.call("POST", "/api/ask", b"{}", {"Content-Type": "text/plain"})[0] == 415
    assert client.call("POST", "/api/ask", b"not json", json_h)[0] == 400
    assert client.call("POST", "/api/ask", b'{"text": 5}', json_h)[0] == 400
    assert client.call("POST", "/api/ask", b'["x"]', json_h)[0] == 400
    assert client.call("POST", "/api/ask", b"x" * (MAX_REQUEST_BYTES + 1), json_h)[0] == 413
    assert client.call("POST", "/api/ask", b"\xff\xfe", json_h)[0] == 400


def test_other_methods_and_routes(client):
    assert client.call("PUT", "/api/ask", b"{}")[0] == 405
    assert client.call("OPTIONS", "/api/ask")[0] == 405
    assert client.call("POST", "/api/nothing", b"")[0] == 404
    assert client.call("DELETE", "/api/other")[0] == 404


def test_delete_discards_everything(client):
    client.upload()
    client.ask("Compare revenue by region")
    assert client.json("DELETE", "/api/session")[1] == {"deleted": True}
    assert client.json("GET", "/api/state")[1] == {"loaded": False}
    assert client.call("GET", "/api/export/report.html")[0] == 404


def test_markup_in_column_names_and_values_is_escaped_everywhere(client):
    evil = b'"<img src=x onerror=alert(1)>","a&b","<script>x</script>"\nr1,5,2025-01-01\nr2,7,2025-02-01\n'
    status, state = client.upload(evil, name="<b>x</b>.csv")
    assert status == 200
    assert "<" not in state["source"]["name"]
    out = client.ask('Sum "a&b" by "<img src=x onerror=alert(1)>"')[1]
    assert out["step"]["kind"] == "plan"
    rep = client.call("GET", "/api/export/report.html")[1].decode()
    assert "<img src=x" not in rep and "<script>x" not in rep and "onerror=alert(1)>" not in rep
    assert "&lt;img src=x onerror=alert(1)&gt;" in rep
    svgs = [a for a in out["state"]["analyses"] if a["image"]]
    import base64

    for a in svgs:
        raw = base64.b64decode(a["image"].split(",", 1)[1]).decode()
        assert "<img" not in raw and "<script" not in raw


def test_report_is_deterministic_and_script_free(client):
    client.upload()
    client.ask("Trend of revenue by month")
    a = client.call("GET", "/api/export/report.html")[1]
    b = client.call("GET", "/api/export/report.html")[1]
    assert a == b and b"<script" not in a and b"http://" not in a and b"https://" not in a
    assert b"Content-Security-Policy" in a and b"Limitations" in a and b"Request history" in a


def test_nothing_is_written_to_disk(client, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client.upload()
    client.ask("Compare revenue by region")
    client.call("GET", "/api/export/report.html")
    assert list(tmp_path.iterdir()) == []


def test_report_module_pure_and_offline():
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(report_module))
    mods = {
        a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names
    }
    mods |= {
        n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module
    }
    assert not mods & {"socket", "urllib", "http", "requests", "httpx", "subprocess", "os"}


def test_crlf_checkout_gets_the_same_page_and_hashes():
    """Windows git (core.autocrlf) turns the page into CRLF; browsers hash inline script/style with LF."""
    import base64
    import hashlib
    import re

    lf = page_bytes()[0]
    for crlf in (lf.replace(b"\n", b"\r\n"), lf.replace(b"\n", b"\r")):
        html, csp = prepare_page(crlf)
        assert html == lf and csp == page_bytes()[1] and b"\r" not in html
    # the hash in the policy is the hash of the text exactly as a browser sees it (LF)
    text = lf.decode()
    for tag in ("script", "style"):
        for body in re.findall(rf"<{tag}[^>]*>(.*?)</{tag}>", text, flags=re.S):
            digest = base64.b64encode(hashlib.sha256(body.encode()).digest()).decode()
            assert f"'sha256-{digest}'" in page_bytes()[1]
