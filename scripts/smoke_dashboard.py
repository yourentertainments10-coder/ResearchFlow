"""Browser smoke test for the dashboard, local file or live URL. Run it from a machine that can reach the site.

    pip install playwright && playwright install chromium
    python scripts/smoke_dashboard.py https://researchflow.yourentertainments10.workers.dev/
    python scripts/smoke_dashboard.py site/index.html          # a local file works too

For every route it checks that the page drew (no "Couldn't draw" box, real text), and for each of
desktop-light, desktop-dark and mobile it fails on any uncaught JS error or console error and on
horizontal overflow. It also confirms the Methodology page shows the validation table. Exit code 1 on any
problem, so it can gate a deployment. It makes only ordinary page loads: no clicks, no form input.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROUTES = ["overview", "countries", "sports", "gender", "timeline", "explorer", "method"]
CONTEXTS = {
    "desktop-light": {"viewport": {"width": 1280, "height": 900}, "color_scheme": "light"},
    "desktop-dark": {"viewport": {"width": 1280, "height": 900}, "color_scheme": "dark"},
    "mobile": {
        "viewport": {"width": 390, "height": 844},
        "is_mobile": True,
        "color_scheme": "light",
    },
}


def target_url(arg: str) -> str:
    path = Path(arg)
    return path.resolve().as_uri() if path.exists() else arg.split("#")[0]


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    from playwright.sync_api import sync_playwright

    url = target_url(argv[1])
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for name, options in CONTEXTS.items():
            context = browser.new_context(**options)
            page = context.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda e, errors=errors: errors.append(str(e)))
            page.on(
                "console",
                lambda m, errors=errors: errors.append(m.text) if m.type == "error" else None,
            )
            for route in ROUTES:
                page.goto(f"{url}#/{route}")
                page.wait_for_timeout(400)
                text = page.inner_text("#app")
                if len(text) < 200 or "Couldn’t draw" in text:
                    problems.append(f"{name} {route}: page did not draw ({len(text)} characters)")
                print(f"{name:14} {route:10} {len(text):5} characters")
            page.goto(f"{url}#/method")
            page.wait_for_timeout(300)
            if "Validation evidence" not in page.inner_text("#app"):
                problems.append(f"{name} method: validation table missing")
            if page.evaluate(
                "document.documentElement.scrollWidth > document.documentElement.clientWidth"
            ):
                problems.append(f"{name}: horizontal overflow")
            problems += [f"{name}: {e}" for e in errors]
            context.close()
        browser.close()
    for problem in problems:
        print("PROBLEM:", problem)
    print("FAIL" if problems else "ALL OK")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
