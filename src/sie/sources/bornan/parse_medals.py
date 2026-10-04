"""Pure parser for ``{DISC}/medals/discipline`` rows. No network, no database.

Only the fields analytics needs are kept. Birth dates, ids of persons and member lists are dropped
on purpose (docs/SOURCE_DISCOVERY.md, personal data). Unknown shapes raise; nothing is guessed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

MEDALS = {"ME_GOLD": "Gold", "ME_SILVER": "Silver", "ME_BRONZE": "Bronze"}
GENDERS = {"M": "Men", "W": "Women", "X": "Mixed", "O": "Open"}


class MedalRowError(ValueError):
    """A medal row is not in the shape the parser knows."""


@dataclass(frozen=True)
class ParsedPlacing:
    discipline: str
    discipline_name: str
    event_code: str
    event_name: str
    gender: str
    medal: str
    slot: int
    country_code: str
    country_name: str
    entrant_type: str  # A athlete, T team (as given)
    entrant_name: str
    awarded_at: str


def parse_medal_rows(rows: list[dict[str, Any]]) -> list[ParsedPlacing]:
    out = []
    for r in rows:
        try:
            medal = MEDALS[r["Medal"]]
            gender = GENDERS[r["Gender"]]
            out.append(
                ParsedPlacing(
                    r["Disc"], r["DiscDesc"], r["Event"], r["EventDesc"], gender, medal,
                    int(r["Order"]), r["Org"], r["OrgDesc"], r.get("Type", ""),
                    r.get("Name", ""), r.get("DateRaw", ""),
                )
            )  # fmt: skip
        except (KeyError, ValueError, TypeError) as exc:
            raise MedalRowError(f"unrecognised medal row {r.get('Event')!r}: {exc!r}") from exc
    keys = [(p.event_code, p.medal, p.slot) for p in out]
    if len(keys) != len(set(keys)):
        raise MedalRowError("duplicate (event, medal, slot)")
    return out
