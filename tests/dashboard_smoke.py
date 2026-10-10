"""Browser smoke test for the dashboard: a page that is blank, throws, or loads from elsewhere fails.

Used two ways:
* by pytest (``tests/unit/test_dashboard_smoke.py``) against a locally built page, and
* by hand against any URL, including the deployed site::

    python tests/dashboard_smoke.py --url https://researchflow.yourentertainments10.workers.dev/
    python tests/dashboard_smoke.py --url file:///.../site/index.html --screenshots /tmp/shots

Every expected number comes from ``reports/placings.csv`` (never typed in here), so the check is
"the page shows what the data says". Exit code 0 only if there are no problems.

Checks, for each route x viewport (desktop 1280, mobile 390) x colour scheme (light, dark):
page errors, console errors, failed or non-2xx requests, requests leaving the page's own origin,
a blank or near-empty ``#app``, the "could not draw" and "not found" fallbacks, the route's
headings, and on mobile no horizontal overflow. Once per page: the nav, the overview figures
against the data, the top country row, the theme colours and the theme toggle.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CSV = ROOT / "reports/placings.csv"

ROUTES: dict[str, list[str]] = {
    "overview": ["Medal table", "Where medals come from", "Medals won over time"],
    "countries": ["Which countries are strong where?", "Country detail", "Medal profile"],
    "sports": ["Which sports drive the medal distribution?", "Sport detail"],
    "gender": ["Who wins where: men, women and mixed events", "Medals and events by gender"],
    "timeline": ["How did the medal race unfold?", "Cumulative medals by day"],
    "explorer": ["Every medal, filterable"],
    "method": ["How these numbers are made", "Validation evidence", "Rules that matter", "Limits"],
}
NAV = ["Overview", "Countries", "Sports", "Gender", "Timeline", "Data Explorer", "Methodology"]
VIEWPORTS = {"desktop": {"width": 1280, "height": 900}, "mobile": {"width": 390, "height": 844}}
MIN_TEXT = 300  # characters of visible text a real route has; a blank page has none
SETTLE_MS = 300
TIMEOUT_MS = 4000  # a missing element is a finding, not a 30 second hang


@dataclass(frozen=True)
class Expected:
    medals: int
    events: int
    countries: int
    sports: int
    top_country: str
    top_total: int

    @staticmethod
    def from_csv(path: Path = DEFAULT_CSV) -> Expected:
        df = pd.read_csv(path)
        totals = df.groupby("country_name").size().sort_values(ascending=False, kind="stable")
        return Expected(
            medals=len(df),
            events=int(df["event_id"].nunique()),
            countries=int(df["country_code"].nunique()),
            sports=int(df["sport"].nunique()),
            top_country=str(totals.index[0]),
            top_total=int(totals.iloc[0]),
        )


def _luminance(css_rgb: str) -> float:
    r, g, b = (int(v) for v in re.findall(r"\d+", css_rgb)[:3])
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255


def run(url: str, expected: Expected, screenshots: Path | None = None, launch=None) -> list[str]:
    """Return the list of problems found (empty means the smoke test passed)."""
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    origin = urlparse(url)
    own = f"{origin.scheme}://{origin.netloc}" if origin.scheme != "file" else "file://"
    problems: list[str] = []
    with sync_playwright() as p:
        browser = (launch or p.chromium.launch)()
        for vname, viewport in VIEWPORTS.items():
            for scheme in ("light", "dark"):
                ctx = browser.new_context(
                    viewport=viewport,
                    color_scheme=scheme,
                    is_mobile=vname == "mobile",
                    has_touch=vname == "mobile",
                )
                page = ctx.new_page()
                page.set_default_timeout(TIMEOUT_MS)
                where = f"{vname}/{scheme}"
                _watch(page, where, problems, own)
                base = url.split("#")[0]
                for route, headings in ROUTES.items():
                    page.goto(f"{base}#/{route}")
                    page.wait_for_timeout(SETTLE_MS)
                    problems += _check_route(page, f"{where}/{route}", headings, vname)
                    if screenshots is not None:
                        screenshots.mkdir(parents=True, exist_ok=True)
                        page.screenshot(
                            path=str(screenshots / f"{vname}-{scheme}-{route}.png"), full_page=True
                        )
                try:
                    _check_page(page, base, where, scheme, expected, problems)
                except PlaywrightError as exc:
                    problems.append(f"[{where}] overview checks could not run: {str(exc)[:120]}")
                ctx.close()
        browser.close()
    return problems


def _watch(page, where: str, problems: list[str], own: str) -> None:
    """Record every error, failed or error response, and request that leaves the page's origin."""

    def note(message: str) -> None:
        problems.append(f"[{where}] {message}")

    def on_console(msg) -> None:
        if msg.type == "error":
            note(f"console error: {msg.text}")

    def on_response(resp) -> None:
        if resp.status >= 400:
            note(f"HTTP {resp.status}: {resp.url[:120]}")

    def on_request(req) -> None:
        if not req.url.startswith(own) and not req.url.startswith(("data:", "blob:")):
            note(f"left the page: {req.url[:120]}")

    page.on("pageerror", lambda e: note(f"page error: {e}"))
    page.on("console", on_console)
    page.on("requestfailed", lambda r: note(f"request failed: {r.url[:120]}"))
    page.on("response", on_response)
    page.on("request", on_request)


def _check_route(page, label: str, headings: list[str], vname: str) -> list[str]:
    out: list[str] = []
    try:
        text = page.inner_text("#app")
    except Exception as exc:  # noqa: BLE001  (no #app at all is a failure, not a crash)
        return [f"[{label}] #app missing: {str(exc)[:100]}"]
    if len(text.strip()) < MIN_TEXT:
        out.append(f"[{label}] blank or near-empty page ({len(text.strip())} characters)")
    if "Couldn’t draw" in text or "Couldn't draw" in text:
        out.append(f"[{label}] the page reports it could not draw")
    if "Page not found" in text:
        out.append(f"[{label}] route not found")
    out += [f"[{label}] missing heading: {h!r}" for h in headings if h not in text]
    if vname == "mobile":
        wide = page.evaluate(
            "document.documentElement.scrollWidth - document.documentElement.clientWidth"
        )
        if wide > 1:
            out.append(f"[{label}] horizontal overflow of {wide}px on mobile")
    return out


def _check_page(page, base: str, where: str, scheme: str, exp: Expected, out: list[str]) -> None:
    """Append findings to ``out`` as they are made, so an exception later keeps the earlier ones."""
    page.goto(f"{base}#/overview")
    page.wait_for_timeout(SETTLE_MS)
    nav = page.locator("#nav a").all_inner_texts()
    if [n.strip() for n in nav] != NAV:
        out.append(f"[{where}] navigation is {nav}, expected {NAV}")
    text = page.inner_text("#app")
    for label, value in (
        ("medals", f"{exp.medals:,}"),
        ("events", f"{exp.events:,}"),
        ("countries", f"{exp.countries:,}"),
    ):
        if not re.search(rf"(^|\n){re.escape(value)}\s*\n\s*{label}", text, re.I):
            out.append(f"[{where}] overview does not show {value} {label} (data says so)")
    out += _rows(page, base, where, "overview", min(10, exp.countries), "top-10 medal table")
    first = page.locator("#app table").first.locator("tr:has(td)").first.inner_text()
    if exp.top_country not in first or str(exp.top_total) not in first.split():
        out.append(f"[{where}] top row is {first!r}, data says {exp.top_country} {exp.top_total}")
    out += _rows(page, base, where, "countries", exp.countries, "country table")
    out += _rows(page, base, where, "sports", exp.sports, "sport table")
    out += _rows(page, base, where, "explorer", min(100, exp.medals), "first page of the explorer")
    page.goto(f"{base}#/overview")
    page.wait_for_timeout(SETTLE_MS)
    bg = page.evaluate("getComputedStyle(document.body).backgroundColor")
    lum = _luminance(bg)
    if (scheme == "dark") != (lum < 0.4):
        out.append(f"[{where}] background {bg} does not look {scheme}")
    before = page.evaluate("document.documentElement.dataset.theme || ''")
    page.click("#theme")
    page.wait_for_timeout(100)
    after = page.evaluate("document.documentElement.dataset.theme || ''")
    flipped = "light" if scheme == "dark" else "dark"
    if after != flipped:
        out.append(f"[{where}] theme toggle gave {after!r} from {before!r}, expected {flipped!r}")
    lum2 = _luminance(page.evaluate("getComputedStyle(document.body).backgroundColor"))
    if (lum2 < 0.4) == (scheme == "dark"):
        out.append(f"[{where}] theme toggle did not change the colours")


def _rows(page, base: str, where: str, route: str, want: int, what: str) -> list[str]:
    page.goto(f"{base}#/{route}")
    page.wait_for_timeout(SETTLE_MS)
    got = page.locator("#app table").first.locator("tr:has(td)").count()
    if got != want:
        return [f"[{where}/{route}] {what} has {got} rows, data says {want}"]
    return []


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--url", required=True, help="page to test (file:// or https://)")
    ap.add_argument("--csv", type=Path, default=DEFAULT_CSV, help="data the page must agree with")
    ap.add_argument("--screenshots", type=Path, help="save full-page screenshots here")
    args = ap.parse_args(argv)
    found = run(args.url, Expected.from_csv(args.csv), args.screenshots)
    for line in found:
        print(line)
    print(f"{len(found)} problem(s) found" if found else "dashboard smoke test passed")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
