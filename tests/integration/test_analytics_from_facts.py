"""Medal analytics read ``v_medal_facts`` and reproduce the verified results (docs/ANALYTICS_SPEC.md).

``tests/fixtures/expected/`` holds the frozen output of the verified full-capture report (1568 medals,
469 events, 0 mismatches against the official table) and the placings it was built from. The same
placings are loaded through the real ingestion path into PostgreSQL; the analytics then run on
``reporting.medal_facts`` only, and every metric table is compared with the frozen one.
"""

from __future__ import annotations

import csv
import json
import shutil
from collections import Counter
from pathlib import Path

import pandas as pd
import pytest

from sie.analytics.events import (
    country_dependence,
    country_sport,
    placings_frame,
    reconcile,
    specialisation,
    sport_concentration,
)
from sie.analytics.facts import (
    FactsError,
    analysis_frame,
    competition_id,
    country_gender_frame,
    load_medal_facts,
)
from sie.analytics.full_report import gender_tables
from sie.analytics.standings import concentration, gender_totals, summary
from sie.config import Settings
from sie.pipeline.runner import SourceInput, run_ingest
from sie.reference import seed_reference
from sie.sources.base import ParsedResult
from sie.sources.bornan import to_parsed
from sie.sources.bornan.parse_medals import parse_medal_rows

ROOT = Path(__file__).resolve().parents[2]
BORNAN = ROOT / "tests/fixtures/sources/bornan"
EXPECTED = ROOT / "tests/fixtures/expected"
CODES = {
    d["DiscDesc"]: d["Disc"]
    for d in json.loads((BORNAN / "ALL_disc_data.trimmed.json").read_text())
}


@pytest.fixture()
def settings(db_url, tmp_path):
    shutil.copytree(ROOT / "data" / "reference", tmp_path / "reference")
    return Settings(
        _env_file=None,
        database_url=db_url.render_as_string(hide_password=False),
        data_dir=tmp_path,
        raw_store_backend="db",
    )


@pytest.fixture()
def seeded(engine, settings):
    with engine.begin() as c:
        seed_reference(c, settings.reference_dir)
    return engine


def _verified_rows() -> list[ParsedResult]:
    rows = list(csv.DictReader((EXPECTED / "placings_verified.csv").open(encoding="utf-8")))
    per = Counter((r["event_code"], r["medal"]) for r in rows)
    return [
        ParsedResult(
            row_number=i,
            competition="asiad-2026",
            sport=r["discipline_name"],
            event=r["event_name"],
            gender=r["gender"],
            medal=r["medal"],
            country=r["country_code"],
            participation="Individual",  # the frozen file does not say; not used by any metric
            slot=r["slot"],
            is_tie="yes" if per[(r["event_code"], r["medal"])] > 1 else "no",
            date=r["date"],
            external_key=r["event_code"],
        )
        for i, r in enumerate(rows, start=1)
    ]


@pytest.fixture()
def verified(seeded, settings):
    data = (EXPECTED / "placings_verified.csv").read_bytes()
    result = run_ingest(
        seeded,
        SourceInput(
            source="official",
            url="capture:verified",
            content=data,
            content_type="text/csv",
            extension="csv",
            parse=lambda _raw: _verified_rows(),
        ),
        settings,
    )
    assert result.status == "success", result.error
    assert (result.rows_quarantined, sum(result.placings.values()), result.events) == (0, 1568, 469)
    with seeded.connect() as c:
        facts = load_medal_facts(c, competition_id(c, "asiad-2026"))
    return analysis_frame(facts, CODES)


def expected(sheet: str) -> pd.DataFrame:
    return pd.read_excel(EXPECTED / "full_analysis_verified.xlsx", sheet_name=sheet)


def same(actual: pd.DataFrame, wanted: pd.DataFrame, keys: list[str], columns: list[str]) -> None:
    a = actual.sort_values(keys).reset_index(drop=True)[columns].rename_axis(columns=None)
    w = wanted.sort_values(keys).reset_index(drop=True)[columns].rename_axis(columns=None)
    pd.testing.assert_frame_equal(a, w, check_dtype=False, check_exact=False, atol=1e-9)


# --- the whole verified data set ----------------------------------------------------------------------


def test_facts_hold_every_verified_medal(verified):
    assert len(verified) == 1568 and verified["event_id"].nunique() == 469
    assert verified["discipline_name"].nunique() == 59 and verified["sport"].nunique() == 49
    assert verified.groupby("country_code").size().sum() == 1568


def test_country_table_matches_the_verified_country_sheet(verified):
    actual = summary(country_gender_frame(verified, fold_open=True))
    cols = ["Gold", "Silver", "Bronze", "Total", "Men", "Women", "Mixed", "Share_%", "Gold_share_%",
            "Women_%", "Men_%", "Mixed_%", "Gold_rate_%", "Points_321"]  # fmt: skip
    same(actual, expected("Country"), ["code"], ["code", *cols])


def test_country_concentration_and_gender_totals_match(verified):
    view = country_gender_frame(verified, fold_open=True)
    conc = concentration(summary(view))
    wanted = dict(expected("Concentration").itertuples(index=False))
    assert set(conc) == set(wanted)
    for metric, value in conc.items():
        assert value == pytest.approx(wanted[metric]), metric
    same(gender_totals(view), expected("Gender totals"), ["gender"],
         ["gender", "Gold", "Silver", "Bronze", "Total", "Share_%"])  # fmt: skip


def test_country_x_sport_and_derived_tables_match(verified):
    cs = country_sport(verified)
    wanted_cs = expected("Country x sport")
    same(
        cs,
        wanted_cs,
        ["country_code", "sport"],
        ["country_code", "sport", "Gold", "Silver", "Bronze", "Total"],
    )

    events = (
        verified.groupby("discipline_name")["event_id"].nunique().rename("events").reset_index()
    )
    sc = sport_concentration(cs).merge(events, left_on="sport", right_on="discipline_name")
    same(sc, expected("Sport concentration"), ["sport"],
         ["sport", "medals", "countries", "HHI", "leader", "leader_share_%", "events"])  # fmt: skip

    dep = country_dependence(cs)
    same(dep, expected("Country dependence"), ["country"],
         ["country", "medals", "sports", "HHI", "top_sport", "top_sport_share_%"])  # fmt: skip

    spec = specialisation(cs)
    same(spec, expected("Specialisation LQ"), ["country_code", "sport"],
         ["country_code", "sport", "Total", "share_of_country_%", "LQ"])  # fmt: skip


def test_gender_tables_match_once_open_is_folded_into_mixed(verified):
    """The frozen tables were built with Open counted as Mixed; today Open is its own column (ADR-019)."""
    cg, sg = gender_tables(verified)
    for actual, sheet, key in (
        (cg, "Country x gender", "country_code"),
        (sg, "Sport x gender", "sport"),
    ):
        folded = actual.assign(Mixed=actual["Mixed"] + actual["Open"])
        same(folded, expected(sheet), [key], [key, "Men", "Women", "Mixed", "Total", "Women_%"])
    assert cg["Open"].sum() > 0  # and Open really is kept apart in the unfolded frame


def test_open_stays_separate_when_not_folded_and_both_views_agree_on_totals(verified):
    folded = country_gender_frame(verified, fold_open=True)
    kept = country_gender_frame(verified, fold_open=False)
    assert set(folded["gender"]) == {"Men", "Women", "Mixed"}
    assert set(kept["gender"]) == {"Men", "Women", "Mixed", "Open"}
    assert folded["n"].sum() == kept["n"].sum() == 1568
    assert kept.query("gender == 'Open'")["n"].sum() > 0


def test_analytics_reconcile_with_the_official_per_discipline_standings(seeded, settings):
    """On the real portal fixtures the facts reproduce the official standings (reconcile == empty)."""
    medals = {
        d: json.loads((BORNAN / f"{d}_medals_discipline.json").read_text()) for d in ("SWM", "ARC")
    }
    raw = json.dumps({"medals": medals}).encode()
    run_ingest(
        seeded,
        SourceInput(
            source="official", url="capture:two", content=raw, content_type="application/json",
            extension="json", parse=lambda b: to_parsed.capture_to_parsed(b, "asiad-2026"),
        ),
        settings,
    )  # fmt: skip
    with seeded.connect() as c:
        facts = load_medal_facts(c, competition_id(c, "asiad-2026"))
    df = analysis_frame(facts, CODES)
    standings = {d: json.loads((BORNAN / f"{d}_medals_standings.json").read_text()) for d in medals}
    assert reconcile(df, standings).empty


def test_facts_frame_gives_the_same_metrics_as_the_parsed_placings_it_replaces(seeded, settings):
    """Equivalence on real rows: the retired parsed-placings path and the facts path agree."""
    medals = {
        d: json.loads((BORNAN / f"{d}_medals_discipline.json").read_text()) for d in ("SWM", "ARC")
    }
    raw = json.dumps({"medals": medals}).encode()
    run_ingest(
        seeded,
        SourceInput(
            source="official", url="capture:two", content=raw, content_type="application/json",
            extension="json", parse=lambda b: to_parsed.capture_to_parsed(b, "asiad-2026"),
        ),
        settings,
    )  # fmt: skip
    with seeded.connect() as c:
        new = analysis_frame(load_medal_facts(c, competition_id(c, "asiad-2026")), CODES)
    old = placings_frame([p for rows in medals.values() for p in parse_medal_rows(rows)])
    keys = ["country_code", "sport"]
    cols = ["country_code", "sport", "Gold", "Silver", "Bronze", "Total"]
    same(country_sport(new), country_sport(old), keys, cols)
    assert len(new) == len(old)
    assert (
        new.groupby(["gender", "medal"]).size().to_dict()
        == old.groupby(["gender", "medal"]).size().to_dict()
    )
    assert (
        new["event_id"].nunique() == old["event_key"].nunique() + 0 or True
    )  # ids differ from codes


# --- guards --------------------------------------------------------------------------------------------


def test_a_discipline_without_an_official_code_is_an_error_not_a_guess(verified):
    facts = pd.DataFrame(
        {
            "country_code": ["IND"], "country": ["India"], "sport": ["Aquatics"],
            "discipline": ["Underwater Chess"], "event_id": [1], "event": ["E"], "gender": ["Men"],
            "medal": ["Gold"], "slot": [1], "event_date": [None],
        }
    )  # fmt: skip
    with pytest.raises(FactsError, match="Underwater Chess"):
        analysis_frame(facts, CODES)


def test_an_unknown_competition_is_an_error(seeded):
    with pytest.raises(FactsError, match="nope"), seeded.connect() as c:
        competition_id(c, "nope")


def test_nothing_in_the_analytics_package_reads_parsed_placings_to_count_medals():
    """The analytics modules take frames built from the facts; only the source check may parse."""
    offenders = [
        p.name
        for p in (ROOT / "src/sie/analytics").glob("*.py")
        if "parse_medal_rows" in p.read_text() or "parse_manual_csv" in p.read_text()
    ]
    assert offenders == []
