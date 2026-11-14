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
    assert page.locator("#starter img").count() >= 1
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
    page.click("#t-report")
    with page.expect_download() as dl:
        page.click("text=Download report (HTML)")
    assert dl.value.suggested_filename == "report.html"
    # layout and theme
    overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    assert overflow <= 0, f"horizontal overflow of {overflow}px"
    for tab in ("overview", "clean", "explore", "predict", "report"):
        page.click(f"#t-{tab}")
        wide = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
        assert wide <= 0, f"horizontal overflow of {wide}px on the {tab} tab"
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


def test_cleaning_panel_applies_after_approval_undoes_and_asks_before_removing(
    browser, base, tmp_path
):
    context, page, problems = open_page(browser, base, "desktop", "light")
    f = tmp_path / "sales.csv"
    f.write_bytes(SALES)
    page.set_input_files("#file", str(f))
    page.wait_for_selector("#cards .card")
    page.click("#t-clean")
    assert page.locator("#cleanstate").inner_text().count("No cleaning applied yet.") == 1
    assert page.locator("#suggest li").count() >= 1  # suggested, not applied
    page.locator("#suggest li button.primary").first.click()
    page.wait_for_selector("#cleanstate ol li")
    assert "Independent check of the cleaning: verified" in page.inner_text("#cleanstate")
    done = page.inner_text("#cleanmsg")
    assert "were filled" in done or "was filled" in done
    page.click("text=Undo last step")
    page.wait_for_selector("text=No cleaning applied yet.")
    page.select_option("#cact", "drop_column")
    page.click("#cleanform button[type=submit]")  # the harness dismisses the confirmation
    assert "No cleaning applied yet." in page.inner_text("#cleanstate")
    assert [p for p in problems if not p.startswith("dialog: This removes data")] == []
    assert any(p.startswith("dialog: This removes data") for p in problems)
    context.close()


def test_model_panel_compares_models_and_finds_groups(browser, base, tmp_path):
    context, page, problems = open_page(browser, base, "desktop", "light")
    rows = ["id,sex,age,fare,survived"]
    for i in range(120):
        sex = "female" if i % 2 else "male"
        survived = 1 if (sex == "female") != (i % 9 == 0) else 0
        rows.append(f"{i + 1},{sex},{20 + i % 40},{10 + i % 70},{survived}")
    f = tmp_path / "people.csv"
    f.write_text("\n".join(rows) + "\n")
    page.set_input_files("#file", str(f))
    page.wait_for_selector("#cards .card")
    page.click("#t-predict")
    page.select_option("#mtarget", "survived")
    page.click("#modelform button[type=submit]")
    page.wait_for_selector("#models article")
    text = page.inner_text("#models")
    assert "Baseline (most frequent class)" in text and "(best)" in text
    assert "Columns that mattered most" in text and "Limits" in text
    assert "Predicting survived:" in page.inner_text("#modelmsg")
    page.select_option("#mtarget", "")
    page.click("#modelform button[type=submit]")
    page.locator("#models article").nth(1).wait_for()
    assert "Found" in page.inner_text("#modelmsg")
    assert problems == []
    context.close()


def load_sales(page, tmp_path):
    f = tmp_path / "sales.csv"
    f.write_bytes(SALES)
    page.set_input_files("#file", str(f))
    page.wait_for_selector("#cards .card")


def test_start_screen_offers_examples_and_a_drop_zone_and_loads_an_example(browser, base):
    context, page, problems = open_page(browser, base, "desktop", "light")
    assert page.locator("#start").is_visible() and page.locator("#drop").is_visible()
    page.wait_for_selector("#samples button")
    assert page.locator("#samples button").count() == 3
    assert "made-up" in page.inner_text("#start")
    page.click("#samples button >> nth=0")
    page.wait_for_selector("#cards .card")
    assert page.locator("#start").is_hidden()
    assert "example-sales.csv" in page.inner_text("#srcinfo")
    assert page.locator("#starter img").count() >= 1
    assert problems == []
    context.close()


def test_dropping_a_file_loads_it(browser, base):
    context, page, problems = open_page(browser, base, "desktop", "light")
    page.evaluate(
        """() => {
        const dt = new DataTransfer();
        dt.items.add(new File(["a,b\\n1,x\\n2,y\\n3,x\\n"], "dropped.csv", {type: "text/csv"}));
        document.getElementById("drop").dispatchEvent(new DragEvent("drop", {dataTransfer: dt, bubbles: true, cancelable: true}));
    }"""
    )
    page.wait_for_selector("#cards .card")
    assert "dropped.csv" in page.inner_text("#srcinfo")
    assert problems == []
    context.close()


def test_tabs_show_one_section_and_work_with_the_keyboard(browser, base, tmp_path):
    context, page, problems = open_page(browser, base, "desktop", "light")
    load_sales(page, tmp_path)
    assert page.locator("#p-overview").is_visible() and page.locator("#p-clean").is_hidden()
    page.focus("#t-overview")
    page.keyboard.press("ArrowRight")
    assert page.locator("#p-clean").is_visible() and page.locator("#p-overview").is_hidden()
    assert page.get_attribute("#t-clean", "aria-selected") == "true"
    page.keyboard.press("End")
    assert page.locator("#p-report").is_visible()
    assert problems == []
    context.close()


def test_a_request_from_any_tab_opens_the_new_answer_at_the_top_of_explore(browser, base, tmp_path):
    context, page, problems = open_page(browser, base, "desktop", "light")
    load_sales(page, tmp_path)
    page.click("#t-clean")
    page.fill("#ask", "Average revenue by product")
    page.click("#askform button[type=submit]")
    page.wait_for_selector("#answer .msg.ok")
    assert page.locator("#p-explore").is_visible()
    page.fill("#ask", "Median revenue by region")
    page.click("#askform button[type=submit]")
    page.locator("#analyses article").nth(1).wait_for()
    assert page.locator("#analyses article").count() == 2
    first = page.locator("#analyses article").first.inner_text()
    assert "Median of revenue by region" in first  # newest first
    assert problems == []
    context.close()


def test_the_ask_bar_stays_in_view_while_scrolling(browser, base, tmp_path):
    context, page, problems = open_page(browser, base, "desktop", "light")
    load_sales(page, tmp_path)
    page.mouse.wheel(0, 1500)
    page.wait_for_timeout(200)
    box = page.locator("#ask").bounding_box()
    assert box is not None and 0 <= box["y"] < 160
    assert problems == []
    context.close()


def test_a_cleaning_error_shows_next_to_the_cleaning_controls(browser, base, tmp_path):
    context, page, problems = open_page(browser, base, "desktop", "light")
    load_sales(page, tmp_path)
    page.click("#t-clean")
    page.select_option("#cact", "fill_constant")
    page.select_option("#ccol", "revenue")
    page.click("#cleanform button[type=submit]")  # no value typed
    page.wait_for_selector("#cleanformmsg .msg")
    assert page.locator("#cleanformmsg .msg").inner_text().strip() != ""
    assert page.locator("#answer").inner_text().strip() == ""
    assert "No cleaning applied yet." in page.inner_text("#cleanstate")
    page.select_option("#cact", "fill_median")
    page.select_option("#ccol", "product")  # text column: the step would change nothing
    page.click("#cleanform button[type=submit]")
    page.wait_for_selector("#cleanformmsg .msg.error")
    assert "No cleaning applied yet." in page.inner_text("#cleanstate")
    assert [p for p in problems if not p.startswith("HTTP")] == []
    context.close()


def test_long_tables_are_cut_and_open_in_place_with_a_sticky_header(browser, base, tmp_path):
    context, page, problems = open_page(browser, base, "desktop", "light")
    rows = ["id,group,value"] + [f"{i},g{i},{i * 3 % 17}" for i in range(1, 61)]
    f = tmp_path / "long.csv"
    f.write_text("\n".join(rows) + "\n")
    page.set_input_files("#file", str(f))
    page.wait_for_selector("#cards .card")
    page.fill("#ask", "Sum value by group")
    page.click("#askform button[type=submit]")
    page.wait_for_selector("#analyses article")
    table = page.locator("#analyses article table").first
    assert table.locator("tbody tr:visible").count() == 12
    page.click("#analyses article button:has-text('Show all')")
    assert table.locator("tbody tr:visible").count() > 12
    assert (
        page.evaluate("getComputedStyle(document.querySelector('#analyses th')).position")
        == "sticky"
    )
    # the wheel over a collapsed table moves the page, not a hidden inner scroller
    page.click("#analyses article button:has-text('Show fewer')")
    assert page.evaluate(
        "document.querySelector('#analyses .tw').scrollHeight <= document.querySelector('#analyses .tw').clientHeight + 1"
    )
    assert problems == []
    context.close()


def test_results_can_be_removed_and_cleared(browser, base, tmp_path):
    context, page, problems = open_page(browser, base, "desktop", "light")
    load_sales(page, tmp_path)
    for text in ("Average revenue by product", "Median revenue by region"):
        page.fill("#ask", text)
        page.click("#askform button[type=submit]")
        page.wait_for_selector("#answer .msg.ok")
    page.locator("#analyses article").nth(1).wait_for()
    page.click("#analyses article >> nth=0 >> button:has-text('Remove')")
    page.wait_for_selector("#answer .msg:has-text('Removed')")
    assert page.locator("#analyses article").count() == 1
    page.click("#clearres")
    page.wait_for_selector("#answer .msg:has-text('Removed 1 result')")
    assert page.locator("#analyses article").count() == 0
    assert page.locator("#none").is_visible() and page.locator("#clearres").is_hidden()
    assert problems == []
    context.close()


def test_theme_switch_changes_the_page_and_the_chart_images(browser, base, tmp_path):
    context, page, problems = open_page(browser, base, "desktop", "light")
    load_sales(page, tmp_path)
    before = page.get_attribute("#starter img >> nth=0", "src")
    page.click("#theme")
    assert page.evaluate("document.documentElement.getAttribute('data-theme')") == "dark"
    after = page.get_attribute("#starter img >> nth=0", "src")
    assert before != after
    bg = page.evaluate("getComputedStyle(document.body).backgroundColor")
    assert sum(int(v) for v in bg.replace("rgb(", "").replace(")", "").split(",")[:3]) / 3 < 100
    page.click("#theme")
    assert page.evaluate("document.documentElement.getAttribute('data-theme')") == "light"
    assert problems == []
    context.close()
