from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import text

from sie.load import LoadError, load_placings
from sie.reference import seed_reference
from sie.sources.bornan.parse_medals import parse_medal_rows

ROOT = Path(__file__).resolve().parents[2]
FIX = ROOT / "tests/fixtures/sources/bornan"


@pytest.fixture()
def loaded(engine):
    rows = {
        d: json.loads((FIX / f"{d}_medals_discipline.json").read_text()) for d in ("SWM", "ARC")
    }
    raw = json.dumps({"medals": rows}).encode()
    placings = [p for r in rows.values() for p in parse_medal_rows(r)]
    with engine.begin() as c:
        seed_reference(c, ROOT / "data" / "reference")
        result = load_placings(c, placings, raw, "test.json")
    return placings, raw, result


def test_load_counts_and_views(engine, loaded):
    placings, _, result = loaded
    assert result.events == len({(p.discipline, p.event_code) for p in placings})
    assert result.placings == {"created": len(placings)}
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM v_medal_facts")).scalar_one() == len(placings)
        ties = c.execute(text("SELECT count(*) FROM placings WHERE is_tie")).scalar_one()
    assert ties >= 2  # the swimming gold tie


def test_reload_is_idempotent(engine, loaded):
    placings, raw, _ = loaded
    with engine.begin() as c:
        again = load_placings(c, placings, raw, "test.json")
    assert again.placings == {"unchanged": len(placings)} and again.raw_unchanged
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM placings")).scalar_one() == len(placings)
        assert c.execute(text("SELECT count(*) FROM placing_history")).scalar_one() == len(placings)


def test_changed_country_becomes_a_reallocation(engine, loaded):
    placings, raw, _ = loaded
    import dataclasses

    first = placings[0]
    swapped = [
        dataclasses.replace(first, country_code="KOR" if first.country_code != "KOR" else "JPN"),
        *placings[1:],
    ]
    with engine.begin() as c:
        res = load_placings(c, swapped, raw + b" ", "test.json")
    assert res.placings["reallocated"] == 1 and not res.raw_unchanged
    with engine.connect() as c:
        assert c.execute(
            text("SELECT count(*) FROM placings WHERE is_current")
        ).scalar_one() == len(placings)
        assert (
            c.execute(text("SELECT count(*) FROM placings WHERE NOT is_current")).scalar_one() == 1
        )


def test_unknown_country_is_refused(engine, loaded):
    import dataclasses

    placings, raw, _ = loaded
    with pytest.raises(LoadError, match="unknown country"), engine.begin() as c:
        load_placings(c, [dataclasses.replace(placings[0], country_code="ZZZ")], raw, "x.json")
