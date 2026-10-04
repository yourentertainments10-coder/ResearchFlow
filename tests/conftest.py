"""Test database setup.

If TEST_DATABASE_URL is set (CI uses a Postgres service container) it is used. Otherwise an embedded
PostgreSQL started by ``pgserver`` is used, so tests need no manual setup. A template database with
migrations applied is built once; every test gets a fresh copy of it (fast, fully isolated).
"""

from __future__ import annotations

import os
import tempfile
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL, make_url

from sie.config import normalise_database_url

ROOT = Path(__file__).resolve().parents[1]


def _alembic_config(url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    os.environ["DATABASE_URL"] = url
    return cfg


@pytest.fixture(scope="session")
def admin_url() -> Iterator[URL]:
    external = os.environ.get("TEST_DATABASE_URL")
    if external:
        yield make_url(normalise_database_url(external))
        return
    import pgserver

    with tempfile.TemporaryDirectory() as tmp:
        server = pgserver.get_server(Path(tmp) / "pg")
        yield make_url(normalise_database_url(server.get_uri()))
        server.cleanup()


@pytest.fixture(scope="session")
def template_db(admin_url: URL) -> Iterator[str]:
    """A database with all migrations applied, used as a template for per-test databases."""
    name = f"sie_tmpl_{uuid.uuid4().hex[:8]}"
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    command.upgrade(_alembic_config(admin_url.set(database=name).render_as_string(False)), "head")
    yield name
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()


@pytest.fixture()
def db_url(admin_url: URL, template_db: str) -> Iterator[URL]:
    name = f"sie_t_{uuid.uuid4().hex[:10]}"
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}" TEMPLATE "{template_db}"'))
    yield admin_url.set(database=name)
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()


@pytest.fixture()
def engine(db_url: URL) -> Iterator[Engine]:
    eng = create_engine(db_url)
    yield eng
    eng.dispose()


@pytest.fixture()
def empty_db_url(admin_url: URL) -> Iterator[URL]:
    """A brand-new database with NO migrations, for migration tests."""
    name = f"sie_e_{uuid.uuid4().hex[:10]}"
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    yield admin_url.set(database=name)
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()


@pytest.fixture()
def alembic_cfg(empty_db_url: URL) -> Config:
    return _alembic_config(empty_db_url.render_as_string(hide_password=False))
