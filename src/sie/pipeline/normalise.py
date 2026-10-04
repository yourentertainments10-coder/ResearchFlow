"""Normalise: resolve a ParsedResult to canonical ids through the reference tables.

Rules (docs/DATA_PIPELINE.md section 5): matching ignores case, punctuation and emoji; a value that is
not in the reference tables is never auto-created, the row is rejected with a reason so the owner can
add an alias and run the import again. Gender and participation come from the structured column first,
then from the event name; the event-name rules are deliberately small and are listed in
``docs/DATA_PIPELINE.md`` section 9.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from sqlalchemy import Connection, text

from sie.pipeline.models import CompetitionRef, NormalisedResult, Reason, Rejection
from sie.reference import load_gender_aliases, normalise_key
from sie.sources.base import ParsedResult

_MEDALS = {"gold": "Gold", "silver": "Silver", "bronze": "Bronze"}
_PARTICIPATION = {"individual": "Individual", "team": "Team", "pair": "Pair"}
_TEAM_WORDS = frozenset({"team", "teams", "relay"})
_PAIR_WORDS = frozenset({"doubles", "pair", "pairs", "duet", "duo"})
_TRUE = frozenset({"1", "true", "yes", "y", "tie"})
_FALSE = frozenset({"", "0", "false", "no", "n"})
# A country cell holding two countries is a multi-country credit (docs/DOMAIN_MODEL.md section 4, rule 7).
_MULTI_COUNTRY_MARKERS = (";", "|")


@dataclass(frozen=True)
class ReferenceIndex:
    """Everything the normaliser needs, keyed by ``normalise_key`` text."""

    competitions: dict[str, CompetitionRef] = field(default_factory=dict)
    countries: dict[str, int] = field(default_factory=dict)  # alias key -> country id
    sport_aliases: dict[str, tuple[int, int | None]] = field(
        default_factory=dict
    )  # -> (sport, discipline)
    disciplines_by_sport: dict[int, dict[str, int]] = field(
        default_factory=dict
    )  # sport -> name key -> id
    double_bronze: frozenset[int] = frozenset()  # sport ids that award two bronzes
    genders: dict[str, str] = field(default_factory=dict)  # alias key -> Men|Women|Mixed|Open


def load_reference_index(conn: Connection, reference_dir: Path) -> ReferenceIndex:
    """Read the seeded reference tables (and gender aliases, which live only in a CSV)."""
    competitions = {
        normalise_key(r.code): CompetitionRef(
            id=r.id,
            code=r.code,
            start_date=r.start_date,
            end_date=r.end_date,
            official_event_total=r.official_event_total,
        )
        for r in conn.execute(
            text("SELECT id, code, start_date, end_date, official_event_total FROM competitions")
        )
    }
    countries = {
        r.alias_norm: r.country_id
        for r in conn.execute(text("SELECT alias_norm, country_id FROM country_aliases"))
    }
    sport_aliases = {
        r.alias_norm: (r.sport_id, r.discipline_id)
        for r in conn.execute(text("SELECT alias_norm, sport_id, discipline_id FROM sport_aliases"))
    }
    disciplines: dict[int, dict[str, int]] = {}
    for r in conn.execute(text("SELECT id, sport_id, name FROM disciplines")):
        disciplines.setdefault(r.sport_id, {})[normalise_key(r.name)] = r.id
    double_bronze = frozenset(
        r.id for r in conn.execute(text("SELECT id FROM sports WHERE double_bronze"))
    )
    return ReferenceIndex(
        competitions=competitions,
        countries=countries,
        sport_aliases=sport_aliases,
        disciplines_by_sport=disciplines,
        double_bronze=double_bronze,
        genders=load_gender_aliases(reference_dir),
    )


def normalise(
    parsed: ParsedResult, ref: ReferenceIndex, competition: CompetitionRef
) -> NormalisedResult | Rejection:
    """Resolve one row, or say precisely why it cannot be resolved."""

    def reject(reason: Reason, detail: str) -> Rejection:
        return Rejection(parsed.row_number, reason, detail, parsed)

    missing = [
        name
        for name in ("competition", "sport", "event", "medal", "country")
        if not getattr(parsed, name)
    ]
    if missing:
        return reject(Reason.MISSING_FIELD, "empty required field(s): " + ", ".join(missing))

    row_competition = normalise_key(parsed.competition)
    if row_competition != normalise_key(competition.code):
        if row_competition in ref.competitions:
            return reject(
                Reason.COMPETITION_MISMATCH,
                f"row is for {parsed.competition!r} but this import is for {competition.code!r}",
            )
        return reject(Reason.UNKNOWN_COMPETITION, f"competition {parsed.competition!r} is unknown")

    medal = _MEDALS.get(parsed.medal.strip().casefold())
    if medal is None:
        return reject(Reason.INVALID_MEDAL, f"medal {parsed.medal!r} is not Gold, Silver or Bronze")

    if any(marker in parsed.country for marker in _MULTI_COUNTRY_MARKERS):
        return reject(
            Reason.MULTI_COUNTRY_ENTRANT,
            f"{parsed.country!r} names more than one country; a placing has exactly one",
        )
    country_id = ref.countries.get(normalise_key(parsed.country))
    if country_id is None:
        return reject(
            Reason.UNKNOWN_COUNTRY,
            f"country {parsed.country!r} is not in the reference tables: add an alias and re-run",
        )

    resolved = _resolve_sport(parsed, ref)
    if isinstance(resolved, Rejection):
        return resolved
    sport_id, discipline_id = resolved

    gender = _resolve_gender(parsed, ref)
    if gender is None:
        return reject(
            Reason.UNKNOWN_GENDER,
            "gender is blank or ambiguous and the event name does not settle it"
            if not parsed.gender
            else f"gender {parsed.gender!r} is not in the reference tables",
        )

    participation = _resolve_participation(parsed, ref)
    if participation is None:
        return reject(
            Reason.INVALID_PARTICIPATION,
            f"participation {parsed.participation!r} is not Individual, Team or Pair",
        )

    slot: int | None = None
    if parsed.slot:
        if not (parsed.slot.isascii() and parsed.slot.isdigit()) or int(parsed.slot) < 1:
            return reject(Reason.INVALID_SLOT, f"slot {parsed.slot!r} is not a whole number from 1")
        slot = int(parsed.slot)

    flag = parsed.is_tie.casefold()
    if flag not in _TRUE and flag not in _FALSE:
        return reject(Reason.INVALID_FLAG, f"is_tie {parsed.is_tie!r} is not yes/no or true/false")

    event_date: date | None = None
    if parsed.date:
        try:
            event_date = date.fromisoformat(parsed.date)
        except ValueError:
            return reject(Reason.INVALID_DATE, f"date {parsed.date!r} is not YYYY-MM-DD")

    return NormalisedResult(
        row_number=parsed.row_number,
        competition_id=competition.id,
        sport_id=sport_id,
        discipline_id=discipline_id,
        event_name=" ".join(parsed.event.split()),
        event_name_raw=parsed.event,
        gender=gender,
        participation=participation,
        medal=medal,
        slot=slot,
        is_tie=flag in _TRUE,
        country_id=country_id,
        entrant=" ".join(parsed.entrant.split()) or None,
        event_date=event_date,
        source_url=parsed.source_url,
        source_note=parsed.source_note,
        parsed=parsed,
    )


def _resolve_sport(parsed: ParsedResult, ref: ReferenceIndex) -> tuple[int, int] | Rejection:
    """Return (sport_id, discipline_id). A sport with several disciplines needs one named."""

    def reject(reason: Reason, detail: str) -> Rejection:
        return Rejection(parsed.row_number, reason, detail, parsed)

    sport_hit = ref.sport_aliases.get(normalise_key(parsed.sport))
    if sport_hit is None:
        return reject(
            Reason.UNKNOWN_SPORT,
            f"sport {parsed.sport!r} is not in the reference tables: add an alias and re-run",
        )
    sport_id, alias_discipline = sport_hit

    if parsed.discipline:
        discipline_hit = ref.sport_aliases.get(normalise_key(parsed.discipline))
        if discipline_hit is None or discipline_hit[1] is None:
            return reject(
                Reason.UNKNOWN_DISCIPLINE,
                f"discipline {parsed.discipline!r} is not in the reference tables",
            )
        if discipline_hit[0] != sport_id:
            return reject(
                Reason.SPORT_DISCIPLINE_MISMATCH,
                f"discipline {parsed.discipline!r} does not belong to sport {parsed.sport!r}",
            )
        return sport_id, discipline_hit[1]

    if alias_discipline is not None:
        return sport_id, alias_discipline
    options = ref.disciplines_by_sport.get(sport_id, {})
    if len(options) == 1:
        return sport_id, next(iter(options.values()))
    return reject(
        Reason.UNKNOWN_DISCIPLINE,
        f"sport {parsed.sport!r} has several disciplines; name one in the discipline column",
    )


def _resolve_gender(parsed: ParsedResult, ref: ReferenceIndex) -> str | None:
    """The gender column if given, otherwise the event name. Gender belongs to the event only."""
    if parsed.gender:
        return ref.genders.get(normalise_key(parsed.gender))
    # Single letters are not read from event names: "x" in "4 x 100m" is not Mixed.
    found = {
        ref.genders[token]
        for token in normalise_key(parsed.event).split()
        if len(token) > 1 and token in ref.genders
    }
    if len(found) > 1:
        found.discard("Open")  # "Men's Open" is a men's event
    return found.pop() if len(found) == 1 else None


def _resolve_participation(parsed: ParsedResult, ref: ReferenceIndex) -> str | None:
    """Explicit column, then event-name words, then 'the entrant is the country itself', else Individual."""
    if parsed.participation:
        return _PARTICIPATION.get(parsed.participation.casefold())
    tokens = set(normalise_key(parsed.event).split())
    if tokens & _TEAM_WORDS:
        return "Team"
    if tokens & _PAIR_WORDS:
        return "Pair"
    if parsed.entrant and normalise_key(parsed.entrant) in ref.countries:
        return "Team"
    return "Individual"
