from alembic import command
from sqlalchemy import create_engine, inspect, text

EXPECTED_TABLES = {
    "competitions", "countries", "country_aliases", "sports", "disciplines", "sport_aliases",
    "events", "entrants", "ingest_runs", "raw_documents", "raw_versions", "raw_fetches",
    "placings", "placing_history", "source_observations", "source_conflicts", "quarantine",
    "reconciliation_results", "analytics_snapshots", "snapshot_rows", "snapshot_changes",
}  # fmt: skip


def _names(url):
    eng = create_engine(url)
    insp = inspect(eng)
    out = set(insp.get_table_names()), set(insp.get_view_names())
    eng.dispose()
    return out


def test_upgrade_creates_every_table_and_the_view(alembic_cfg, empty_db_url):
    command.upgrade(alembic_cfg, "head")
    tables, views = _names(empty_db_url)
    assert tables >= EXPECTED_TABLES
    assert views == {"v_medal_facts"}


def test_current_placing_uniqueness_is_a_partial_index(alembic_cfg, empty_db_url):
    command.upgrade(alembic_cfg, "head")
    eng = create_engine(empty_db_url)
    with eng.connect() as conn:
        definition = conn.execute(
            text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ux_placings_current'")
        ).scalar_one()
    eng.dispose()
    assert "UNIQUE" in definition and "WHERE is_current" in definition


def test_downgrade_removes_everything_and_upgrade_can_repeat(alembic_cfg, empty_db_url):
    command.upgrade(alembic_cfg, "head")
    command.downgrade(alembic_cfg, "base")
    tables, views = _names(empty_db_url)
    assert tables <= {"alembic_version"} and not views
    command.upgrade(alembic_cfg, "head")
    tables, _ = _names(empty_db_url)
    assert tables >= EXPECTED_TABLES
