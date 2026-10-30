"""Workspace rules: import boundaries, no network code, no dynamic evaluation, many random files
agreeing with the independent check, and the command adapter."""

from __future__ import annotations

import ast
import json
import random
import sys
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from sie.workspace import build as build_module
from sie.workspace import build_workspace
from sie.workspace.models import Verification
from sie.workspace_cli import register

SRC = Path(__file__).resolve().parents[2] / "src" / "sie"
PKG = SRC / "workspace"
ALLOWED_THIRD_PARTY = {"pandas", "pydantic", "numpy"}
FORBIDDEN_SIE = ("sie.cli", "sie.db", "sie.pipeline", "sie.analytics", "sie.evidence", "sie.config")
NETWORK = {
    "socket",
    "urllib",
    "http",
    "httpx",
    "requests",
    "ssl",
    "ftplib",
    "smtplib",
    "subprocess",
}


def _imports(path: Path) -> list[str]:
    names: list[str] = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names += [n.name for n in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.append(node.module)
    return names


def test_the_package_imports_only_datasets_answer_and_the_standard_library():
    stdlib = set(sys.stdlib_module_names)
    for path in PKG.glob("*.py"):
        for name in _imports(path):
            root = name.split(".")[0]
            assert root in stdlib or root in ALLOWED_THIRD_PARTY or root == "sie", (path.name, name)
            assert not any(name == f or name.startswith(f + ".") for f in FORBIDDEN_SIE), (
                path.name,
                name,
            )
            assert root not in NETWORK, (path.name, name)
            assert root != "typer", path.name


def test_only_the_figure_module_imports_the_answer_package():
    users = sorted(
        p.name for p in PKG.glob("*.py") if any(n.startswith("sie.answer") for n in _imports(p))
    )
    assert users == ["figs.py"]


def test_no_dynamic_evaluation_anywhere_in_the_package():
    for path in [*PKG.glob("*.py"), SRC / "workspace_cli.py"]:
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in {"eval", "exec", "compile", "__import__"}, path.name


def test_the_checker_imports_no_calculation_code_from_the_engine():
    wanted = set()
    for name in _imports(PKG / "verify.py"):
        assert not name.startswith("sie.datasets.calc"), name
        wanted.add(name)
    assert "sie.datasets.models" in wanted


# --- random files: the engine and the independent path must agree ---------------------------------


def _random_csv(seed: int) -> bytes:
    rng = random.Random(seed)
    regions = ["North", "South", "East", "West", "central"][: rng.randint(2, 5)]
    lines = ["region,kind,day,qty,price,flag"]
    for _ in range(rng.randint(8, 60)):
        region = "" if rng.random() < 0.08 else rng.choice(regions)
        qty = "" if rng.random() < 0.1 else str(rng.randint(0, 6))
        price = "" if rng.random() < 0.1 else f"{rng.randint(0, 5000) / 100:.2f}"
        day = (
            "" if rng.random() < 0.05 else f"2025-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}"
        )
        lines.append(
            f"{region},{rng.choice('xyz')},{day},{qty},{price},{rng.choice(['yes', 'no'])}"
        )
    return ("\n".join(lines) + "\n").encode()


@pytest.mark.parametrize("seed", range(40))
def test_random_files_agree_with_the_independent_check(seed):
    workspace = build_workspace(_random_csv(seed), "random.csv")
    for i in workspace.items:
        assert i.verification.status == "verified", (seed, i.planned.id, i.verification.to_dict())
    assert workspace.profile_verification.status == "verified", seed
    assert all(c.verified for c in workspace.cards)


# --- command adapter ------------------------------------------------------------------------------

app = typer.Typer()
register(app)
runner = CliRunner()
SALES = b"region,units\nnorth,10\nsouth,20\nnorth,5\nsouth,7\nnorth,3\nsouth,2\n"


def invoke(tmp_path: Path, *args: str):
    return runner.invoke(app, ["workspace", *args], env={"DATA_DIR": str(tmp_path / "data")})


def test_run_writes_a_verified_directory_under_out_and_prints_a_summary(tmp_path):
    source = tmp_path / "sales.csv"
    source.write_bytes(SALES)
    result = invoke(tmp_path, "run", str(source), "--out", str(tmp_path / "out"))
    assert result.exit_code == 0, result.output
    summary = json.loads(result.output)
    assert Path(summary["directory"]).parent == tmp_path / "out"
    assert summary["not_exported"] == [] and summary["analyses"] == summary["exported"]
    assert {c["label"] for c in summary["cards"]} >= {"Rows", "Columns"}
    again = json.loads(invoke(tmp_path, "run", str(source), "--out", str(tmp_path / "out")).output)
    assert again["already_present"] is True


def test_run_exits_1_for_an_unusable_file(tmp_path):
    source = tmp_path / "bad.csv"
    source.write_bytes(b"a,b\n1,2,3\n")
    result = invoke(tmp_path, "run", str(source), "--out", str(tmp_path / "out"))
    assert result.exit_code == 1
    assert not (tmp_path / "out").exists() or not list((tmp_path / "out").iterdir())


def test_run_exits_3_and_exports_nothing_unverified(tmp_path, monkeypatch):
    real = build_module.verify_result

    def fail_trend(ds, spec, table):
        if spec.op == "top_n":
            return Verification("unverified", 1, ["forced"])
        return real(ds, spec, table)

    monkeypatch.setattr(build_module, "verify_result", fail_trend)
    source = tmp_path / "sales.csv"
    source.write_bytes(SALES)
    result = invoke(tmp_path, "run", str(source), "--out", str(tmp_path / "out"))
    assert result.exit_code == 3
    summary = json.loads(result.output)
    assert [s["id"] for s in summary["not_exported"]] == ["a2"]
    assert not list(Path(summary["directory"]).glob("analysis_a2.*"))
