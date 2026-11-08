"""Browser smoke test of the workspace page: the real page, a real server, desktop and mobile, light and
dark. Needs Playwright and Chromium; skipped without them unless REQUIRE_BROWSER=1 (CI sets it)."""

from __future__ import annotations

import os
import threading

import pytest
from test_workspace_core import SALES

from sie.workspace_server import make_server

REQUIRED = os.environ.get("REQUIRE_BROWSER") == "1"
VIEWPORTS = {"desktop": {"width": 1280, "height": 900}, "mobile": {"width": 390, "height": 844}}
EVIL = (
    b'"<img src=x onerror=window.__xss=1>","amount","<b>when</b>"\n'
    b"r1,5,2025-01-01\nr2,7,2025-02-01\n"
)


@pytest.fixture(scope="module")
def browser():
    try:
        from playwright.sync_api import sync_playwright

        p = sync_playwright().start()
        b = p.chromium.launch()
    except Exception as exc:  # noqa: BLE001
        if REQUIRED:
            pytest.fail(f"REQUIRE_BROWSER=1 but no browser is available: {exc}")
        pytest.skip(f"no Playwright/Chromium: {exc}")
    yield b
    b.close()
    p.stop()


@pytest.fixture
def base():
    server = make_server(0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def open_page(browser, base, vname, scheme, **ctx):
    context = browser.new_context(
        viewport=VIEWPORTS[vname], color_scheme=scheme, is_mobile=vname == "mobile", **ctx
    )
    page = context.new_page()
    page.set_default_timeout(5000)
    problems: list[str] = []
    page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
    page.on(
        "console",
        lambda m: problems.append(f"console {m.type}: {m.text}") if m.type == "error" else None,
    )
    page.on("dialog", lambda d: (problems.append(f"dialog: {d.message}"), d.dismiss()))
    page.on(
        "request",
        lambda r: (
            problems.append(f"outside request: {r.url}")
            if not (r.url.startswith(base) or r.url.startswith("data:"))
            else None
        ),
    )
    page.on(
        "response",
        lambda r: problems.append(f"HTTP {r.status}: {r.url}") if r.status >= 400 else None,
    )
    page.goto(base + "/")
    return context, page, problems


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("vname", ["desktop", "mobile"])
def test_the_whole_journey_in_the_browser(browser, base, tmp_path, vname, scheme):
    context, page, problems = open_page(browser, base, vname, scheme)
    csv = tmp_path / "sales.csv"
    csv.write_bytes(SALES)
    assert page.locator("#work").is_hidden()
    page.set_input_files("#file", str(csv))
    page.wait_for_selector("#cards .card")
    assert page.locator("#work").is_visible()
    assert "sales.csv" in page.inner_text("#srcinfo") and "8 rows" in page.inner_text("#srcinfo")
    assert page.locator("#cards .card").count() >= 4
    assert page.locator("#analyses img").count() >= 1
    assert page.locator("#findings li").count() >= 1
    # a request that plans
    page.fill("#ask", "Compare revenue by region")
    page.click("#askform button[type=submit]")
    page.wait_for_selector("#answer .msg.ok")
    assert "Request: Compare revenue by region" in page.inner_text("#analyses")
    assert "110" in page.inner_text("#analyses")
    # a request that needs a clarification, answered by clicking an option
    page.fill("#ask", "Sum revenue")
    page.click("#askform button[type=submit]")
    page.wait_for_selector("#answer .msg.ask button")
    page.click("#answer .msg.ask button >> nth=0")
    page.wait_for_selector("#answer .msg.ok")
    # an unsupported request is shown, not executed
    page.fill("#ask", "Sum revenue by region; DROP TABLE sales")
    page.click("#askform button[type=submit]")
    page.wait_for_selector("#answer .msg.error")
    # exports are real downloads
    with page.expect_download() as dl:
        page.click("#analyses a:has-text('CSV') >> nth=0")
    assert dl.value.suggested_filename.endswith(".csv")
    with page.expect_download() as dl:
        page.click("text=Download report (HTML)")
    assert dl.value.suggested_filename == "report.html"
    # layout and theme
    overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    assert overflow <= 0, f"horizontal overflow of {overflow}px"
    bg = page.evaluate("getComputedStyle(document.body).backgroundColor")
    r, g, b = (int(v) for v in bg.replace("rgb(", "").replace(")", "").split(",")[:3])
    assert (r + g + b) / 3 < 100 if scheme == "dark" else (r + g + b) / 3 > 200
    # reload keeps the session (state is on the server), then delete removes it
    page.reload()
    page.wait_for_selector("#cards .card")
    page.click("#del")
    page.wait_for_selector("#work", state="hidden")
    page.reload()
    assert page.locator("#work").is_hidden()
    assert problems == []
    context.close()


def test_markup_in_data_never_runs(browser, base, tmp_path):
    context, page, problems = open_page(browser, base, "desktop", "light")
    f = tmp_path / "evil.csv"
    f.write_bytes(EVIL)
    page.set_input_files("#file", str(f))
    page.wait_for_selector("#cards .card")
    page.fill("#ask", 'Sum amount by "<img src=x onerror=window.__xss=1>"')
    page.click("#askform button[type=submit]")
    page.wait_for_selector("#answer .msg")
    assert page.evaluate("window.__xss") is None
    assert page.locator("#analyses img[src^='x']").count() == 0
    assert "<img src=x onerror" in page.inner_text("#inspect") + page.inner_text(
        "#analyses"
    )  # shown as text
    assert problems == []
    context.close()


def test_unusable_file_is_reported_and_nothing_is_shown(browser, base, tmp_path):
    context, page, problems = open_page(browser, base, "desktop", "light")
    f = tmp_path / "bad.csv"
    f.write_bytes(b"a,b\n1,2,3\n")
    page.set_input_files("#file", str(f))
    page.wait_for_selector("#msg .msg.error")
    assert page.locator("#work").is_hidden()
    assert [p for p in problems if "422" not in p] == []
    context.close()


def test_reduced_motion_is_honoured(browser, base):
    context, page, _ = open_page(browser, base, "desktop", "light", reduced_motion="reduce")
    assert page.evaluate("matchMedia('(prefers-reduced-motion: reduce)').matches")
    duration = page.evaluate(
        "getComputedStyle(document.querySelector('button')).transitionDuration"
    )
    assert duration in ("0s", "0.000001s", "1e-06s"), duration
    context.close()


def test_keyboard_reaches_the_main_controls(browser, base):
    context, page, _ = open_page(browser, base, "desktop", "light")
    page.keyboard.press("Tab")
    assert page.evaluate("document.activeElement && document.activeElement.id") == "file"
    context.close()


def test_page_works_when_the_file_was_checked_out_with_crlf(browser, base, tmp_path, monkeypatch):
    """Windows ``core.autocrlf`` rewrites the page with CRLF; the CSP hashes must still match in Chromium."""
    from sie.workspace_server import _PAGE, page_bytes, prepare_page

    lf = page_bytes()[0]
    monkeypatch.setitem(_PAGE, "html", None)
    html, csp = prepare_page(lf.replace(b"\n", b"\r\n"))
    monkeypatch.setitem(_PAGE, "html", html)
    monkeypatch.setitem(_PAGE, "csp", csp)
    context, page, problems = open_page(browser, base, "desktop", "light")
    f = tmp_path / "sales.csv"
    f.write_bytes(SALES)
    page.set_input_files("#file", str(f))
    page.wait_for_selector("#cards .card")
    assert problems == []
    context.close()
