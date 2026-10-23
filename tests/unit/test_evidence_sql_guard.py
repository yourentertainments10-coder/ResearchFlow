"""Static guard: every SQL string in sie.evidence that touches a project-owned table names project_id.

This catches the classic leak, a query on ``claims`` or ``evidence_sources`` written without a project
filter. It is a text check (it cannot prove a filter is *correct*), so the behavioural cross-project
cases in tests/integration/test_evidence_isolation.py remain the real proof.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[2] / "src" / "sie" / "evidence"
PROJECT_OWNED = (
    "evidence_sources",
    "claims",
    "claim_reviews",
    "claim_conflicts",
    "project_documents",
    "evidence_claims_current",
)
TOUCH = re.compile(
    r"\b(?:FROM|JOIN|INTO|UPDATE)\s+(" + "|".join(PROJECT_OWNED) + r")\b", re.IGNORECASE
)
# Owner-only maintenance code, which acts on one project given explicitly and is not callable with a
# scope. Each entry needs a reason; the list is deliberately empty until such a module exists.
EXEMPT: dict[str, str] = {}


def sql_strings(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    inside_fstring: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            text = "".join(
                part.value if isinstance(part, ast.Constant) else "{}" for part in node.values
            )
            inside_fstring.update(id(part) for part in node.values)
            yield node.lineno, text
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in inside_fstring
        ):
            yield node.lineno, node.value


def module_constants(path: Path) -> dict[str, str]:
    """Module-level string constants (for example a shared column list) that f-strings splice in."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        t.id: n.value.value
        for n in tree.body
        if isinstance(n, ast.Assign)
        and isinstance(n.value, ast.Constant)
        and isinstance(n.value.value, str)
        for t in n.targets
        if isinstance(t, ast.Name)
    }


@pytest.mark.parametrize("path", sorted(PACKAGE.glob("*.py")), ids=lambda p: p.name)
def test_every_query_on_a_project_owned_table_filters_or_sets_project_id(path):
    if path.name in EXEMPT:
        pytest.skip(EXEMPT[path.name])
    problems = []
    for line, sql in sql_strings(path):
        if TOUCH.search(sql) and "project_id" not in sql:
            problems.append(f"{path.name}:{line}: {' '.join(sql.split())[:100]}")
    assert not problems, "SQL on a project-owned table without project_id:\n" + "\n".join(problems)


def test_the_guard_itself_catches_a_leaky_query(tmp_path):
    bad = tmp_path / "leaky.py"
    bad.write_text('Q = "SELECT * FROM claims WHERE id = :i"\n')
    flagged = [s for _, s in sql_strings(bad) if TOUCH.search(s) and "project_id" not in s]
    assert flagged == ["SELECT * FROM claims WHERE id = :i"]
