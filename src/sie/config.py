"""Single settings module. All configuration comes from environment variables (or .env)."""

from __future__ import annotations

from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DRIVER = "postgresql+psycopg"


def normalise_database_url(url: str) -> str:
    """Accept the URL forms hosting providers hand out and return a psycopg3 SQLAlchemy URL.

    Render, Neon and Supabase give ``postgres://`` or ``postgresql://``. SQLAlchemy needs the
    driver named explicitly. Query parameters such as ``sslmode=require`` are kept unchanged.
    """
    url = url.strip()
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return DRIVER + "://" + url[len(prefix) :]
    if url.startswith(DRIVER + "://"):
        return url
    raise ValueError("DATABASE_URL must start with postgres://, postgresql:// or " + DRIVER + "://")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql://sie:sie@localhost:5432/sie"
    data_dir: Path = Path("data")
    competition_id: str = (
        "asiad-2026"  # the competition code this run loads (reference table `competitions`)
    )
    raw_store_backend: str = (
        "fs"  # where raw bytes live: fs (data/raw) or db (raw_blobs); docs/DATABASE.md s4
    )
    manual_csv_max_rows: int = 20000
    manual_csv_max_cell_chars: int = 500
    http_user_agent: str = "SIE-research/0.1 (set HTTP_USER_AGENT with a contact address)"
    http_min_interval_seconds: float = 2.0
    http_timeout_seconds: float = 30.0
    # Kill switch for the live portal fetcher (ADR-031). Off unless explicitly set to true, so nothing
    # reaches the portal by accident; set PORTAL_FETCH_ENABLED=false to stop it without a code change.
    portal_fetch_enabled: bool = False
    source_priority: str = "official,manual"  # tie-break input only (docs/DATA_PIPELINE.md s7)
    freshness_threshold_minutes: int = 30
    # Sources expected to refresh on a schedule (comma separated). `sie health` checks these even if
    # they have never run, so a source that never started raises an alert. Empty: only sources that ran.
    scheduled_sources: str = ""
    log_level: str = "INFO"
    # Used only by `sie db-roles` (run once by the schema owner), never by the pipeline or dashboard.
    sie_reader_password: str | None = None
    sie_pipeline_password: str | None = None
    llm_api_key: str | None = None
    notify_webhook_url: str | None = None
    # Alert de-duplication: where the state lives, and when to remind about an alert that is still
    # open. 0 means never remind (an alert is sent once until it resolves and returns).
    alert_state_path: Path | None = None
    backup_age_recipient: str | None = None  # public age key (age1...), not a secret
    alert_renotify_minutes: int = 0
    # Backups and tools: directory of pg_dump/pg_restore (default: found on PATH).
    pg_bin_dir: Path | None = None

    @field_validator("database_url")
    @classmethod
    def _normalise_url(cls, v: str) -> str:
        return normalise_database_url(v)

    @property
    def alert_state_file(self) -> Path:
        return self.alert_state_path or self.data_dir / "alert_state.json"

    @property
    def scheduled_source_list(self) -> list[str]:
        return [s.strip() for s in self.scheduled_sources.split(",") if s.strip()]

    @property
    def source_priority_list(self) -> list[str]:
        return [s.strip() for s in self.source_priority.split(",") if s.strip()]

    @property
    def reference_dir(self) -> Path:
        return self.data_dir / "reference"


def get_settings() -> Settings:
    """Build settings from the current environment (not cached, so tests can change it)."""
    return Settings()
