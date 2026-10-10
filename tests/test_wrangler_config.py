"""Cloudflare Workers Builds runs `npx wrangler preview` on non-production branches, and wrangler 4.x
refuses to run it unless wrangler.jsonc has a `previews` block (it may be empty). Production deploys
(`wrangler deploy`) must keep serving ./site unchanged."""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_jsonc(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"^\s*//.*$", "", text, flags=re.MULTILINE)  # whole-line comments only
    return json.loads(text)


def test_previews_block_exists_and_production_settings_are_unchanged():
    cfg = load_jsonc(ROOT / "wrangler.jsonc")
    assert "previews" in cfg and isinstance(cfg["previews"], dict)
    assert cfg["name"] == "researchflow"
    assert cfg["assets"] == {"directory": "./site"}
    assert (ROOT / "site" / "index.html").exists()


def test_previews_holds_no_secret_or_binding():
    # An empty block: previews must not carry variables or bindings that could reach production data.
    assert load_jsonc(ROOT / "wrangler.jsonc")["previews"] == {}
