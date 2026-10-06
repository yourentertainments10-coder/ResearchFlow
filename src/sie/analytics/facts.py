"""The single input of medal analytics: ``v_medal_facts`` (read through ``reporting.medal_facts``).

docs/ANALYTICS_SPEC.md: all metrics are computed from ``v_medal_facts``. Nothing in this package reads
parsed placings, capture files or the raw store to count medals. The only other inputs are the
official tables, used as independent references to check our numbers against, never to produce them.

Names: ``sport`` is the official sport (49), ``discipline`` the source's finer split (59); ``country``
is the reference name, whatever the source called the country.

Grain: one row per country medal (a placing is current, credited to one country).
"""

from __future__ import annotations

from itertools import product

import pandas as pd
from sqlalchemy import Connection, text

FACT_COLUMNS = (
    "competition_id",
    "event_id",
    "placing_id",
    "medal",
    "slot",
    "is_tie",
    "country_code",
    "country",
    "sport",
    "discipline",
    "gender",
    "participation",
    "event",
    "event_date",
    "is_disputed",
    "entrant",
    "result_date",
    "source_country",
)
MEDALS = ("Gold", "Silver", "Bronze")
GENDERS = ("Men", "Women", "Mixed")

# The official table has no Open column: it counts Open events under Mixed. Anything that is compared
# with or published like the official table says so explicitly through ``fold_open``. Whether the
# project's own tables should keep Open separate everywhere is the owner's decision (ADR-019).
OPEN_AS_MIXED = {"Open": "Mixed"}


class FactsError(ValueError):
    """The facts cannot be turned into an analysis frame without guessing."""


def competition_id(conn: Connection, code: str) -> int:
    found = conn.execute(
        text("SELECT id FROM competitions WHERE code = :c"), {"c": code}
    ).scalar_one_or_none()
    if found is None:
        raise FactsError(f"competition {code!r} is not in the database")
    return found


def load_medal_facts(conn: Connection, competition: int) -> pd.DataFrame:
    """Every current country medal of one competition, in a stable order."""
    rows = conn.execute(
        text(
            """SELECT competition_id, event_id, placing_id, medal, slot, is_tie, country_code, country,
                      sport, discipline, gender, participation, event, event_date, is_disputed, entrant,
                      result_date, source_country
               FROM reporting.medal_facts
               WHERE competition_id = :c
               ORDER BY event_id, medal, slot, country_code"""
        ),
        {"c": competition},
    ).all()
    return pd.DataFrame(rows, columns=list(FACT_COLUMNS))


def analysis_frame(facts: pd.DataFrame, discipline_codes: dict[str, str]) -> pd.DataFrame:
    """Facts under the column names the metric functions use.

    ``discipline_codes`` maps a discipline name to the official portal's code, which the official
    per-discipline standings are keyed by. A discipline without a code is an error, not a guess.
    """
    unknown = sorted(set(facts["discipline"]) - set(discipline_codes))
    if unknown:
        raise FactsError(f"no official code for discipline(s): {unknown}")
    return pd.DataFrame(
        {
            "country_code": facts["country_code"],
            "country_name": facts["country"],
            "discipline": facts["discipline"].map(discipline_codes),
            "discipline_name": facts["discipline"],
            "sport": facts["sport"],
            "event_id": facts["event_id"],
            "event_name": facts["event"],
            "gender": facts["gender"],
            "medal": facts["medal"],
            "slot": facts["slot"],
            # The day this medal was decided, not the event's latest date: 55 events are decided on
            # several days, and a medal timeline must put each medal on its own day (ADR-025).
            "date": facts["result_date"].map(lambda d: d.isoformat() if pd.notna(d) else ""),
            "event_date": facts["event_date"].map(lambda d: d.isoformat() if pd.notna(d) else ""),
            # What the source called the country; analytics and display use ``country_name``
            # (the reference name), this is provenance only (ADR-026).
            "source_country": facts["source_country"],
        }
    )


def country_gender_frame(df: pd.DataFrame, *, fold_open: bool) -> pd.DataFrame:
    """Long frame ``code, country, medal, gender, n`` with every medal x gender cell present.

    ``fold_open=True`` reproduces the official table's three gender buckets (Open counted as Mixed).
    ``fold_open=False`` keeps Open as its own gender and adds it to the grid.
    """
    genders = GENDERS if fold_open else (*GENDERS, "Open")
    gender = df["gender"].replace(OPEN_AS_MIXED) if fold_open else df["gender"]
    counts = (
        df.assign(gender=gender).groupby(["country_code", "country_name", "medal", "gender"]).size()
    )
    countries = sorted(set(zip(df["country_code"], df["country_name"], strict=True)))
    rows = [
        (code, name, medal, g, int(counts.get((code, name, medal, g), 0)))
        for (code, name), medal, g in product(countries, MEDALS, genders)
    ]
    return pd.DataFrame(rows, columns=["code", "country", "medal", "gender", "n"])
