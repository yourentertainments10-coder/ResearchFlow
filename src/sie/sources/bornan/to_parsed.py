"""Turn a decoded portal capture into the shared ``ParsedResult`` rows. Pure: bytes in, rows out.

The capture is the JSON the owner's browser snippet saves: ``{"medals": {DISC: [medal rows]}}``.
Personal data never leaves this module: athlete names are not copied (``entrant`` stays empty), because
the pipeline stores placings per country (docs/SOURCE_DISCOVERY.md, personal data rule).
"""

from __future__ import annotations

import json
from collections import Counter

from sie.sources.base import ParsedResult
from sie.sources.bornan.parse_medals import ParsedPlacing, parse_medal_rows

SOURCE = "official"  # the source name used for runs, raw documents, placings and source priority


def capture_to_parsed(raw: bytes, competition: str) -> list[ParsedResult]:
    capture = json.loads(raw)
    placings = [p for rows in capture["medals"].values() for p in parse_medal_rows(rows)]
    return placings_to_parsed(placings, competition)


def placings_to_parsed(placings: list[ParsedPlacing], competition: str) -> list[ParsedResult]:
    per_medal = Counter((p.discipline, p.event_code, p.medal) for p in placings)
    team_events = {(p.discipline, p.event_code) for p in placings if p.entrant_type == "T"}
    return [
        ParsedResult(
            row_number=number,
            competition=competition,
            sport=p.discipline_name,
            event=p.event_name,
            gender=p.gender,
            medal=p.medal,
            country=p.country_code,
            country_label=p.country_name,
            participation="Team" if (p.discipline, p.event_code) in team_events else "Individual",
            slot=str(p.slot),
            # Two placings of one medal are marked as a tie. The validator decides what a second
            # bronze means: a normal outcome in a double-bronze sport, a tie (Pole Vault) elsewhere.
            is_tie="yes" if per_medal[(p.discipline, p.event_code, p.medal)] > 1 else "no",
            date=p.awarded_at[:10],
            external_key=p.event_code,
        )
        for number, p in enumerate(placings, start=1)
    ]
