"""A fetched portal capture goes through the same single ingestion path as a capture file (ADR-021)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from sqlalchemy import text

from portal_fakes import BASE, CONTACT_AGENT, FakePortal, portal_bodies
from sie.config import Settings
from sie.pipeline.runner import run_ingest
from sie.reference import seed_reference
from sie.sources.bornan.fetch import RAW_URL, fetch_capture, make_client, portal_source_input
from sie.sources.http import FetchBlocked

ROOT = Path(__file__).resolve().parents[2]
BORNAN = ROOT / "tests/fixtures/sources/bornan"


@pytest.fixture()
def settings(db_url, tmp_path):
    shutil.copytree(ROOT / "data" / "reference", tmp_path / "reference")
    return Settings(
        _env_file=None,
        database_url=db_url.render_as_string(hide_password=False),
        data_dir=tmp_path,
        raw_store_backend="fs",
        http_user_agent=CONTACT_AGENT,
        http_min_interval_seconds=0,
    )


@pytest.fixture()
def seeded(engine, settings):
    with engine.begin() as c:
        seed_reference(c, settings.reference_dir)
    return engine


def client_for(settings, transport):
    return make_client(settings, transport=transport, sleep=lambda _s: None)


def test_a_fetched_capture_loads_like_a_capture_file_and_a_second_fetch_changes_nothing(
    seeded, settings
):
    portal = FakePortal()
    first = run_ingest(
        seeded, portal_source_input(settings, client=client_for(settings, portal)), settings
    )
    assert first.status == "success", first.error
    assert first.raw_outcome == "new_version"
    assert sum(first.placings.values()) == 153 and first.rows_quarantined == 0

    again = run_ingest(
        seeded,
        portal_source_input(settings, client=client_for(settings, FakePortal())),
        settings,
    )
    assert again.status == "success"
    assert again.raw_outcome == "unchanged"  # same bytes: no new version, nothing re-loaded
    assert again.placings.get("created", 0) == 0

    with seeded.connect() as c:
        docs = c.execute(text("SELECT source, url FROM raw_documents")).all()
        placings = c.execute(text("SELECT count(*) FROM placings WHERE is_current")).scalar_one()
    assert [tuple(r) for r in docs] == [("official", RAW_URL)] and placings == 153


def test_a_changed_portal_becomes_a_new_version_and_reallocates_through_history(seeded, settings):
    run_ingest(
        seeded,
        portal_source_input(settings, client=client_for(settings, FakePortal())),
        settings,
    )
    bodies = portal_bodies()
    rows = bodies[f"{BASE}/ARC/medals/discipline"]
    gold = next(r for r in rows if r["Medal"] == "ME_GOLD" and r["Order"] == 1)
    other = next(r["Org"] for r in rows if r["Org"] != gold["Org"])
    gold["Org"], gold["OrgDesc"] = other, "Reallocated country"  # a correction at the source
    changed = run_ingest(
        seeded,
        portal_source_input(settings, client=client_for(settings, FakePortal(bodies))),
        settings,
    )
    assert changed.raw_outcome == "new_version"
    assert changed.placings.get("reallocated", 0) >= 1
    with seeded.connect() as c:
        history = c.execute(
            text("SELECT count(*) FROM placing_history WHERE change_type = 'reallocated'")
        ).scalar_one()
    assert history >= 1


def test_a_blocked_portal_fails_the_fetch_and_stores_nothing(seeded, settings):
    from sie.sources.http import Response

    portal = FakePortal()
    portal.overrides[f"{BASE}/ALL/disc/data"] = Response(429, b"")
    with pytest.raises(FetchBlocked):
        portal_source_input(settings, client=client_for(settings, portal))
    with seeded.connect() as c:
        assert c.execute(text("SELECT count(*) FROM raw_versions")).scalar_one() == 0
        assert c.execute(text("SELECT count(*) FROM placings")).scalar_one() == 0


def test_the_file_capture_carries_the_fetch_time_and_loads_unchanged(seeded, settings, tmp_path):
    data = fetch_capture(settings, client=client_for(settings, FakePortal()), with_time=True)
    capture = json.loads(data)
    assert capture["at"].endswith("Z") and set(capture) >= {"at", "medals", "standings"}
    path = tmp_path / "capture.json"
    path.write_bytes(data)
    from sie.cli import _capture_input

    result = run_ingest(seeded, _capture_input(path, settings), settings)
    assert result.status == "success" and sum(result.placings.values()) == 153


def test_the_default_placeholder_agent_cannot_fetch(settings):
    from sie.sources.http import FetchRefused

    placeholder = Settings(_env_file=None, data_dir=settings.data_dir)
    with pytest.raises(FetchRefused, match="contact"):
        fetch_capture(placeholder, client=client_for(placeholder, FakePortal()), with_time=False)
