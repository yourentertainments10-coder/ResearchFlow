"""Import rules from docs/ARCHITECTURE.md section 6, enforced from day one.

Source adapters (fetch only) must not import parsers; parsers (pure) must not import network or
database code. The sources package arrives in Phase 3: until then these tests pass vacuously, and
they start protecting the code the moment adapter.py / parser.py files appear.
"""

from __future__ import annotations

import ast
from pathlib import Path

SOURCES = Path(__file__).resolve().parents[2] / "src" / "sie" / "sources"
FORBIDDEN_IN_PARSER = {"httpx", "requests", "urllib3", "sqlalchemy", "psycopg", "sie.db"}


def _imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            names.add(module)
            names |= {f"{module}.{a.name}" for a in node.names}
    return names


def _starts(imports: set[str], prefixes: set[str]) -> set[str]:
    return {i for i in imports if any(i == p or i.startswith(p + ".") for p in prefixes)}


def test_adapters_do_not_import_parsers():
    for adapter in SOURCES.glob("*/adapter.py"):
        bad = {
            i for i in _imports(adapter) if i.endswith(".parser") or i.split(".")[-1] == "parser"
        }
        assert not bad, f"{adapter} imports its parser: {bad}"


def test_parsers_do_not_import_network_or_database_code():
    for parser in SOURCES.glob("*/parser.py"):
        bad = _starts(_imports(parser), FORBIDDEN_IN_PARSER)
        assert not bad, f"{parser} imports forbidden modules: {bad}"


def test_the_checker_itself_detects_violations(tmp_path):
    f = tmp_path / "parser.py"
    f.write_text("import httpx\nfrom sie.db.session import make_engine\n", encoding="utf-8")
    assert _starts(_imports(f), FORBIDDEN_IN_PARSER) == {
        "httpx",
        "sie.db.session",
        "sie.db.session.make_engine",
    }
