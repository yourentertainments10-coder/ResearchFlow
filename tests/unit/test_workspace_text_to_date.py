"""Text to dates: strict, one stated format, never guessed, checked by a second path. Numbers by hand."""

from __future__ import annotations

import pandas as pd
import pytest

from sie.datasets import Limits, LoadOptions, open_dataset
from sie.datasets.models import OperationError
from sie.workspace import start_session
from sie.workspace.clean_verify import verify_cleaning
from sie.workspace.cleaning import (
    DATE_FORMAT_WORDS,
    DATE_FORMATS,
    TextToDate,
    apply_steps,
    describe,
    parse_step,
    step_title,
)

DMY = b"when,amt\n31/12/2025,5\n01/02/2025,7\n15/03/2025,9\n,2\n"


def ds(data: bytes):
    return open_dataset(data, "d.csv", LoadOptions(), Limits())


def test_converts_and_the_independent_check_agrees():
    d = ds(DMY)
    step = TextToDate(column="when", format="%d/%m/%Y")
    out = apply_steps(d, [step])
    got = out.dataset.frame["when"]
    assert [None if pd.isna(v) else v.date().isoformat() for v in got] == [
        "2025-12-31", "2025-02-01", "2025-03-15", None,
    ]  # fmt: skip
    assert out.entries[0]["cells_converted"] == 3 and out.entries[0]["rows_after"] == 4
    assert verify_cleaning(d, [step], out.dataset, out.entries).status == "verified"
    assert d.frame["when"].tolist()[0] == "31/12/2025"  # the loaded data is untouched


def test_the_check_catches_a_wrong_conversion():
    d = ds(DMY)
    step = TextToDate(column="when", format="%d/%m/%Y")
    out = apply_steps(d, [step])
    wrong = out.dataset.frame.copy()
    wrong.loc[wrong.index[1], "when"] = pd.Timestamp("2025-01-02")  # day and month swapped
    from dataclasses import replace

    check = verify_cleaning(d, [step], replace(out.dataset, frame=wrong), out.entries)
    assert check.status == "unverified"


def test_the_other_reading_gives_different_dates():
    d = ds(b"when,amt\n01/02/2025,1\n03/04/2025,2\n")
    dmy = apply_steps(d, [TextToDate(column="when", format="%d/%m/%Y")]).dataset.frame["when"]
    mdy = apply_steps(d, [TextToDate(column="when", format="%m/%d/%Y")]).dataset.frame["when"]
    assert [v.date().isoformat() for v in dmy] == ["2025-02-01", "2025-04-03"]
    assert [v.date().isoformat() for v in mdy] == ["2025-01-02", "2025-03-04"]


@pytest.mark.parametrize(
    ("data", "fmt"),
    [
        (b"w\n31/12/2025\n12/31/2025\n", "%d/%m/%Y"),  # one value does not fit: nothing changes
        (b"w\n2025-12-31x\n", "%d/%m/%Y"),
        (b"w\n32/01/2025\n", "%d/%m/%Y"),
        (b"w\n31 Dec 2025\n", "%d/%m/%Y"),
        (b"w\n31/12/0001\n", "%d/%m/%Y"),
    ],
)
def test_a_value_that_does_not_fit_stops_the_step_and_changes_nothing(data, fmt):
    d = ds(data)
    with pytest.raises(OperationError) as err:
        apply_steps(d, [TextToDate(column="w", format=fmt)])
    assert "nothing was changed" in str(err.value)


def test_only_text_columns_with_values_and_listed_formats():
    with pytest.raises(OperationError):
        apply_steps(ds(b"n\n1\n2\n"), [TextToDate(column="n", format="%d/%m/%Y")])
    with pytest.raises(OperationError):
        apply_steps(ds(b"w\n\n\n"), [TextToDate(column="w", format="%d/%m/%Y")])
    with pytest.raises(OperationError):
        apply_steps(ds(DMY), [TextToDate(column="nope", format="%d/%m/%Y")])
    for bad in ("%Y-%m-%dT%H:%M:%S%z", "%s", "%d/%m/%y", "dd/mm/yyyy", "%d/%m/%Y\n"):
        with pytest.raises(OperationError):
            parse_step({"op": "text_to_date", "column": "when", "format": bad})
    assert parse_step({"op": "text_to_date", "column": "when", "format": "%d/%m/%Y"})


def test_month_names_and_other_listed_formats():
    cases = {
        "%d %b %Y": ("31 Dec 2025", "2025-12-31"),
        "%b %d, %Y": ("Dec 31, 2025", "2025-12-31"),
        "%d-%b-%Y": ("31-Dec-2025", "2025-12-31"),
        "%d.%m.%Y": ("31.12.2025", "2025-12-31"),
        "%Y/%m/%d": ("2025/12/31", "2025-12-31"),
        "%B %d, %Y": ("December 31, 2025", "2025-12-31"),
    }
    for fmt, (text, iso) in cases.items():
        d = ds(f'w\n"{text}"\n'.encode())
        step = TextToDate(column="w", format=fmt)
        out = apply_steps(d, [step])
        assert out.dataset.frame["w"].iloc[0].date().isoformat() == iso, fmt
        assert verify_cleaning(d, [step], out.dataset, out.entries).status == "verified", fmt
    assert set(DATE_FORMAT_WORDS) == set(DATE_FORMATS)


def test_text_for_people_has_exact_figures():
    d = ds(DMY)
    step = TextToDate(column="when", format="%d/%m/%Y")
    entry = apply_steps(d, [step]).entries[0]
    assert describe(entry, d.source.sha256).startswith(
        "3 when values were converted from text to dates"
    )
    assert "day/month/year" in step_title(step)


def test_suggestion_is_one_format_when_only_one_fits_and_never_a_guess_when_several_do():
    s = start_session(DMY, "d.csv")
    ids = [x.id for x in s.suggestions() if x.id.startswith("text_to_date")]
    assert ids == ["text_to_date:when:%d/%m/%Y"]  # 31/12 cannot be month/day
    amb = start_session(b"when,amt\n01/02/2025,5\n03/04/2025,7\n", "a.csv")
    sug = [x for x in amb.suggestions() if x.id.startswith("text_to_date")]
    assert {x.step.format for x in sug} == {"%d/%m/%Y", "%m/%d/%Y"}
    assert all("nothing is guessed" in x.reason for x in sug)
    none = start_session(b"when,amt\nsoon,5\nlater,7\n", "n.csv")
    assert not [x for x in none.suggestions() if x.id.startswith("text_to_date")]
    iso = start_session(b"when,amt\n2025-01-02,5\n2025-03-04,7\n", "i.csv")  # already dates
    assert not [x for x in iso.suggestions() if x.id.startswith("text_to_date")]


def test_the_converted_column_works_in_a_trend_and_replays():
    s = start_session(DMY, "d.csv")
    st = s.clean({"op": "text_to_date", "column": "when", "format": "%d/%m/%Y"})
    assert st.kind == "clean" and s.workspace.cleaning_verification.status == "verified"
    trend = s.ask("Sum amt by month")
    assert trend.kind == "plan" and trend.verification == "verified"
    t = s.items[-1].result.table
    got = {p: v for p, v in zip(t["period"], t["amt:sum"], strict=True) if v is not None}
    assert got == {
        "2025-02": 7,
        "2025-03": 9,
        "2025-12": 5,
    }  # months with no rows stay empty, not zero
    from sie.workspace import replay

    assert replay(DMY, "d.csv", s.history()).sha256() == s.sha256()


def test_a_failed_conversion_leaves_the_session_as_it_was():
    s = start_session(b"when,amt\n31/12/2025,5\n12/31/2025,7\n", "b.csv")
    before = s.sha256()
    st = s.clean({"op": "text_to_date", "column": "when", "format": "%d/%m/%Y"})
    assert st.kind == "clean_rejected" and s.workspace.cleaning_steps == []
    assert s.workspace.profile.column_profiles[0].type == "text"
    assert before != s.sha256()  # the rejected request is in the history, the data is not changed
