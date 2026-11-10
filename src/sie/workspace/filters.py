"""Plain-words requests that keep or drop rows ("only 2024", "exclude north", "only region is south").

A request is read with a small fixed grammar into a ``FilterRows`` cleaning step (data, never code), or
answered with a plain message saying what was not understood. Nothing is guessed: a column must be named
(or a value must sit in exactly one text column), text is matched to a value that really occurs in the
data, and a value that occurs nowhere is refused instead of being turned into a filter that does nothing.
The step is then applied like any other cleaning step: on a copy, logged, checked a second way, undoable.

Grammar (case-insensitive; several values are separated by commas or "or"):
  only <YYYY>                                    a year, when the data has exactly one date column
  only <column> is <value> [, <value> ...]       also: keep only rows where <column> is ...
  only <column> is not <value>
  exclude <value> [, <value> ...]                the value must sit in exactly one text column
  exclude rows where <column> is <value>         also: remove / drop / hide
  only <column> between <A> and <B>              numbers or YYYY-MM-DD dates; also "from <A> to <B>"
  only <value>                                   as "exclude <value>" but keeps the matching rows
"""

from __future__ import annotations

import re
from typing import Any

import pandas as pd

from sie.datasets.models import Dataset, ProfileResult
from sie.datasets.types import is_null
from sie.workspace.cleaning import MAX_FILTER_VALUES, FilterRows, column_kind

_KEEP = r"(?:(?:keep|show|use)\s+)?only"
_DROP = r"(?:exclude|remove|drop|hide)"
_ROWS = r"(?:rows\s+)?(?:where|with|in\s+which)\s+"
_YEAR = re.compile(rf"^\s*{_KEEP}\s+(?:in\s+|year\s+)?(?P<year>\d{{4}})\s*$", re.I)
_BETWEEN = re.compile(
    rf"^\s*{_KEEP}\s+(?:{_ROWS})?(?P<col>.+?)\s+(?:between|from)\s+(?P<a>\S+)\s+(?:and|to)\s+(?P<b>\S+)\s*$",
    re.I,
)
_KEEP_WHERE = re.compile(
    rf"^\s*{_KEEP}\s+{_ROWS}(?P<col>.+?)\s+(?:is|are|=|equals)\s+(?P<neg>not\s+)?(?P<val>.+?)\s*$",
    re.I,
)
_DROP_WHERE = re.compile(
    rf"^\s*{_DROP}\s+{_ROWS}(?P<col>.+?)\s+(?:is|are|=|equals)\s+(?P<val>.+?)\s*$", re.I
)
_COL_IS = re.compile(r"^(?P<col>.+?)\s+(?:is|are|=|equals)\s+(?P<neg>not\s+)?(?P<val>.+)$", re.I)
_KEEP_BARE = re.compile(rf"^\s*{_KEEP}\s+(?P<val>.+?)\s*$", re.I)
_DROP_BARE = re.compile(rf"^\s*{_DROP}\s+(?P<val>.+?)\s*$", re.I)
_SPLIT = re.compile(r"\s*,\s*|\s+or\s+", re.I)


def _unquote(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'`":
        return text[1:-1]
    return text


def _find_column(wanted: str, columns: list[str]) -> str | list[str] | None:
    wanted = _unquote(wanted)
    found = [c for c in columns if c == wanted] or [
        c for c in columns if c.lower() == wanted.lower()
    ]
    if len(found) == 1:
        return found[0]
    return found or None


def _values(text: str) -> list[str]:
    return [v for v in (_unquote(p) for p in _SPLIT.split(text.strip())) if v]


def _present(ds: Dataset, column: str) -> pd.Series:
    na = tuple(ds.options.na_values)
    s = ds.frame[column]
    return s[[not is_null(v, na) for v in s.tolist()]]


def _resolve(ds: Dataset, column: str, given: list[str]) -> list[str] | str:
    """The values as they are written in the data, or a message when one does not occur."""
    if len(given) > MAX_FILTER_VALUES:
        return f"Too many values (the limit is {MAX_FILTER_VALUES})."
    kind = column_kind(ds, column)
    if kind != "text":
        return given  # numbers and dates are checked by the step itself
    actual = {str(v) for v in _present(ds, column).tolist()}
    folded: dict[str, list[str]] = {}
    for a in actual:
        folded.setdefault(a.casefold(), []).append(a)
    out: list[str] = []
    for g in given:
        if g in actual:
            out.append(g)
        elif len(folded.get(g.casefold(), [])) == 1:
            out.append(folded[g.casefold()][0])
        elif g.casefold() in folded:
            return f"{g!r} matches more than one spelling in {column}; write it exactly."
        else:
            return f"No row has {column} = {g[:60]!r}, so that filter would do nothing."
    return list(dict.fromkeys(out))


def _step(ds: Dataset, column: str, given: list[str], negative: bool) -> FilterRows | str:
    got = _resolve(ds, column, given)
    if isinstance(got, str):
        return got
    if len(got) == 1:
        return FilterRows(column=column, test="not_equals" if negative else "equals", values=got)
    return FilterRows(column=column, test="not_in" if negative else "in", values=got)


def _bare(
    ds: Dataset, profile: ProfileResult, given: list[str], negative: bool
) -> FilterRows | str:
    texts = [c.name for c in profile.column_profiles if c.type == "text"]
    hits: list[str] = []
    for c in texts:
        s = _present(ds, c).astype(str).str.casefold()
        if all(s.eq(g.casefold()).any() for g in given):
            hits.append(c)
    word = ", ".join(repr(g[:40]) for g in given[:4]) + (" ..." if len(given) > 4 else "")
    if not hits:
        return (
            f"No single text column holds {word}. Name the column, for example: "
            f"only {texts[0] if texts else 'column'} is {given[0][:40]}."
        )
    if len(hits) > 1:
        return (
            f"{word} appears in more than one column ({', '.join(hits)[:200]}); say which, for example: "
            f"only {hits[0]} is {given[0][:40]}."
        )
    return _step(ds, hits[0], given, negative)


def parse_filter_request(text: str, ds: Dataset, profile: ProfileResult) -> FilterRows | str | None:
    """A ``FilterRows`` for a filter request, a plain message when it cannot be read safely, or None when
    the text is not a filter request at all."""
    columns = [c.name for c in profile.column_profiles]
    dates = [c.name for c in profile.column_profiles if c.type in ("date", "datetime")]

    def column(wanted: str) -> str | None | list[str]:
        return _find_column(wanted, columns)

    def column_test(rest: str, drop: bool) -> FilterRows | str | None:
        """ "<column> is [not] <value>" after only/exclude, when the first words name a column."""
        mm = _COL_IS.match(rest)
        if not mm or not isinstance(column(mm["col"]), str):
            return None
        given = _values(mm["val"])
        if not given:
            return "Say which value to filter on."
        return _step(ds, str(column(mm["col"])), given, drop or bool(mm["neg"]))

    def unknown_column(rest: str, bare: Any, drop: bool) -> FilterRows | str | None:
        """A bare value; when it reads "<name> is <value>" and no column has that name, say so."""
        given = _values(rest)
        if not given:
            return None
        got = bare(ds, profile, given, drop)
        mm = _COL_IS.match(rest)
        if isinstance(got, str) and mm:
            return (
                f"There is no single column named {_unquote(mm['col'])[:60]!r}. "
                f"Columns are: {', '.join(columns)[:300]}."
            )
        return got

    m = _YEAR.match(text)
    if m:
        year = m["year"]
        if len(dates) != 1:
            return (
                f"There is no single date column to read {year} from. Say which: "
                f"only <date column> between {year}-01-01 and {year}-12-31."
                if dates
                else f"There is no date column to read {year} from. Clean a text column to dates first."
            )
        return FilterRows(
            column=dates[0], test="between", low=f"{year}-01-01", high=f"{year}-12-31"
        )
    m = _BETWEEN.match(text)
    if m:
        c = column(m["col"])
        if not isinstance(c, str):
            return f"There is no single column named {_unquote(m['col'])[:60]!r}. Columns are: {', '.join(columns)[:300]}."
        return FilterRows(column=c, test="between", low=_unquote(m["a"]), high=_unquote(m["b"]))
    m = _DROP_WHERE.match(text)
    if m:
        negative, col_text, val_text = True, m["col"], m["val"]
    else:
        m = _KEEP_WHERE.match(text)
        if m:
            negative, col_text, val_text = bool(m["neg"]), m["col"], m["val"]
        else:
            m = _DROP_BARE.match(text)
            if m and not re.match(r"^(?:rows|columns?)\b", m["val"], re.I):
                direct = column_test(m["val"], True)
                if direct is not None:
                    return direct
                return unknown_column(m["val"], _bare, True)
            m = _KEEP_BARE.match(text)
            if m:
                direct = column_test(m["val"], False)
                if direct is not None:
                    return direct
                return unknown_column(m["val"], _bare, False)
            return None
    c = column(col_text)
    if not isinstance(c, str):
        return (
            f"There is no single column named {_unquote(col_text)[:60]!r}. "
            f"Columns are: {', '.join(columns)[:300]}."
        )
    given = _values(val_text)
    if not given:
        return "Say which value to filter on."
    return _step(ds, c, given, negative)
