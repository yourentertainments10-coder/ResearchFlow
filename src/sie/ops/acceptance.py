"""``sie acceptance``: a read-only check of a loaded database against the Asian Games 2026 figures.

It answers one question with evidence: does this database hold the verified results, and is the
pipeline keeping it current? Every check reports its observed value and its expected value, so a human
can read the result without looking at workflow logs. It reads only (one READ ONLY transaction), so it is
safe to point at the production database from the owner's computer.

What it cannot see: whether a particular GitHub Actions run was *scheduled* or manual (the database does
not store the trigger; read the run list on GitHub for that), and the contents of the ``analyze``
artifacts (read ``manifest.json`` there). ``docs/ACCEPTANCE.md`` lists those manual steps.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Engine, text


@dataclass(frozen=True)
class Expected:
    """The verified figures (docs/SOURCE_DISCOVERY.md section 11)."""

    events: int = 469
    placings: int = 1568
    gold: int = 470
    silver: int = 469
    bronze: int = 629
    countries: int = 40
    source: str = "official"
    max_age: timedelta = timedelta(hours=26)  # a daily refresh, plus GitHub's start-up delay


@dataclass
class Check:
    name: str
    ok: bool
    observed: Any
    expected: Any
    note: str = ""


@dataclass
class AcceptanceReport:
    competition: str
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(c.ok for c in self.checks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "competition": self.competition,
            "ok": self.ok,
            "checks": [
                {
                    "name": c.name,
                    "ok": c.ok,
                    "observed": c.observed,
                    "expected": c.expected,
                    "note": c.note,
                }
                for c in self.checks
            ],
        }


def acceptance_report(
    engine: Engine, competition: str, now: datetime, expected: Expected | None = None
) -> AcceptanceReport:
    exp = expected or Expected()
    report = AcceptanceReport(competition)

    def add(name: str, observed: Any, wanted: Any, note: str = "", ok: bool | None = None) -> None:
        report.checks.append(
            Check(name, observed == wanted if ok is None else ok, observed, wanted, note)
        )

    with engine.connect().execution_options(
        isolation_level="REPEATABLE READ", postgresql_readonly=True
    ) as conn:
        comp = conn.execute(
            text("SELECT id, official_event_total FROM competitions WHERE code = :c"),
            {"c": competition},
        ).one_or_none()
        if comp is None:
            add("competition exists", None, competition)
            return report
        cid = comp.id
        one = lambda sql, **p: conn.execute(text(sql), {"c": cid, **p}).scalar_one()  # noqa: E731

        revision = conn.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one_or_none()
        add("schema migrated (alembic revision present)", revision, "a revision", ok=bool(revision))
        mode = conn.execute(text("SHOW transaction_read_only")).scalar_one()
        add("this check ran in a read-only transaction", mode, "on")
        add(
            "events in the database",
            one("SELECT count(*) FROM events WHERE competition_id = :c"),
            exp.events,
        )
        add("official event total on the competition", comp.official_event_total, exp.events)
        medals = dict(
            conn.execute(
                text(
                    """SELECT p.medal, count(*) FROM placings p JOIN events e ON e.id = p.event_id
                       WHERE e.competition_id = :c AND p.is_current GROUP BY p.medal"""
                ),
                {"c": cid},
            ).all()
        )
        add("current placings", sum(medals.values()), exp.placings)
        add("gold placings", medals.get("Gold", 0), exp.gold)
        add("silver placings", medals.get("Silver", 0), exp.silver)
        add("bronze placings", medals.get("Bronze", 0), exp.bronze)
        add(
            "events with a gold",
            one(
                """SELECT count(DISTINCT p.event_id) FROM placings p JOIN events e ON e.id = p.event_id
                   WHERE e.competition_id = :c AND p.is_current AND p.medal = 'Gold'"""
            ),
            exp.events,
        )
        add(
            "countries with a medal",
            one(
                """SELECT count(DISTINCT p.country_id) FROM placings p JOIN events e ON e.id = p.event_id
                   WHERE e.competition_id = :c AND p.is_current"""
            ),
            exp.countries,
        )
        add(
            "disputed events",
            one("SELECT count(*) FROM events WHERE competition_id = :c AND is_disputed"),
            0,
        )
        add(
            "open conflicts",
            one(
                """SELECT count(*) FROM source_conflicts k JOIN events e ON e.id = k.event_id
                   WHERE e.competition_id = :c AND k.status = 'needs_review'"""
            ),
            0,
        )
        add(
            "unresolved quarantined rows",
            one(
                """SELECT count(*) FROM quarantine q JOIN ingest_runs r ON r.id = q.run_id
                   WHERE r.competition_id = :c AND NOT q.resolved"""
            ),
            0,
        )

        last = conn.execute(
            text(
                """SELECT max(finished_at) FROM ingest_runs
                   WHERE competition_id = :c AND source = :s AND status = 'success'"""
            ),
            {"c": cid, "s": exp.source},
        ).scalar_one()
        age = None if last is None else now - last
        add(
            f"last successful '{exp.source}' run is recent",
            None if age is None else f"{int(age.total_seconds() // 60)} minutes ago",
            f"within {int(exp.max_age.total_seconds() // 60)} minutes",
            ok=age is not None and age <= exp.max_age,
        )
        stuck = one(
            "SELECT count(*) FROM ingest_runs WHERE competition_id = :c AND status = 'running'"
        )
        add("runs still marked running", stuck, 0)
        snaps = one("SELECT count(*) FROM analytics_snapshots WHERE competition_id = :c")
        add("analytics snapshots taken (run `analyze` first)", snaps, ">= 1", ok=snaps >= 1)
    return report
