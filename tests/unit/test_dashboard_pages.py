"""Open every dashboard page in a real browser. A page that throws is a failed test.

Skipped where Playwright or its Chromium is not installed (for example plain CI).
"""

from pathlib import Path

import pytest

from sie.dashboard import CAPTURED_ISO, build

ROOT = Path(__file__).resolve().parents[2]
ROUTES = ["overview", "countries", "sports", "gender", "timeline", "explorer", "method"]


@pytest.fixture(scope="module")
def page_file(tmp_path_factory):
    out = tmp_path_factory.mktemp("site") / "index.html"
    build(ROOT / "reports/placings.csv", CAPTURED_ISO, out, 469)
    return out


def test_every_page_renders_without_errors(page_file):
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"no Chromium available: {exc}")
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        for route in ROUTES:
            page.goto(f"{page_file.as_uri()}#/{route}")
            page.wait_for_timeout(150)
            assert "Couldn’t draw" not in page.inner_text("#app"), route
        browser.close()
    assert errors == []


def test_aliases_and_unknown_route(page_file):
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"no Chromium available: {exc}")
        page = browser.new_page()
        page.goto(f"{page_file.as_uri()}#/methodology")
        page.wait_for_timeout(150)
        assert "How these numbers are made" in page.inner_text("#app")
        page.goto(f"{page_file.as_uri()}#/nonsense")
        page.wait_for_timeout(150)
        assert "Page not found" in page.inner_text("#app")
        browser.close()
