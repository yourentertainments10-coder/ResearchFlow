"""competitions.official_event_total comes from the reference seed, and `sie acceptance` reads it.

Regression: the seed never wrote the column, so production had NULL and the acceptance check
"official event total on the competition" reported observed=None. The expected number is derived here
from the official event list fixture (ALL/disc/data), never typed in.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text

from sie.ops.acceptance import Expected, acceptance_report
from sie.reference import ReferenceDataError, seed_reference

ROOT = Path(__file__).resolve().parents[2]
EVENT_LIST = ROOT / "tests" / "fixtures" / "sources" / "bornan" / "ALL_disc_data.trimmed.json"
CHECK = "official event total on the competition"


def official_total() -> int:
    return sum(len(d["Events"]) for d in json.loads(EVENT_LIST.read_text()))


@pytest.fixture()
def ref_dir(tmp_path):
    shutil.copytree(ROOT / "data" / "reference", tmp_path / "reference")
    return tmp_path / "reference"


def stored(engine) -> int | None:
    with engine.connect() as c:
        return c.execute(
            text("SELECT official_event_total FROM competitions WHERE code = 'asiad-2026'")
        ).scalar_one()


def check(engine):
    report = acceptance_report(
        engine, "asiad-2026", datetime.now(UTC), Expected(events=official_total())
    )
    return next(c for c in report.checks if c.name == CHECK)


def test_reference_total_equals_the_official_event_list(engine, ref_dir):
    with engine.begin() as c:
        seed_reference(c, ref_dir)
    assert stored(engine) == official_total()
    result = check(engine)
    assert result.ok and result.observed == official_total()


def test_a_database_seeded_before_the_fix_is_repaired_by_the_next_seed(engine, ref_dir):
    with engine.begin() as c:
        seed_reference(c, ref_dir)
        c.execute(
            text("UPDATE competitions SET official_event_total = NULL")
        )  # the production state
    result = check(engine)
    assert not result.ok and result.observed is None  # the reported failure
    with engine.begin() as c:
        seed_reference(c, ref_dir)  # what `refresh` runs first, daily
    assert check(engine).ok
    with engine.begin() as c:
        seed_reference(c, ref_dir)  # idempotent
    assert stored(engine) == official_total()


def test_an_empty_total_stays_null_and_a_bad_one_is_refused(engine, ref_dir):
    path = ref_dir / "competitions.csv"
    rows = path.read_text().splitlines()
    path.write_text(rows[0] + "\n" + rows[1].rsplit(",", 1)[0] + ",\n")
    with engine.begin() as c:
        seed_reference(c, ref_dir)
    assert stored(engine) is None
    path.write_text(rows[0] + "\n" + rows[1].rsplit(",", 1)[0] + ",46x\n")
    with pytest.raises(ReferenceDataError), engine.begin() as c:
        seed_reference(c, ref_dir)
