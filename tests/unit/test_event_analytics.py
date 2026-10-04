import json
from pathlib import Path

import pytest

from sie.analytics.events import (
    country_dependence,
    country_sport,
    placings_frame,
    reconcile,
    specialisation,
    sport_concentration,
)
from sie.sources.bornan.parse_medals import MedalRowError, parse_medal_rows

FIX = Path(__file__).parents[1] / "fixtures/sources/bornan"


def load(name):
    return json.loads((FIX / name).read_text())


@pytest.fixture(scope="module")
def df():
    rows = load("SWM_medals_discipline.json") + load("ARC_medals_discipline.json")
    return placings_frame(parse_medal_rows(rows))


def test_reconciles_with_official_standings(df):
    std = {"SWM": load("SWM_medals_standings.json"), "ARC": load("ARC_medals_standings.json")}
    assert reconcile(df, std).empty


def test_reconcile_detects_difference(df):
    std = {"SWM": load("SWM_medals_standings.json"), "ARC": load("ARC_medals_standings.json")}
    std["SWM"][0]["Count"]["ME_GOLD"]["M"] += 1
    assert len(reconcile(df, std)) == 1


def test_parser_drops_personal_data(df):
    assert not any("birth" in c.lower() or "members" in c.lower() for c in df.columns)


def test_duplicate_slot_rejected():
    rows = load("ARC_medals_discipline.json")
    with pytest.raises(MedalRowError):
        parse_medal_rows(rows + rows[:1])


def test_sport_and_country_views(df):
    cs = country_sport(df)
    assert cs["Total"].sum() == len(df)
    assert sport_concentration(cs)["medals"].sum() == len(df)
    assert country_dependence(cs)["medals"].sum() == len(df)
    assert (specialisation(cs)["LQ"] > 0).all()


def test_open_event_gender_comes_from_event_code_not_athlete():
    row = {
        "Medal": "ME_GOLD", "Order": 1, "Org": "JPN", "OrgDesc": "Japan", "Reg": "1", "Type": "A",
        "DateRaw": "2026-10-01T10:00:00+09:00", "Name": "X", "Disc": "ELS", "DiscDesc": "Esports",
        "Event": "O.GT7---------------.FNL-", "EventDesc": "Gran Turismo 7 Finals", "Bib": "1", "Gender": "M",
    }  # fmt: skip
    assert parse_medal_rows([row])[0].gender == "Open"


def test_dashboard_builds_and_embeds_all_rows(tmp_path):
    import pandas as pd

    from sie.dashboard import build

    csv = tmp_path / "p.csv"
    placings_frame(parse_medal_rows(load("ARC_medals_discipline.json"))).drop(
        columns=["entrant_name", "awarded_at", "entrant_type"]
    ).to_csv(csv, index=False)
    out = build(csv, "2026-10-04", tmp_path / "i.html", 6)
    text = out.read_text()
    assert text.count('"m":') == len(pd.read_csv(csv)) and "__DATA__" not in text
