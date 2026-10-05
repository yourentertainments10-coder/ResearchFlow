"""The reference data carries the sport > discipline grouping (docs/DOMAIN_MODEL.md, ADR-020).

Pure file checks: no database. The portal fixture is the verified list of 59 disciplines.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REF = ROOT / "data" / "reference"
PORTAL = ROOT / "tests/fixtures/sources/bornan/ALL_disc_data.trimmed.json"


def _rows(name: str) -> list[dict[str, str]]:
    with (REF / name).open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def test_every_portal_discipline_is_a_listed_discipline_of_exactly_one_sport():
    portal = [d["DiscDesc"] for d in json.loads(PORTAL.read_text(encoding="utf-8"))]
    listed = [r["name"] for r in _rows("disciplines.csv")]
    assert len(portal) == 59
    assert sorted(portal) == sorted(listed)  # none missing, none invented, none twice


def test_official_sports_group_their_disciplines_instead_of_one_sport_per_discipline():
    by_sport: dict[str, set[str]] = {}
    for r in _rows("disciplines.csv"):
        by_sport.setdefault(r["sport"], set()).add(r["name"])
    assert by_sport["Aquatics"] == {"Swimming", "Diving", "Artistic Swimming", "Water Polo"}
    assert len(by_sport["Cycling"]) == 5
    assert len(by_sport["Gymnastics"]) == 3
    assert by_sport["Canoe"] == {"Canoe Sprint", "Canoe Slalom"}
    assert len(by_sport) == 49  # not 59


def test_every_sport_has_a_discipline_and_every_discipline_a_known_sport():
    sports = {r["name"] for r in _rows("sports.csv")}
    assert sports == {r["sport"] for r in _rows("disciplines.csv")}


def test_double_bronze_flags_follow_the_official_data():
    flagged = {r["name"] for r in _rows("sports.csv") if r["double_bronze"] == "true"}
    assert {"Boxing", "Judo", "Badminton", "Fencing", "Squash", "Table Tennis"} <= flagged
    assert "Athletics" not in flagged  # its single slot-2 bronze is a tie
    assert "Archery" not in flagged and "Aquatics" not in flagged
