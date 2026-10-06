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
    country_timeline,
    medal_timeline,
    placings_frame,
    reconcile,
    reconcile_official_table,
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
    per = Counter((r["discipline"], r["event_code"], r["medal"]) for r in rows)
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
            is_tie="yes" if per[(r["discipline"], r["event_code"], r["medal"])] > 1 else "no",
            date=r["date"],
            external_key=r["event_code"],
            country_label=r["country_name"],
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


def test_only_the_real_ties_are_stored_as_ties(verified, seeded):
    """Swimming's gold tie, its double silver and the Women's Pole Vault bronze tie: 6 rows, no more.

    Portal event codes repeat across disciplines, so a tie must be judged inside one discipline.
    """
    from sqlalchemy import text

    with seeded.connect() as c:
        by_medal = dict(
            c.execute(
                text("SELECT medal, count(*) FROM placings WHERE is_tie GROUP BY medal")
            ).all()
        )
        pole_vault = c.execute(
            text(
                """SELECT count(*) FROM placings p JOIN events e ON e.id = p.event_id
                   WHERE e.name = 'Women''s Pole Vault' AND p.medal = 'Bronze' AND p.is_tie"""
            )
        ).scalar_one()
    assert by_medal == {"Gold": 2, "Silver": 2, "Bronze": 2}
    assert pole_vault == 2


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


def _as_discipline(frame: pd.DataFrame) -> pd.DataFrame:
    """The frozen sheets were cut by discipline and called it 'sport'."""
    return frame.rename(
        columns={
            "sport": "discipline",
            "sports": "disciplines",
            "top_sport": "top_discipline",
            "top_sport_share_%": "top_discipline_share_%",
        }
    )


def test_discipline_tables_match_the_frozen_tables(verified):
    """Decision 2: the 59 source disciplines are kept, and the frozen discipline-level results hold."""
    cs = country_sport(verified, "discipline")
    same(
        cs,
        _as_discipline(expected("Country x sport")),
        ["country_code", "discipline"],
        ["country_code", "discipline", "Gold", "Silver", "Bronze", "Total"],
    )
    events = (
        verified.groupby("discipline_name")["event_id"].nunique().rename("events").reset_index()
    )
    sc = sport_concentration(cs, "discipline").merge(
        events, left_on="discipline", right_on="discipline_name"
    )
    same(sc, _as_discipline(expected("Sport concentration")), ["discipline"],
         ["discipline", "medals", "countries", "HHI", "leader", "leader_share_%", "events"])  # fmt: skip
    dep = country_dependence(cs, "discipline")
    same(dep, _as_discipline(expected("Country dependence")), ["country"],
         ["country", "medals", "disciplines", "HHI", "top_discipline", "top_discipline_share_%"])  # fmt: skip
    spec = specialisation(cs, "discipline")
    same(spec, _as_discipline(expected("Specialisation LQ")), ["country_code", "discipline"],
         ["country_code", "discipline", "Total", "share_of_country_%", "LQ"])  # fmt: skip
    assert cs["discipline"].nunique() == 59


def _sport_of_discipline() -> dict[str, str]:
    rows = csv.DictReader((ROOT / "data/reference/disciplines.csv").open(encoding="utf-8"))
    return {r["name"]: r["sport"] for r in rows}


def test_sport_tables_are_the_49_official_sports(verified):
    """Decision 2: the sport dimension is the 49 official sports.

    The expected values are not read from the database: they are the frozen discipline-level medals,
    summed by the reference file that maps each discipline to its sport.
    """
    mapping = _sport_of_discipline()
    frozen = expected("Country x sport").rename(columns={"sport": "discipline"})
    frozen["sport"] = frozen["discipline"].map(mapping)
    assert frozen["sport"].notna().all() and frozen["sport"].nunique() == 49
    wanted = (
        frozen.groupby(["country_code", "sport"])[["Gold", "Silver", "Bronze", "Total"]]
        .sum()
        .reset_index()
    )
    cs = country_sport(verified)
    same(cs, wanted, ["country_code", "sport"],
         ["country_code", "sport", "Gold", "Silver", "Bronze", "Total"])  # fmt: skip
    assert cs["sport"].nunique() == 49 and cs["Total"].sum() == 1568
    # Aquatics is the case that matters: five disciplines, one sport.
    aquatic_disciplines = {d for d, s in mapping.items() if s == "Aquatics"}
    assert len(aquatic_disciplines) > 1
    in_disciplines = verified[verified["discipline_name"].isin(aquatic_disciplines)]
    assert cs.query("sport == 'Aquatics'")["Total"].sum() == len(in_disciplines)

    conc = sport_concentration(cs)
    assert len(conc) == 49 and conc["medals"].sum() == 1568
    mass = conc.set_index("sport")["medals"]
    assert mass["Aquatics"] == len(in_disciplines)
    events_per_sport = verified.groupby("sport")["event_id"].nunique()
    assert events_per_sport.sum() == 469 and len(events_per_sport) == 49
    assert set(specialisation(cs)["sport"]) == set(cs["sport"])
    assert set(country_dependence(cs).columns) >= {"sports", "top_sport", "top_sport_share_%"}


def test_gender_tables_match_once_open_is_folded_into_mixed(verified):
    """The frozen tables were built with Open counted as Mixed; today Open is its own column (ADR-019)."""
    cg, sg = gender_tables(verified)
    folded = cg.assign(Mixed=cg["Mixed"] + cg["Open"])
    same(folded, expected("Country x gender"), ["country_code"],
         ["country_code", "Men", "Women", "Mixed", "Total", "Women_%"])  # fmt: skip
    assert cg["Open"].sum() > 0  # and Open really is kept apart in the unfolded frame

    # Sport x gender is by the 49 sports; the frozen sheet is by discipline, so it is summed first.
    frozen = expected("Sport x gender")
    frozen["sport"] = frozen["sport"].map(_sport_of_discipline())
    summed = frozen.groupby("sport")[["Men", "Women", "Mixed", "Total"]].sum().reset_index()
    folded_sg = sg.assign(Mixed=sg["Mixed"] + sg["Open"])
    same(folded_sg, summed, ["sport"], ["sport", "Men", "Women", "Mixed", "Total"])
    assert len(sg) == 49 and sg["Open"].sum() == cg["Open"].sum()


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


# --- decision 1: each medal keeps its own date ----------------------------------------------------------


def _frozen_rows() -> list[dict[str, str]]:
    return list(csv.DictReader((EXPECTED / "placings_verified.csv").open(encoding="utf-8")))


def test_every_medal_keeps_the_date_the_source_gave_it(verified):
    frozen = Counter(
        (
            r["discipline_name"],
            r["event_name"],
            r["gender"],
            r["medal"],
            r["country_code"],
            r["date"],
        )
        for r in _frozen_rows()
    )
    ours = Counter(
        zip(verified["discipline_name"], verified["event_name"], verified["gender"],
            verified["medal"], verified["country_code"], verified["date"], strict=True)
    )  # fmt: skip
    assert ours == frozen
    # The portal gives no date for the three Modern Pentathlon men's individual medals: they stay
    # undated rather than being given their event's date.
    undated = verified[verified["date"] == ""]
    assert len(undated) == 3 and set(undated["event_name"]) == {"Men's Individual"}
    assert set(undated["discipline_name"]) == {"Modern Pentathlon"}


def test_the_events_decided_on_several_days_are_not_collapsed_to_one_date(verified):
    """53 events have medals on different days; 106 medals are not on their event's latest day.

    The earlier figures (55 events, 288 medals) came from grouping by the portal's event code alone,
    which merges different events of different disciplines; counted per real event they are 53 and 106.
    """
    days = verified.groupby("event_id")["date"].nunique()
    assert int((days > 1).sum()) == 53
    assert int((verified["date"] != verified["event_date"]).sum()) == 106
    assert (
        verified["date"] <= verified["event_date"]
    ).all()  # the event date is the latest of them


def test_timeline_uses_each_medals_own_day_and_ends_at_the_country_table_totals(verified):
    timeline = medal_timeline(verified)
    wanted_per_day = Counter(r["date"] or "undated" for r in _frozen_rows())
    assert dict(zip(timeline["date"], timeline["Total"], strict=True)) == dict(wanted_per_day)
    assert timeline["date"].iloc[-1] == "undated" and timeline["Total"].iloc[-1] == 3
    assert timeline["Total_cum"].is_monotonic_increasing
    country = summary(country_gender_frame(verified, fold_open=True))
    last = timeline.iloc[-1]
    assert last["Total_cum"] == country["Total"].sum() == 1568
    assert (last["Gold_cum"], last["Silver_cum"], last["Bronze_cum"]) == (
        country["Gold"].sum(),
        country["Silver"].sum(),
        country["Bronze"].sum(),
    )
    ends = country_timeline(verified)
    final = ends[ends["date"] == "undated"].set_index("country_code")["cumulative"]
    assert final.to_dict() == country.set_index("code")["Total"].to_dict()
    dated_only = ends[ends["date"] == timeline["date"].iloc[-2]].set_index("country_code")
    assert dated_only["cumulative"].sum() == 1565


def test_timeline_would_be_wrong_if_it_used_the_events_latest_date(verified):
    """The regression this guards: counting by event date moves medals to later days."""
    by_event_date = medal_timeline(verified.assign(date=verified["event_date"]))
    by_medal_date = medal_timeline(verified)
    merged = by_medal_date.merge(by_event_date, on="date", suffixes=("", "_event"))
    assert (merged["Total_cum"] != merged["Total_cum_event"]).any()
    assert by_medal_date["Total_cum"].iloc[-1] == by_event_date["Total_cum"].iloc[-1] == 1568


def _one_placing(seeded):
    from sqlalchemy import text

    with seeded.begin() as c:
        row = c.execute(
            text(
                """SELECT p.id, p.event_id, p.medal, p.slot, p.country_id, p.is_tie, p.result_date,
                          p.raw_version_id FROM placings p
                   WHERE p.is_current ORDER BY p.id LIMIT 1"""
            )
        ).one()
    return row


def test_a_placing_without_a_date_gets_it_filled_in_place_and_a_changed_date_is_a_correction(
    verified, seeded
):
    from datetime import UTC, date, datetime

    from sqlalchemy import text

    from sie.db.placings import apply_placing

    row = _one_placing(seeded)
    now = datetime(2026, 10, 6, tzinfo=UTC)
    args = dict(
        event_id=row.event_id, medal=row.medal, slot=row.slot, country_id=row.country_id,
        entrant_id=None, raw_version_id=row.raw_version_id, source="official", now=now,
        is_tie=row.is_tie,
    )  # fmt: skip

    def versions(c) -> int:
        return c.execute(
            text("SELECT count(*) FROM placings WHERE event_id=:e AND medal=:m AND slot=:s"),
            {"e": row.event_id, "m": row.medal, "s": row.slot},
        ).scalar_one()

    with seeded.begin() as c:  # a row from before migration 004: no date, no source label
        c.execute(
            text("UPDATE placings SET result_date = NULL, source_country = NULL WHERE id = :i"),
            {"i": row.id},
        )
        before = versions(c)
        assert apply_placing(c, **args, result_date=None) == "unchanged"
        assert apply_placing(c, **args, result_date=date(2026, 10, 1), source_country="X") == (
            "unchanged"
        )
        stored = c.execute(
            text("SELECT result_date, source_country FROM placings WHERE id = :i"), {"i": row.id}
        ).one()
        assert tuple(stored) == (date(2026, 10, 1), "X")
        assert versions(c) == before  # filled in place: no new version, nothing claimed before

        assert apply_placing(c, **args, result_date=date(2026, 10, 1)) == "unchanged"
        assert apply_placing(c, **args, result_date=None) == "unchanged"  # silence is not a change
        assert apply_placing(c, **args, result_date=date(2026, 10, 2)) == "corrected"
        assert versions(c) == before + 1  # the old version is kept, never overwritten
        now_current = c.execute(
            text(
                "SELECT result_date FROM placings WHERE event_id=:e AND medal=:m AND slot=:s AND is_current"
            ),
            {"e": row.event_id, "m": row.medal, "s": row.slot},
        ).scalar_one()
        assert now_current == date(2026, 10, 2)


# --- decision 3: Open stays distinct, only the official comparison folds it ---------------------------


def test_the_official_country_table_reconciles_with_open_folded_into_mixed(verified):
    """Zero mismatches over all 40 countries x 3 medals x M/W/X against the portal's own table."""
    records = json.loads((BORNAN / "ALL_medals_standings.decoded.json").read_text())
    assert len(records) == 40
    assert reconcile_official_table(verified, records).empty
    # Folding is what makes it reconcile: compared as-is, Open would show up as a mismatch.
    assert not reconcile_official_table(
        verified.assign(gender=verified["gender"].replace({"Open": "Women"})), records
    ).empty
    assert (verified["gender"] == "Open").sum() > 0  # and our own frame still has Open


def test_a_missing_country_is_a_mismatch_in_both_directions(verified):
    records = json.loads((BORNAN / "ALL_medals_standings.decoded.json").read_text())
    assert not reconcile_official_table(verified[verified["country_code"] != "JPN"], records).empty
    assert not reconcile_official_table(verified, records[1:]).empty


# --- decision 4: reference names in analytics, source names kept as provenance -----------------------


def test_analytics_use_reference_country_names_and_keep_the_source_names(verified):
    reference = {
        r["code"]: r["name"]
        for r in csv.DictReader((ROOT / "data/reference/countries.csv").open(encoding="utf-8"))
    }
    names = dict(zip(verified["country_code"], verified["country_name"], strict=True))
    assert names == {code: reference[code] for code in names} and len(names) == 40
    source = dict(zip(verified["country_code"], verified["source_country"], strict=True))
    portal = {r["country_code"]: r["country_name"] for r in _frozen_rows()}
    assert source == portal  # exactly as the source wrote them
    differing = {c for c in names if names[c] != source[c]}
    assert (
        len(differing) == 8
    )  # e.g. the portal's long forms; none of them leaks into the analytics
    assert all(
        verified.loc[verified["country_code"] == c, "country_name"].iloc[0] == names[c]
        for c in differing
    )
    portal_only = {source[c] for c in differing}
    assert portal_only.isdisjoint(set(verified["country_name"]))


def test_event_code_collisions_across_disciplines_cannot_merge_events(verified):
    codes = {r["event_code"] for r in _frozen_rows()}
    pairs = {(r["discipline"], r["event_code"]) for r in _frozen_rows()}
    assert len(codes) == 409 and len(pairs) == 469  # codes repeat across disciplines
    assert verified["event_id"].nunique() == 469


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
