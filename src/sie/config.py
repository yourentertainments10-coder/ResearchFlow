"""Single settings module. All configuration comes from environment variables (or .env)."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, field_validator
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
    alert_renotify_minutes: int = 0
    # Backups and tools: directory of pg_dump/pg_restore (default: found on PATH).
    pg_bin_dir: Path | None = None
    # Backup encryption (docs/DEPLOYMENT.md, ADR-034): the age PUBLIC key dumps are encrypted to. The
    # private key is never a setting; it is a file given to `sie restore-test --identity-file`.
    backup_age_recipient: str | None = None
    # A scratch PostgreSQL server for restore tests. Unset: the restore test uses DATABASE_URL's server,
    # which must never be production (the workflow always sets this).
    restore_test_database_url: str | None = None
    # Evidence ledger (ADR-042, docs/EVIDENCE_RETENTION.md).
    evidence_max_upload_bytes: int = 25 * 1024 * 1024  # provisional cap (owner decision 2026-10-12)
    evidence_max_decompressed_bytes: int = (
        200 * 1024 * 1024
    )  # ceiling when stored bytes are read back
    # How long a web page kept in FULL (only allowed under a recorded policy) is retained. The default
    # is 90 days; `sie evidence prune` drops older bytes. Excerpt-only sources have no such clock.
    evidence_full_retention_days: int = 90
    # Project health (ADR-043). A web source older than this is `stale` (potentially outdated, not false).
    evidence_web_max_age_days: int = Field(default=30, ge=1)
    # An upload older than this gets an informational `old_upload` flag. It never fails a health check.
    evidence_old_upload_days: int = Field(default=180, ge=1)
    # Web fetching (ADR-044). The interval may be raised but never lowered below 2 s (AGENTS.md rule 9).
    evidence_fetch_interval_seconds: float = Field(default=2.0, ge=2.0)
    evidence_fetch_max_bytes: int = Field(default=5 * 1024 * 1024, ge=1024)
    evidence_fetch_timeout_seconds: float = Field(default=20.0, gt=0, le=120)
    # Persisted collected datasets (ADR-044): where they live, how long they are kept by default.
    evidence_dataset_path: Path | None = None
    evidence_dataset_retention_days: int = Field(default=90, ge=1)
    # The recorded per-source retention policies (docs/EVIDENCE_RETENTION.md). Unset: data/evidence/
    # source_policies.csv. A host that is not listed there can only be kept as excerpts, never in full.
    evidence_policy_path: Path | None = None
    # What the owner found in the database provider's console about point-in-time history. Unset means
    # NOT VERIFIED, and purge output says so. Never guessed (docs/EVIDENCE_RETENTION.md section 6).
    db_history_window_hours: int | None = Field(default=None, ge=0)
    # How long encrypted backups are kept (the backup workflow's retention-days). Used only to word
    # what a purge cannot undo.
    backup_retention_days: int = 14

    @field_validator("database_url")
    @classmethod
    def _normalise_url(cls, v: str) -> str:
        return normalise_database_url(v)

    @field_validator("db_history_window_hours", mode="before")
    @classmethod
    def _blank_window_is_unset(cls, v):
        return None if isinstance(v, str) and not v.strip() else v

    @field_validator("backup_age_recipient", "restore_test_database_url", mode="before")
    @classmethod
    def _empty_is_unset(cls, v: str | None) -> str | None:
        return v.strip() or None if isinstance(v, str) else v

    @field_validator("restore_test_database_url")
    @classmethod
    def _normalise_scratch_url(cls, v: str | None) -> str | None:
        return normalise_database_url(v) if v else v

    @property
    def alert_state_file(self) -> Path:
        return self.alert_state_path or self.data_dir / "alert_state.json"

    @property
    def evidence_policy_file(self) -> Path:
        return self.evidence_policy_path or self.data_dir / "evidence" / "source_policies.csv"

    @property
    def evidence_dataset_dir(self) -> Path:
        return self.evidence_dataset_path or self.data_dir / "evidence_datasets"

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
