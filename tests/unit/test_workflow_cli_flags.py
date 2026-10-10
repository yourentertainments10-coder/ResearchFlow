"""Every ``sie <command> --flag`` in a workflow must exist in the CLI.

``backup.yml`` once called ``sie backup --out`` while the option is ``--out-dir``: all scheduled runs
failed with a usage error, unnoticed, because nothing runs workflows in tests.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import typer.main

from sie.cli import app

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
COMMANDS = typer.main.get_command(app).commands  # type: ignore[attr-defined]
INVOCATION = re.compile(r"(?<![\w./-])sie[ \t]+([a-z][a-z-]*)([^\n;&|]*)")


def invocations(text: str):
    text = text.replace("\\\n", " ")  # join shell line continuations
    for match in INVOCATION.finditer(text):
        yield match.group(1), re.findall(r"(?<![\w-])(--[a-z][a-z-]*)", match.group(2))


def options_of(command: str) -> set[str]:
    return {o for p in COMMANDS[command].params for o in (*p.opts, *p.secondary_opts)}


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda p: p.name)
def test_workflow_commands_and_flags_exist(workflow):
    found = list(invocations(workflow.read_text(encoding="utf-8")))
    for command, flags in found:
        assert command in COMMANDS, f"{workflow.name}: unknown command `sie {command}`"
        unknown = [f for f in flags if f not in options_of(command)]
        assert not unknown, f"{workflow.name}: `sie {command}` has no option(s) {unknown}"


def test_the_checker_would_have_caught_the_backup_bug():
    ((command, flags),) = list(invocations("run: sie backup --out backups --keep 14"))
    assert command == "backup" and "--out" in flags and "--out" not in options_of("backup")


def test_backup_workflow_never_uploads_a_plaintext_dump():
    text = (ROOT / ".github/workflows/backup.yml").read_text(encoding="utf-8")
    assert "--require-encryption" in text and "sie-backup-encrypted" in text
    assert "name: sie-backup\n" not in text  # the old, unencrypted artifact name
