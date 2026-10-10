"""Reference data: key normalisation and idempotent seeding from data/reference/*.csv.

Matching rules follow docs/DATA_PIPELINE.md section 5: case-insensitive after trimming and NFKC;
hyphens and slashes become spaces; apostrophes, other punctuation and emoji are dropped
(for MATCHING only). Unknown values are never auto-created.
"""

from __future__ import annotations

import csv
import logging
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Connection, text

log = logging.getLogger(__name__)

_VARIATION_SELECTOR = "\ufe0f"
# Dashes, slashes and underscores become a space, so "Timor-Leste" and "Timor Leste" match.
_SEPARATORS = frozenset("-\u2010\u2011\u2012\u2013\u2014\u2212/\\_")


class ReferenceDataError(ValueError):
    """Reference CSVs are inconsistent. Fix the files; nothing is guessed."""


def normalise_key(value: str) -> str:
    """Canonical matching key: NFKC, casefold, drop punctuation/symbols/emoji, collapse spaces."""
    value = unicodedata.normalize("NFKC", value).casefold()
    kept = []
    for ch in value:
        cat = unicodedata.category(ch)
        if ch in _SEPARATORS:
            kept.append(" ")
        elif ch == _VARIATION_SELECTOR or cat[0] in ("P", "S") or cat == "Cf":
            continue  # apostrophes, commas, emoji and flags are dropped: Men's -> mens
        else:
            kept.append(ch)
    return re.sub(r"\s+", " ", "".join(kept)).strip()


def _read_csv(path: Path, required: set[str]) -> list[dict[str, str]]:
    if not path.exists():
        raise ReferenceDataError(f"missing reference file: {path}")
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ReferenceDataError(f"{path.name}: missing columns {sorted(missing)}")
        return [{k: (v or "").strip() for k, v in row.items()} for row in reader]


def load_gender_aliases(reference_dir: Path) -> dict[str, str]:
    """Return {normalised alias: Men|Women|Mixed|Open}."""
    rows = _read_csv(reference_dir / "gender_aliases.csv", {"alias", "gender"})
    out: dict[str, str] = {}
    for row in rows:
        if row["gender"] not in {"Men", "Women", "Mixed", "Open"}:
            raise ReferenceDataError(f"gender_aliases.csv: bad gender {row['gender']!r}")
        key = normalise_key(row["alias"])
        if out.get(key, row["gender"]) != row["gender"]:
            raise ReferenceDataError(f"gender_aliases.csv: {row['alias']!r} maps to two genders")
        out[key] = row["gender"]
    return out


@dataclass
class SeedResult:
    competitions: int
    countries: int
    country_aliases: int
    sports: int
    disciplines: int
    sport_aliases: int


def _unique_aliases(pairs: list[tuple[str, str]], label: str) -> dict[str, str]:
    """pairs of (alias, target). Two aliases with the same key must point at the same target."""
    out: dict[str, str] = {}
    for alias, target in pairs:
        key = normalise_key(alias)
        if not key:
            raise ReferenceDataError(f"{label}: alias {alias!r} normalises to an empty key")
        if out.setdefault(key, target) != target:
            raise ReferenceDataError(
                f"{label}: alias {alias!r} (key {key!r}) maps to both {out[key]!r} and {target!r}"
            )
    return out


def seed_reference(conn: Connection, reference_dir: Path) -> SeedResult:
    """Load reference CSVs into the database. Safe to run repeatedly (upserts)."""
    competitions = _read_csv(
        reference_dir / "competitions.csv", {"code", "name", "timezone", "start_date", "end_date"}
    )
    countries = _read_csv(reference_dir / "countries.csv", {"code", "name", "region"})
    c_aliases = _read_csv(reference_dir / "country_aliases.csv", {"alias", "country_code"})
    sports = _read_csv(reference_dir / "sports.csv", {"name", "double_bronze"})
    disciplines = _read_csv(reference_dir / "disciplines.csv", {"sport", "name"})
    s_aliases = _read_csv(reference_dir / "sport_aliases.csv", {"alias", "sport", "discipline"})

    country_codes = {r["code"] for r in countries}
    sport_names = {r["name"] for r in sports}
    discipline_keys = {(r["sport"], r["name"]) for r in disciplines}

    for r in c_aliases:
        if r["country_code"] not in country_codes:
            raise ReferenceDataError(f"country_aliases.csv: unknown country {r['country_code']!r}")
    for r in disciplines:
        if r["sport"] not in sport_names:
            raise ReferenceDataError(f"disciplines.csv: unknown sport {r['sport']!r}")
    for r in s_aliases:
        if r["sport"] not in sport_names:
            raise ReferenceDataError(f"sport_aliases.csv: unknown sport {r['sport']!r}")
        if r["discipline"] and (r["sport"], r["discipline"]) not in discipline_keys:
            raise ReferenceDataError(
                f"sport_aliases.csv: unknown discipline {r['sport']}/{r['discipline']}"
            )

    # Every code and canonical name is also an alias of itself.
    country_pairs = [(a["alias"], a["country_code"]) for a in c_aliases]
    country_pairs += [(r["code"], r["code"]) for r in countries]
    country_pairs += [(r["name"], r["code"]) for r in countries]
    country_alias_map = _unique_aliases(country_pairs, "country aliases")

    sport_pairs = [(a["alias"], f"{a['sport']}|{a['discipline']}") for a in s_aliases]
    # A sport's own name resolves to its same-named discipline when it has one (for example
    # Athletics/Athletics), otherwise to the sport alone. Every discipline name is an alias too.
    sport_pairs += [
        (r["name"], f"{r['name']}|{r['name'] if (r['name'], r['name']) in discipline_keys else ''}")
        for r in sports
    ]
    sport_pairs += [
        (r["name"], f"{r['sport']}|{r['name']}")
        for r in disciplines
        if r["name"] not in sport_names
    ]
    sport_alias_map = _unique_aliases(sport_pairs, "sport aliases")

    for r in competitions:
        total = r.get("official_event_total", "")
        if total and not total.isdigit():
            raise ReferenceDataError(
                f"competitions.csv: official_event_total {total!r} is not a whole number"
            )
        conn.execute(
            text(
                """
                INSERT INTO competitions (code, name, timezone, start_date, end_date,
                                          official_event_total)
                VALUES (:code, :name, :timezone, NULLIF(:start_date, '')::date,
                        NULLIF(:end_date, '')::date, NULLIF(:official_event_total, '')::integer)
                ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name, timezone = EXCLUDED.timezone,
                  start_date = EXCLUDED.start_date, end_date = EXCLUDED.end_date,
                  official_event_total = EXCLUDED.official_event_total
                """
            ),
            {"official_event_total": "", **r},
        )
    for r in countries:
        conn.execute(
            text(
                """
                INSERT INTO countries (code, name, region) VALUES (:code, :name, NULLIF(:region, ''))
                ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name, region = EXCLUDED.region
                """
            ),
            r,
        )
    for r in sports:
        conn.execute(
            text(
                """
                INSERT INTO sports (name, double_bronze) VALUES (:name, :double_bronze)
                ON CONFLICT (name) DO UPDATE SET double_bronze = EXCLUDED.double_bronze
                """
            ),
            {
                "name": r["name"],
                "double_bronze": r["double_bronze"].lower() in ("1", "true", "yes"),
            },
        )
    for r in disciplines:
        conn.execute(
            text(
                """
                INSERT INTO disciplines (sport_id, name)
                SELECT id, :name FROM sports WHERE name = :sport
                ON CONFLICT (sport_id, name) DO NOTHING
                """
            ),
            r,
        )
    for key, code in country_alias_map.items():
        conn.execute(
            text(
                """
                INSERT INTO country_aliases (alias_norm, country_id)
                SELECT :key, id FROM countries WHERE code = :code
                ON CONFLICT (alias_norm) DO UPDATE SET country_id = EXCLUDED.country_id
                """
            ),
            {"key": key, "code": code},
        )
    for key, target in sport_alias_map.items():
        sport, discipline = target.split("|", 1)
        conn.execute(
            text(
                """
                INSERT INTO sport_aliases (alias_norm, sport_id, discipline_id)
                SELECT :key, s.id, d.id
                FROM sports s LEFT JOIN disciplines d ON d.sport_id = s.id AND d.name = NULLIF(:disc, '')
                WHERE s.name = :sport
                ON CONFLICT (alias_norm) DO UPDATE
                  SET sport_id = EXCLUDED.sport_id, discipline_id = EXCLUDED.discipline_id
                """
            ),
            {"key": key, "sport": sport, "disc": discipline},
        )

    result = SeedResult(
        competitions=len(competitions),
        countries=len(countries),
        country_aliases=len(country_alias_map),
        sports=len(sports),
        disciplines=len(disciplines),
        sport_aliases=len(sport_alias_map),
    )
    log.info("reference data seeded", extra=result.__dict__)
    return result
