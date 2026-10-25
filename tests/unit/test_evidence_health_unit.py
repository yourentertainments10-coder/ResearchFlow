"""Pure parts of project health (ADR-043): state classification, old-upload flag, gap mapping."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from sie.answer.models import GapKind
from sie.evidence.health import (
    ProjectHealth,
    SourceHealthItem,
    SourceState,
    classify_source,
    gaps_from_health,
    is_old_upload,
)

NOW = datetime(2026, 10, 15, 10, 0, tzinfo=UTC)
MAX_AGE = timedelta(days=30)


def classify(kind="web", age_days=1, retain_until=None, pruned_at=None):
    return classify_source(
        kind=kind,
        retrieved_at=NOW - timedelta(days=age_days),
        retain_until=retain_until,
        pruned_at=pruned_at,
        now=NOW,
        web_max_age=MAX_AGE,
    )


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"age_days": 1}, SourceState.CURRENT),
        ({"age_days": 30}, SourceState.CURRENT),  # exactly at the limit is not yet stale
        ({"age_days": 31}, SourceState.STALE),
        (
            {"kind": "upload", "age_days": 3650},
            SourceState.UPLOAD,
        ),  # age alone never makes it stale
        ({"retain_until": NOW}, SourceState.EXPIRED),  # deadline reached
        ({"retain_until": NOW + timedelta(seconds=1)}, SourceState.CURRENT),
        ({"retain_until": NOW - timedelta(days=1), "age_days": 100}, SourceState.EXPIRED),
        ({"pruned_at": NOW - timedelta(days=1)}, SourceState.PRUNED),
        (  # removed bytes beat an elapsed deadline and age: never reported as current
            {"pruned_at": NOW, "retain_until": NOW - timedelta(days=5), "age_days": 200},
            SourceState.PRUNED,
        ),
        ({"kind": "upload", "pruned_at": NOW}, SourceState.PRUNED),
    ],
)
def test_classify_source(kwargs, expected):
    assert classify(**kwargs) is expected


def test_expired_and_pruned_are_different_states():
    expired = classify(retain_until=NOW - timedelta(days=1))
    pruned = classify(retain_until=NOW - timedelta(days=1), pruned_at=NOW)
    assert expired is SourceState.EXPIRED and pruned is SourceState.PRUNED


def test_old_upload_flag_is_for_uploads_only_and_has_a_strict_boundary():
    limit = timedelta(days=180)
    old = NOW - timedelta(days=181)
    assert is_old_upload("upload", old, NOW, limit)
    assert not is_old_upload("upload", NOW - limit, NOW, limit)
    assert not is_old_upload("web", old, NOW, limit)


def item(source_id, state, old=False):
    return SourceHealthItem(
        source_id=source_id, kind="upload" if state is SourceState.UPLOAD else "web",
        title="t", state=state, retrieved_at=NOW, age_seconds=0.0, retain_until=None,
        pruned_at=None, old_upload=old,
    )  # fmt: skip


def report(items=(), by_status=None, conflicts=(), pruned_claims=0, attention=()):
    return ProjectHealth(
        project="p", checked_at=NOW, web_max_age_days=30, old_upload_days=180,
        sources=tuple(items), claims_by_status=by_status or {},
        claims_on_pruned_sources=pruned_claims, claims_on_expired_sources=0,
        xlsx_claims_pending=0, open_conflict_ids=tuple(conflicts), attention=tuple(attention),
    )  # fmt: skip


def test_empty_project_is_an_explicit_no_sources_report_not_a_failure():
    r = report()
    assert r.no_sources and r.overall == "no_sources" and not r.attention_needed
    assert r.to_dict()["no_sources"] is True
    assert [g.id for g in gaps_from_health(r)] == ["project_no_sources"]


def test_old_upload_alone_does_not_need_attention():
    r = report([item(1, SourceState.UPLOAD, old=True)])
    assert r.overall == "ok" and not r.attention_needed
    assert r.to_dict()["sources"][0]["old_upload"] is True


def test_gaps_cover_each_problem_with_counts_from_the_report():
    r = report(
        [item(1, SourceState.STALE), item(2, SourceState.EXPIRED), item(3, SourceState.PRUNED)],
        by_status={"unverified": 4, "source_located": 2},
        conflicts=(7,),
        pruned_claims=3,
    )
    gaps = {g.id: g for g in gaps_from_health(r)}
    assert set(gaps) == {
        "project_stale_sources", "project_expired_sources", "project_pruned_sources",
        "project_open_conflicts", "project_unverified_claims",
    }  # fmt: skip
    assert gaps["project_stale_sources"].kind is GapKind.STALE
    assert gaps["project_pruned_sources"].kind is GapKind.MISSING_DATA
    assert "3 claim(s)" in gaps["project_pruned_sources"].message
    assert gaps["project_open_conflicts"].kind is GapKind.CONFLICT
    assert "4 claim(s)" in gaps["project_unverified_claims"].message


def test_a_healthy_project_has_no_gaps():
    r = report([item(1, SourceState.CURRENT)], by_status={"source_located": 1})
    assert gaps_from_health(r) == ()
