"""CLI guards of ``sie backup`` that must hold before any database or tool is touched."""

from __future__ import annotations

from typer.testing import CliRunner

from sie.cli import app

PROD = "postgresql://prod_user:secret@prod.example.com:5432/prod"
runner = CliRunner()


def run(*args, **env):
    return runner.invoke(app, ["backup", *args], env={"DATABASE_URL": PROD, **env})


def test_a_plaintext_backup_is_refused_when_encryption_is_required():
    result = run("--require-encryption", "--no-verify", BACKUP_AGE_RECIPIENT="")
    assert result.exit_code == 2
    assert "refusing to write a plaintext backup" in result.output


def test_the_restore_scratch_server_may_not_be_the_production_database():
    result = run("--scratch-url", PROD)
    assert result.exit_code == 2
    assert "must not be the production" in result.output
    assert "secret" not in result.output  # the password never reaches the output


def test_verifying_an_encrypted_backup_needs_the_identity_file():
    recipient = "age1" + "q" * 58
    result = run("--encrypt-to", recipient, "--scratch-url", "postgresql://u:p@localhost:5432/x")
    assert result.exit_code == 2
    assert "--identity-file" in result.output
