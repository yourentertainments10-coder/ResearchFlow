from __future__ import annotations

from sqlalchemy import Engine, create_engine

from sie.config import Settings, get_settings


def make_engine(settings: Settings | None = None) -> Engine:
    settings = settings or get_settings()
    # pool_pre_ping: managed Postgres (Render, Neon) closes idle connections; reconnect transparently.
    return create_engine(settings.database_url, pool_pre_ping=True, future=True)
