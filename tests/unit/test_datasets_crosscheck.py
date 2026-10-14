"""Independent check (design acceptance criteria 1 and 2): every operation's numbers are reproduced by a
separate pandas calculation on the committed sample files, and CSV, JSON and XLSX give the same results."""

from __future__ import annotations

import io
from pathlib import Path

import pandas as pd
import pytest
from openpyxl import Workbook

from sie.datasets import open_dataset, run_operation

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "datasets"


def load(name: str):
    return open_dataset((FIXTURES / name).read_bytes(), name)


def oracle() -> pd.DataFrame:
    """The sample read with pandas directly (the independent calculation)."""
    frame = pd.read_csv(FIXTURES / "sales.csv", dtype={"region": "string", "product": "string"})
    frame["region"] = frame["region"].astype(object).where(frame["region"].notna(), None)
    return frame


def _cell(value):
    """A pandas value as a spreadsheet cell: gaps stay empty, whole numbers stay integers."""
    if pd.isna(value):
        return None
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def xlsx_bytes() -> bytes:
    frame = pd.read_csv(FIXTURES / "sales.csv")
    book = Workbook()
    sheet = book.active
    sheet.append(list(frame.columns))
    for row in frame.itertuples(index=False, name=None):
        sheet.append([_cell(v) for v in row])
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def test_group_sums_means_counts_match_pandas():
    ds = load("sales.csv")
    result = run_operation(
        ds,
        {
            "op": "group_aggregate",
            "by": ["region"],
            "metrics": ["units:sum", "units:mean", "price:max", "units:count"],
        },
    )
    frame = oracle()
    got = {row[0]: row[1:] for row in result.table.values.tolist()}
    groups = frame.groupby(frame["region"].fillna("(null)"), sort=True)
    for key, part in groups:
        rows, usum, umean, pmax, ucount = got[key]
        assert rows == len(part)
        assert usum == int(part["units"].sum())
        assert umean == pytest.approx(part["units"].mean())
        assert pmax == pytest.approx(part["price"].max())
        assert ucount == int(part["units"].notna().sum())
    assert set(got) == set(groups.groups)


def test_describe_matches_pandas():
    ds = load("sales.csv")
    table = run_operation(ds, {"op": "describe", "columns": ["units", "price"]}).table.set_index(
        "column"
    )
    frame = oracle()
    for name in ("units", "price"):
        column = frame[name].dropna()
        row = table.loc[name]
        assert row["count"] == len(column) and row["nulls"] == frame[name].isna().sum()
        assert row["mean"] == pytest.approx(column.mean())
        assert row["median"] == pytest.approx(column.median())
        assert row["std"] == pytest.approx(column.std(ddof=1))
        assert row["p25"] == pytest.approx(column.quantile(0.25))
        assert row["p75"] == pytest.approx(column.quantile(0.75))
        assert row["min"] == pytest.approx(column.min()) and row["max"] == pytest.approx(
            column.max()
        )


def test_value_counts_and_top_n_match_pandas():
    ds = load("sales.csv")
    counts = dict(
        map(
            tuple,
            run_operation(ds, {"op": "value_counts", "column": "product"}).table.values.tolist(),
        )
    )
    expected = oracle()["product"].value_counts().to_dict()
    assert counts == expected
    top = run_operation(ds, {"op": "top_n", "by": "product", "metric": "units:sum", "n": 1}).table
    sums = oracle().groupby("product")["units"].sum()
    best = sums.max()
    assert set(top["product"]) == set(sums[sums == best].index) and top["units:sum"].iloc[0] == best


def test_time_trend_matches_pandas():
    ds = load("sales.csv")
    table = run_operation(
        ds,
        {"op": "time_trend", "date_column": "ordered_on", "metric": "units:sum", "freq": "month"},
    ).table
    frame = oracle().dropna(subset=["ordered_on"])
    frame["month"] = pd.to_datetime(frame["ordered_on"]).dt.strftime("%Y-%m")
    by_month = frame.groupby("month")["units"].agg(["size", "sum", "count"])
    for period, rows, total in table.values.tolist():
        assert rows == by_month.loc[period, "size"]
        if by_month.loc[period, "count"]:
            assert total == by_month.loc[period, "sum"]


@pytest.mark.parametrize("method", ["pearson", "spearman"])
def test_correlation_matches_pandas(method):
    ds = load("sales.csv")
    table = run_operation(
        ds, {"op": "correlation", "columns": ["units", "price"], "method": method}
    ).table
    frame = oracle()[["units", "price"]].dropna()
    # Spearman is Pearson on average ranks; computed without SciPy (pandas needs it only for method=)
    source = frame.rank() if method == "spearman" else frame
    expected = source["units"].corr(source["price"])
    _, _, coefficient, n_used = table.values.tolist()[0]
    assert n_used == len(frame) and coefficient == pytest.approx(expected)


def test_csv_json_and_xlsx_give_the_same_results():
    specs = [
        {"op": "group_aggregate", "by": ["region"], "metrics": ["units:sum", "price:mean"]},
        {"op": "value_counts", "column": "product"},
        {"op": "describe", "columns": ["units", "price"]},
    ]
    sources = [load("sales.csv"), load("sales.json"), open_dataset(xlsx_bytes(), "sales.xlsx")]
    for spec in specs:
        tables = [run_operation(ds, spec).table.values.tolist() for ds in sources]
        assert _same(tables[0], tables[1])
        assert _same(tables[0], tables[2])


def _same(a, b) -> bool:
    if len(a) != len(b):
        return False
    for ra, rb in zip(a, b, strict=True):
        for x, y in zip(ra, rb, strict=True):
            if isinstance(x, float) or isinstance(y, float):
                if x is None or y is None:
                    if x is not y:
                        return False
                elif x != pytest.approx(y):
                    return False
            elif x != y:
                return False
    return True
