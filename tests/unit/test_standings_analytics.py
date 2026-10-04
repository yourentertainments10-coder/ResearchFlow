import json
from pathlib import Path

import pytest

from sie.analytics.report import build
from sie.analytics.standings import StandingsError, concentration, summary, to_frame

FIX = Path(__file__).parents[1] / "fixtures/sources/bornan/ALL_medals_standings.decoded.json"


@pytest.fixture(scope="module")
def recs():
    return json.loads(FIX.read_text())


def test_totals_consistent(recs):
    df = to_frame(recs)
    s = summary(df)
    assert s["Total"].sum() == df["n"].sum() == sum(r["Count"]["total"]["total"] for r in recs)
    assert (s[["Men", "Women", "Mixed"]].sum(axis=1) == s["Total"]).all()
    assert s.iloc[0].code == "CHN" and s.iloc[0].Gold == 169


def test_concentration_bounds(recs):
    c = concentration(summary(to_frame(recs)))
    assert 0 < c["hhi_total"] < 10000 and c["top10_share_%"] <= 100


def test_bad_feed_fails_loudly(recs):
    bad = json.loads(json.dumps(recs))
    bad[0]["Count"]["ME_GOLD"]["total"] += 1
    with pytest.raises(StandingsError):
        to_frame(bad)


def test_report_files(recs, tmp_path):
    out = build(recs, tmp_path)
    assert out["xlsx"].stat().st_size > 0 and "Asian Games" in out["html"].read_text()
