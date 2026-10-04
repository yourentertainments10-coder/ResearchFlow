"""Small hand-built reference world for unit tests of the pipeline (no database, fake ids)."""

from __future__ import annotations

from datetime import date

from sie.pipeline.models import CompetitionRef, NormalisedResult
from sie.pipeline.normalise import ReferenceIndex
from sie.sources.base import ParsedResult

COMPETITION = CompetitionRef(
    id=1,
    code="asiad-2026",
    start_date=date(2026, 9, 19),
    end_date=date(2026, 10, 4),
    official_event_total=469,
)

IND, KOR, CHN, JPN, THA = 1, 2, 3, 4, 5
ARCHERY, AQUATICS, BOXING, ATHLETICS, BADMINTON = 10, 11, 12, 13, 14
ARCHERY_D, SWIMMING_D, DIVING_D, BOXING_D, ATHLETICS_D, BADMINTON_D = 100, 110, 111, 120, 130, 140


def reference() -> ReferenceIndex:
    return ReferenceIndex(
        competitions={"asiad 2026": COMPETITION},
        countries={
            "ind": IND,
            "india": IND,
            "republic of india": IND,
            "kor": KOR,
            "korea": KOR,
            "chn": CHN,
            "jpn": JPN,
            "japan": JPN,
            "tha": THA,
        },
        sport_aliases={
            "archery": (ARCHERY, ARCHERY_D),
            "aquatics": (AQUATICS, None),
            "swimming": (AQUATICS, SWIMMING_D),
            "diving": (AQUATICS, DIVING_D),
            "boxing": (BOXING, BOXING_D),
            "athletics": (ATHLETICS, ATHLETICS_D),
            "track and field": (ATHLETICS, ATHLETICS_D),
            "badminton": (BADMINTON, BADMINTON_D),
        },
        disciplines_by_sport={
            ARCHERY: {"archery": ARCHERY_D},
            AQUATICS: {"swimming": SWIMMING_D, "diving": DIVING_D},
            BOXING: {"boxing": BOXING_D},
            ATHLETICS: {"athletics": ATHLETICS_D},
            BADMINTON: {"badminton": BADMINTON_D},
        },
        double_bronze=frozenset({BOXING}),
        genders={
            "men": "Men",
            "mens": "Men",
            "male": "Men",
            "m": "Men",
            "women": "Women",
            "womens": "Women",
            "w": "Women",
            "mixed": "Mixed",
            "x": "Mixed",
            "open": "Open",
        },
    )


def parsed(row_number: int = 2, **fields: str) -> ParsedResult:
    base = {
        "competition": "asiad-2026",
        "sport": "Archery",
        "event": "Recurve Men's Individual",
        "medal": "Gold",
        "country": "India",
    }
    return ParsedResult(row_number=row_number, **{**base, **fields})


def row(
    row_number: int,
    medal: str = "Gold",
    country: int = IND,
    *,
    event: str = "Recurve Men's Individual",
    sport: int = ARCHERY,
    discipline: int = ARCHERY_D,
    gender: str = "Men",
    participation: str = "Individual",
    slot: int | None = None,
    is_tie: bool = False,
    entrant: str | None = None,
    event_date: date | None = None,
) -> NormalisedResult:
    return NormalisedResult(
        row_number=row_number,
        competition_id=COMPETITION.id,
        sport_id=sport,
        discipline_id=discipline,
        event_name=event,
        event_name_raw=event,
        gender=gender,
        participation=participation,
        medal=medal,
        slot=slot,
        is_tie=is_tie,
        country_id=country,
        entrant=entrant,
        event_date=event_date,
        source_url="",
        source_note="",
        parsed=parsed(row_number, medal=medal, event=event),
    )
