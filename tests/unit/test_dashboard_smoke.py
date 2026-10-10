"""The dashboard smoke test must pass on the real page and fail on broken ones.

Needs Playwright and Chromium. Skipped without them, unless REQUIRE_BROWSER=1 (CI sets it), in which
case a missing browser is a failure so the test cannot silently stop running.
"""

from __future__ import annotations

import os
import re
from dataclasses import replace
from pathlib import Path

import pytest

from dashboard_smoke import DEFAULT_CSV, Expected, run
from sie.dashboard import CAPTURED_ISO, build

ROOT = Path(__file__).resolve().parents[2]
REQUIRED = os.environ.get("REQUIRE_BROWSER") == "1"


def _need_browser() -> None:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            p.chromium.launch().close()
    except Exception as exc:  # noqa: BLE001
        if REQUIRED:
            pytest.fail(f"REQUIRE_BROWSER=1 but no browser is available: {exc}")
        pytest.skip(f"no Playwright/Chromium: {exc}")


@pytest.fixture(scope="module", autouse=True)
def browser_available():
    _need_browser()


@pytest.fixture(scope="module")
def expected() -> Expected:
    return Expected.from_csv(DEFAULT_CSV)


@pytest.fixture(scope="module")
def built(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("built") / "index.html"
    build(DEFAULT_CSV, CAPTURED_ISO, out, Expected.from_csv(DEFAULT_CSV).events)
    return out


def inject(html: str, snippet: str) -> str:
    return html.replace("</html>", snippet + "</html>")


def variant(built: Path, tmp_path: Path, edit) -> str:
    out = tmp_path / "index.html"
    out.write_text(edit(built.read_text(encoding="utf-8")), encoding="utf-8")
    return out.as_uri()


def test_the_page_built_from_the_template_passes(built, expected):
    assert run(built.as_uri(), expected) == []


def test_the_committed_site_passes(expected):
    """site/index.html is what gets deployed; it must match the data too."""
    assert run((ROOT / "site/index.html").as_uri(), expected) == []


def test_a_blank_page_fails(built, expected, tmp_path):
    url = variant(built, tmp_path, lambda h: re.sub(r"<script.*?</script>", "", h, flags=re.S))
    problems = run(url, expected)
    assert any("blank or near-empty" in p for p in problems)
    assert any("navigation is" in p for p in problems)


def test_the_javascript_syntax_error_that_pr_14_fixed_fails(built, expected, tmp_path):
    """An unescaped apostrophe in a string literal blanked the whole page in production."""
    url = variant(built, tmp_path, lambda h: inject(h, "<script>const x = 'it's';</script>"))
    assert any("page error" in p for p in run(url, expected))


def _with(built: Path, tmp_path: Path, snippet: str) -> str:
    return variant(built, tmp_path, lambda h: inject(h, snippet))


def test_a_runtime_error_on_load_fails(built, expected, tmp_path):
    url = _with(built, tmp_path, "<script>null.x</script>")
    assert any("page error" in p for p in run(url, expected))


def test_a_console_error_fails(built, expected, tmp_path):
    url = _with(built, tmp_path, "<script>console.error('boom')</script>")
    assert any("console error: boom" in p for p in run(url, expected))


def test_a_request_leaving_the_page_fails(built, expected, tmp_path):
    url = _with(built, tmp_path, '<script src="https://example.invalid/x.js"></script>')
    problems = run(url, expected)
    assert any("left the page" in p for p in problems)
    assert any("request failed" in p for p in problems)


def test_horizontal_overflow_on_mobile_fails(built, expected, tmp_path):
    url = _with(built, tmp_path, '<div style="width:2000px;height:1px"></div>')
    assert any("horizontal overflow" in p and "mobile" in p for p in run(url, expected))


def test_numbers_that_disagree_with_the_data_fail(built, expected):
    problems = run(
        built.as_uri(),
        replace(expected, medals=expected.medals + 1, top_total=expected.top_total + 1),
    )
    assert any("does not show" in p and "medals" in p for p in problems)
    assert any("top row is" in p for p in problems)


def test_a_missing_section_fails(built, expected, tmp_path):
    url = variant(built, tmp_path, lambda h: h.replace("Validation evidence", "Evidence"))
    assert any("missing heading: 'Validation evidence'" in p for p in run(url, expected))


def test_the_dark_scheme_is_really_dark(built, expected, tmp_path):
    css = "@media(prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#fff}}"
    url = variant(built, tmp_path, lambda h: h.replace("</style>", css + "</style>", 1))
    assert any("does not look dark" in p for p in run(url, expected))
