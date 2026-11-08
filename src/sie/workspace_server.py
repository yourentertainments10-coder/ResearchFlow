"""Local server for the analysis workspace page: ``sie workspace serve``.

Standard library only. Binds 127.0.0.1 (a non-loopback bind address is refused), serves a fixed set of
routes, never reads a path or file name from a request, and keeps the uploaded data in memory only (nothing
is written to disk; ``DELETE /api/session`` or stopping the server discards it). Every response carries a
strict Content-Security-Policy; the page's one inline script and style are allowed by hash, nothing else.

Routes
  GET    /                       the page
  GET    /api/state              session view (JSON); {"loaded": false} when no file is loaded
  POST   /api/upload?name=...    raw file bytes; a CSV beyond the normal limits is read in one bounded
                                 pass and sampled (up to 2 GiB); replaces the session
  POST   /api/ask                {"text": "..."}  -> the step and the new state
  POST   /api/clean              {"step": {...}}  one approved cleaning step -> the step and the new state
  POST   /api/clean/undo         remove the last approved cleaning step
  POST   /api/model              {"target": "column" | null}  compare models (null = find groups)
  GET    /api/export/<id>.csv|.xlsx|.svg   one verified analysis (unverified ones are refused)
  GET    /api/export/report.html the self-contained report
  DELETE /api/session            discard everything

Requests whose Host is not this server's loopback address are refused (DNS-rebinding guard); a POST or
DELETE must be same-origin if it sends an Origin header at all.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Any
from urllib.parse import parse_qs, urlsplit

from sie.datasets import DatasetError, Limits
from sie.datasets.export import table_csv_bytes, table_xlsx_bytes
from sie.workspace import Session
from sie.workspace.report import build_report
from sie.workspace.sampling import MAX_STREAM_BYTES
from sie.workspace.session import open_stream
from sie.workspace.view import session_view

LOOPBACK = "127.0.0.1"
MAX_REQUEST_BYTES = 8 * 1024
MAX_NAME_CHARS = 120
_EXPORT = re.compile(r"^/api/export/(?P<id>[A-Za-z0-9_]{1,16})\.(?P<ext>csv|xlsx|svg)$")
_PAGE: dict[str, Any] = {}


def prepare_page(raw: bytes) -> tuple[bytes, str]:
    """The page with LF line endings and its Content-Security-Policy.

    Browsers normalise CR/LF to LF inside inline script and style before checking a CSP hash, so the hash
    must be computed on LF text, and the page is served as that same LF text. A checkout with CRLF line
    endings (Windows ``core.autocrlf``) therefore still matches."""
    text = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    hashes: dict[str, list[str]] = {"script": [], "style": []}
    for tag in hashes:
        for body in re.findall(rf"<{tag}[^>]*>(.*?)</{tag}>", text, flags=re.S):
            digest = base64.b64encode(hashlib.sha256(body.encode("utf-8")).digest()).decode()
            hashes[tag].append(f"'sha256-{digest}'")
    csp = (
        f"default-src 'none'; script-src {' '.join(hashes['script'])}; "
        f"style-src {' '.join(hashes['style'])}; img-src data:; "
        "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    )
    return text.encode("utf-8"), csp


def page_bytes() -> tuple[bytes, str]:
    """The page and its Content-Security-Policy (inline script and style allowed by hash only)."""
    if not _PAGE:
        html, csp = prepare_page(resources.files("sie.web").joinpath("workspace.html").read_bytes())
        _PAGE.update(html=html, csp=csp)
    return _PAGE["html"], _PAGE["csp"]


def safe_label(raw: str) -> str:
    """A display name for the upload: the last path part, printable characters only, cut short. It is
    only ever shown (escaped) and recorded as data; it never becomes a file path."""
    last = raw.replace("\\", "/").rsplit("/", 1)[-1]
    clean = "".join(c if c.isprintable() and c not in '<>"' else "_" for c in last).strip()
    return (clean or "upload.csv")[:MAX_NAME_CHARS]


class _BodyStream:
    """Exactly ``size`` bytes of a request body as a readable stream (never more, never buffered whole)."""

    def __init__(self, rfile: Any, size: int) -> None:
        self._rfile, self.remaining = rfile, size

    @property
    def exhausted(self) -> bool:
        return self.remaining == 0

    def read(self, n: int = -1) -> bytes:
        if self.remaining <= 0:
            return b""
        n = self.remaining if n is None or n < 0 else min(n, self.remaining)
        chunk = self._rfile.read(n)
        self.remaining -= len(chunk)
        if not chunk:
            self.remaining = 0
        return chunk


class WorkspaceState:
    """The one in-memory session of this server, guarded by a lock."""

    def __init__(self, limits: Limits | None = None, stream_limit: int = MAX_STREAM_BYTES) -> None:
        self.limits = limits or Limits()
        self.stream_limit = stream_limit
        self.session: Session | None = None
        self.lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    server_version = "ResearchFlowWorkspace"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # -- plumbing ----------------------------------------------------------------------------------
    @property
    def state(self) -> WorkspaceState:
        return self.server.state  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - quiet by default
        return

    def _send(
        self, status: int, body: bytes, ctype: str, extra: dict[str, str] | None = None
    ) -> None:
        _, csp = page_bytes()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Security-Policy", csp)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, obj: Any) -> None:
        body = json.dumps(obj, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        if self.command in {"POST", "PUT", "PATCH", "DELETE", "OPTIONS"}:
            self.close_connection = True  # the body may be unread; do not reuse the connection
        self._json(status, {"error": message})

    def _host_ok(self) -> bool:
        port = self.server.server_address[1]
        return self.headers.get("Host", "") in {f"{LOOPBACK}:{port}", f"localhost:{port}"}

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        port = self.server.server_address[1]
        return origin in {f"http://{LOOPBACK}:{port}", f"http://localhost:{port}"}

    def _guard(self, *, writes: bool) -> bool:
        if not self._host_ok():
            self._error(403, "This server answers only on its own loopback address.")
            return False
        if writes and not self._origin_ok():
            self._error(403, "Cross-origin requests are refused.")
            return False
        return True

    def _body(self, limit: int) -> bytes | None:
        raw = self.headers.get("Content-Length")
        if raw is None or not raw.isdigit():
            self._error(411, "A Content-Length header is required.")
            return None
        size = int(raw)
        if size > limit:
            self._error(413, f"The request is larger than {limit} bytes.")
            self.close_connection = True
            return None
        return self.rfile.read(size)

    # -- verbs -------------------------------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        if not self._guard(writes=False):
            return
        parts = urlsplit(self.path)
        path = parts.path
        if path == "/":
            html, _ = page_bytes()
            self._send(200, html, "text/html; charset=utf-8")
        elif path == "/api/state":
            with self.state.lock:
                session = self.state.session
                view = session_view(session) if session else None
            self._json(200, view if view is not None else {"loaded": False})
        elif path == "/api/export/report.html":
            with self.state.lock:
                session = self.state.session
                body = build_report(session).encode("utf-8") if session else None
            if body is None:
                self._error(404, "No file is loaded yet.")
            else:
                self._send(
                    200, body, "text/html; charset=utf-8",
                    {"Content-Disposition": 'attachment; filename="report.html"'},
                )  # fmt: skip
        elif m := _EXPORT.match(path):
            self._export(m["id"], m["ext"])
        else:
            self._error(404, "Not found.")

    do_HEAD = do_GET  # noqa: N815

    def _export(self, item_id: str, ext: str) -> None:
        with self.state.lock:
            session = self.state.session
            items = [*session.workspace.items, *session.items] if session else []
            item = next((i for i in items if i.planned.id == item_id), None)
            if session is None or item is None:
                self._error(404, "No such analysis.")
                return
            if not item.exportable:
                self._error(
                    409, "This analysis did not pass its independent check and is not exported."
                )
                return
            if ext == "csv":
                body, _ = table_csv_bytes(item.result.table)
                ctype = "text/csv; charset=utf-8"
            elif ext == "xlsx":
                body, _ = table_xlsx_bytes(item.result.table, item.planned.title)
                ctype = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            else:
                if not item.svg:
                    self._error(404, "This analysis is a table and has no chart.")
                    return
                body, ctype = item.svg.encode("utf-8"), "image/svg+xml"
        self._send(
            200,
            body,
            ctype,
            {"Content-Disposition": f'attachment; filename="analysis_{item_id}.{ext}"'},
        )

    def do_POST(self) -> None:  # noqa: N802
        if not self._guard(writes=True):
            return
        parts = urlsplit(self.path)
        if parts.path == "/api/upload":
            self._upload(parse_qs(parts.query))
        elif parts.path == "/api/ask":
            self._ask()
        elif parts.path == "/api/clean":
            self._clean()
        elif parts.path == "/api/clean/undo":
            self._undo()
        elif parts.path == "/api/model":
            self._model()
        else:
            self._error(404, "Not found.")

    def _upload(self, query: dict[str, list[str]]) -> None:
        raw = self.headers.get("Content-Length")
        if raw is None or not raw.isdigit():
            self._error(411, "A Content-Length header is required.")
            return
        size = int(raw)
        name = safe_label(query.get("name", ["upload.csv"])[0])
        if size > self.state.stream_limit:
            self._error(413, f"The file is larger than {self.state.stream_limit} bytes.")
            self.close_connection = True
            return
        # up to the normal limit the file is read whole; a larger CSV is read in one bounded pass and the
        # session runs on a seeded random sample (stated on the page); the body is never held whole
        stream = _BodyStream(self.rfile, size)
        try:
            session = open_stream(
                stream, name, limits=self.state.limits, max_stream_bytes=self.state.stream_limit
            )
        except DatasetError as exc:
            if stream.remaining and not stream.exhausted:
                self.close_connection = True  # unread body left on the connection
            self._error(422, f"This file cannot be used: {exc}")
            return
        with self.state.lock:
            self.state.session = session
            view = session_view(session)
        self._json(200, view)

    def _ask(self) -> None:
        if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
            self._error(415, "Send application/json.")
            return
        body = self._body(MAX_REQUEST_BYTES)
        if body is None:
            return
        try:
            payload = json.loads(body.decode("utf-8"))
            text = payload["text"]
        except (ValueError, KeyError, TypeError):
            self._error(400, 'Send {"text": "..."}.')
            return
        if not isinstance(text, str):
            self._error(400, '"text" must be a string.')
            return
        with self.state.lock:
            session = self.state.session
            if session is None:
                self._error(404, "No file is loaded yet.")
                return
            step = session.ask(text)
            view = session_view(session)
        self._json(200, {"step": step.to_dict(), "state": view})

    def _clean(self) -> None:
        if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
            self._error(415, "Send application/json.")
            return
        body = self._body(MAX_REQUEST_BYTES)
        if body is None:
            return
        try:
            step = json.loads(body.decode("utf-8"))["step"]
        except (ValueError, KeyError, TypeError):
            self._error(400, 'Send {"step": {...}}.')
            return
        with self.state.lock:
            session = self.state.session
            if session is None:
                self._error(404, "No file is loaded yet.")
                return
            result = session.clean(step)
            view = session_view(session)
        self._json(200, {"step": result.to_dict(), "state": view})

    def _model(self) -> None:
        if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
            self._error(415, "Send application/json.")
            return
        body = self._body(MAX_REQUEST_BYTES)
        if body is None:
            return
        try:
            doc = json.loads(body.decode("utf-8"))
            target = doc["target"]
            time_column = doc.get("time")
        except (ValueError, KeyError, TypeError, AttributeError):
            self._error(400, 'Send {"target": "column name" or null}.')
            return
        if target is not None and (
            not isinstance(target, str) or not target.strip() or len(target) > 200
        ):
            self._error(400, '"target" must be a column name or null.')
            return
        if time_column is not None and (
            not isinstance(time_column, str) or not time_column.strip() or len(time_column) > 200
        ):
            self._error(400, '"time" must be a date column name or null.')
            return
        with self.state.lock:
            session = self.state.session
            if session is None:
                self._error(404, "No file is loaded yet.")
                return
            if target is None:
                request = "Find groups"
            elif time_column is None:
                request = f"Predict {target}"
            else:
                request = f"Predict {target} over time using {time_column}"
            step = session.ask(request)
            view = session_view(session)
        self._json(200, {"step": step.to_dict(), "state": view})

    def _undo(self) -> None:
        if self._body(MAX_REQUEST_BYTES) is None:
            return
        with self.state.lock:
            session = self.state.session
            if session is None:
                self._error(404, "No file is loaded yet.")
                return
            result = session.undo_clean()
            view = session_view(session)
        self._json(200, {"step": result.to_dict(), "state": view})

    def do_DELETE(self) -> None:  # noqa: N802
        if not self._guard(writes=True):
            return
        if urlsplit(self.path).path != "/api/session":
            self._error(404, "Not found.")
            return
        with self.state.lock:
            self.state.session = None
        self._json(200, {"deleted": True})

    def _method_not_allowed(self) -> None:
        self._error(405, "Method not allowed.")

    do_PUT = do_PATCH = do_OPTIONS = _method_not_allowed  # noqa: N815


def make_server(
    port: int = 8765, *, limits: Limits | None = None, stream_limit: int = MAX_STREAM_BYTES
) -> ThreadingHTTPServer:
    """A server bound to 127.0.0.1 only (``port=0`` picks a free port, used by the tests)."""
    server = ThreadingHTTPServer((LOOPBACK, port), Handler)
    server.state = WorkspaceState(limits, stream_limit)  # type: ignore[attr-defined]
    server.daemon_threads = True
    return server


__all__ = ["Handler", "WorkspaceState", "make_server", "safe_label"]
