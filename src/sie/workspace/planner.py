"""Natural-language request -> validated analysis plan, by a small fixed grammar (no model, no network).

The planner only *proposes*: an ``OperationSpec`` (data, never code) plus how to show its table. It never
calculates anything; ``sie.datasets`` does, afterwards. Three outcomes, and nothing else:

* ``Plan``                  a typed spec, a chart kind, and a plain-language statement of how the request
                            and the column names were read (shown to the user);
* ``NeedsClarification``    the request fits the grammar but one thing cannot be decided without guessing
                            (two columns differ only by case, several date columns, no N, no grouping);
* ``Unsupported``           outside the grammar, or it names a column that does not exist or whose type
                            does not fit; the message says why and lists the real column names.

Grammar (case-insensitive keywords; whitespace collapsed; one trailing ``.``, ``!`` or ``?`` ignored)::

    compare|sum|total  [of] M by G        sum of M for each G                  (compare = sum, stated)
    average|avg|mean   [of] M by G        mean
    median | min[imum] | max[imum] [of] M by G
    trend of M by U                       sum of M per time unit (day, month, quarter, year)
    top N G by M                          the N groups with the largest sum of M
    show|display|plot|view|change [it|this] as|to [a] bar|line|table [chart]    re-draw the current result

``M`` and ``G`` are column names. A name is matched exactly; failing that, ignoring upper/lower case (the
note says so). Two columns that differ only by case never get silently chosen. Names with spaces may be
written in double quotes. ``by month`` etc. means the dataset's date column when ``month`` is not itself a
column name. Text is data: it is looked up among the column names and nothing else; SQL, Python, HTML or
formula text in a request cannot run, because nothing here evaluates text.

Out of scope on purpose: filters, joins, several group columns, arbitrary expressions, any model.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import ValidationError

from sie.datasets.models import (
    AGG_FUNCTIONS,
    GroupAggregate,
    ProfileResult,
    TimeTrend,
    TopN,
)
from sie.workspace.models import ChartKind

MAX_REQUEST_CHARS = 300
MAX_TOP_N = 1000
SHOWN_CHARS = 60
TIME_UNITS = {
    "day": "day", "daily": "day", "month": "month", "monthly": "month",
    "quarter": "quarter", "quarterly": "quarter", "year": "year", "yearly": "year",
    "annual": "year", "annually": "year",
}  # fmt: skip
UNSUPPORTED_UNITS = {
    "week": "week",
    "weekly": "week",
    "hour": "hour",
    "hourly": "hour",
    "minute": "minute",
}
NUMERIC_TYPES = ("integer", "decimal")
GROUP_TYPES = ("text", "boolean", "integer")
DATE_TYPES = ("date", "datetime")
# the fixed meaning of each verb (a documented rule, shown in every interpretation note)
VERBS: dict[str, str] = {
    "compare": "sum", "sum": "sum", "total": "sum", "average": "mean", "avg": "mean", "mean": "mean",
    "median": "median", "min": "min", "minimum": "min", "max": "max", "maximum": "max",
}  # fmt: skip
assert set(VERBS.values()) <= set(AGG_FUNCTIONS)
FUNCTION_WORDS = {
    "sum": "sum",
    "mean": "mean",
    "median": "median",
    "min": "minimum",
    "max": "maximum",
}

_VERB = "|".join(sorted(VERBS, key=len, reverse=True))
_AGG = re.compile(rf"^(?P<verb>{_VERB})(?:\s+of)?\s+(?P<rest>.+)$", re.I)
_TREND = re.compile(r"^trend\s+of\s+(?P<rest>.+)$", re.I)
_TOP = re.compile(r"^top\s+(?P<rest>.+)$", re.I)
_CHART = re.compile(
    r"^(?:show|display|plot|view|change|switch)(?:\s+(?:it|this|that|the\s+chart|the\s+result))?"
    r"\s+(?:as|to)\s+(?:an?\s+)?(?P<kind>bar|line|table)(?:\s+chart)?$",
    re.I,
)
_BY = re.compile(r"\s+by\s+", re.I)
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f  ]")


def shown(text: object, limit: int = SHOWN_CHARS) -> str:
    """User or data text made safe to put in a message: printable, one line, no markup characters, cut
    short. Invisible and direction-changing characters become spaces; ``<`` and ``>`` become ``‹`` and ``›``."""
    plain = "".join(
        " " if unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp", "Cn", "Co", "Cs") else c
        for c in str(text)
    )
    plain = plain.replace("<", "‹").replace(">", "›")
    plain = " ".join(plain.split())
    return plain if len(plain) <= limit else plain[: limit - 1] + "…"


@dataclass(frozen=True)
class Plan:
    spec: Any  # a sie.datasets operation spec model
    chart: ChartKind
    x: str | None  # result column for labels / the x axis
    y: str | None  # result column for values
    title: str
    interpretation: str  # how the request was read, in words, for display
    notes: tuple[
        str, ...
    ] = ()  # visible rules applied (case-insensitive match, fixed verb meaning)
    kind: Literal["plan"] = "plan"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "plan",
            "spec": self.spec.model_dump(mode="json"),
            "chart": self.chart,
            "x": self.x,
            "y": self.y,
            "title": self.title,
            "interpretation": self.interpretation,
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class ChartChange:
    """Draw the current result differently. The numbers do not change and nothing is recomputed."""

    chart: ChartKind
    interpretation: str
    kind: Literal["chart_change"] = "chart_change"

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "chart_change", "chart": self.chart, "interpretation": self.interpretation}


@dataclass(frozen=True)
class NeedsClarification:
    question: str
    options: tuple[str, ...] = ()  # complete requests that the grammar reads unambiguously
    kind: Literal["clarification"] = "clarification"

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "clarification", "question": self.question, "options": list(self.options)}


@dataclass(frozen=True)
class Unsupported:
    reason: str
    available_columns: tuple[str, ...] = field(default_factory=tuple)
    kind: Literal["unsupported"] = "unsupported"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "unsupported",
            "reason": self.reason,
            "available_columns": list(self.available_columns),
        }


Outcome = Plan | ChartChange | NeedsClarification | Unsupported


def _quote(name: str) -> str:
    return f'"{name}"'


def _strip_quotes(phrase: str) -> str:
    phrase = phrase.strip()
    if len(phrase) >= 2 and phrase[0] == phrase[-1] == '"':
        return phrase[1:-1].strip()
    return phrase


@dataclass(frozen=True)
class _Found:
    name: str | None = None
    rule: str = ""  # "" exact, otherwise a visible note
    candidates: tuple[str, ...] = ()


def _resolve(phrase: str, names: list[str]) -> _Found:
    """A column by exact name, else by case-insensitive name. Several case-only matches: no choice made."""
    wanted = " ".join(_strip_quotes(phrase).split())
    if not wanted:
        return _Found()
    for name in names:
        if name == wanted or " ".join(name.split()) == wanted:
            return _Found(name)
    folded = [n for n in names if " ".join(n.split()).casefold() == wanted.casefold()]
    if len(folded) == 1:
        return _Found(
            folded[0],
            f"matched {_quote(shown(folded[0]))} ignoring upper/lower case (you wrote {_quote(shown(wanted))})",
        )
    return _Found(None, "", tuple(folded))


def _unknown(phrase: str, names: list[str], what: str) -> Unsupported:
    return Unsupported(
        f"There is no column named {_quote(shown(_strip_quotes(phrase)))} (needed as {what}). "
        "Column names are matched exactly or ignoring upper/lower case; nothing is guessed.",
        tuple(names),
    )


def _clarify_columns(prefix: str, candidates: tuple[str, ...], build: Any) -> NeedsClarification:
    return NeedsClarification(
        f"{prefix} matches more than one column that differs only by upper/lower case. Which one?",
        tuple(build(c) for c in candidates),
    )


def _types(profile: ProfileResult) -> dict[str, str]:
    return {c.name: c.type for c in profile.column_profiles}


def _metric_problem(name: str, types: dict[str, str], function: str) -> Unsupported | None:
    if types[name] in NUMERIC_TYPES:
        return None
    return Unsupported(
        f"Column {_quote(shown(name))} is read as {types[name]}, and {FUNCTION_WORDS[function]} needs "
        "numbers. Text is never turned into numbers silently; a cleaning step (for example "
        "parse-number) would be needed first.",
        tuple(types),
    )


def _group_problem(name: str, types: dict[str, str]) -> Unsupported | None:
    if types[name] in GROUP_TYPES:
        return None
    return Unsupported(
        f"Column {_quote(shown(name))} is read as {types[name]}; grouping works on text, yes/no or "
        "whole-number columns here. For dates say a unit instead, for example 'by month'.",
        tuple(types),
    )


def _splits(rest: str) -> list[tuple[str, str]]:
    """Every way to cut ``rest`` at a ' by ', so column names that contain 'by' still resolve."""
    out = []
    for m in _BY.finditer(rest):
        before, after = rest[: m.start()].strip(), rest[m.end() :].strip()
        if before and after:
            out.append((before, after))
    return out


def _title_group(fn: str, metric: str, group: str) -> str:
    return f"{FUNCTION_WORDS[fn].capitalize()} of {shown(metric)} by {shown(group)}"


def _date_column(types: dict[str, str], unit: str, build: Any) -> tuple[str | None, Outcome | None]:
    dates = [n for n, t in types.items() if t in DATE_TYPES]
    if not dates:
        return None, Unsupported(
            f"'by {unit}' needs a date column and the data has none. Columns: "
            + ", ".join(_quote(shown(n)) for n in types),
            tuple(types),
        )
    if len(dates) > 1:
        return None, NeedsClarification(
            f"'by {unit}' could use several date columns. Which one?",
            tuple(build(d) for d in dates),
        )
    return dates[0], None


def _aggregate(verb: str, rest: str, profile: ProfileResult, *, trend: bool = False) -> Outcome:
    function = "sum" if trend else VERBS[verb.lower()]
    types = _types(profile)
    names = list(types)
    splits = _splits(rest)
    if not splits:
        examples = tuple(
            f"{verb.capitalize() if not trend else 'Trend of'} {_quote(shown(m))} by {_quote(shown(g))}"
            for m in [n for n in names if types[n] in NUMERIC_TYPES][:1]
            for g in [n for n in names if types[n] in GROUP_TYPES][:2]
        )
        return NeedsClarification(
            "Which column should it be grouped by? Say 'by <column>' (or 'by month' for a date column).",
            examples,
        )
    first_failure: Outcome | None = None
    for metric_phrase, group_phrase in splits:
        outcome = _aggregate_one(verb, function, trend, metric_phrase, group_phrase, types, names)
        if not isinstance(outcome, Unsupported):
            return outcome
        first_failure = first_failure or outcome
    assert first_failure is not None
    return first_failure


def _aggregate_one(
    verb: str, function: str, trend: bool, metric_phrase: str, group_phrase: str,
    types: dict[str, str], names: list[str],
) -> Outcome:  # fmt: skip
    verb_text = "Trend of" if trend else verb.capitalize()

    def request(metric: str, group: str) -> str:
        return f"{verb_text} {_quote(shown(metric))} by {_quote(shown(group))}"

    metric = _resolve(metric_phrase, names)
    if metric.name is None and len(metric.candidates) > 1:
        return _clarify_columns(
            f"{_quote(shown(_strip_quotes(metric_phrase)))}", metric.candidates,
            lambda c: request(c, _strip_quotes(group_phrase)),
        )  # fmt: skip
    if metric.name is None:
        return _unknown(metric_phrase, names, "the measure")
    bad = _metric_problem(metric.name, types, function)
    if bad:
        return bad
    notes = [f"{verb.lower() if not trend else 'trend'} is read as {FUNCTION_WORDS[function]}"]
    if metric.rule:
        notes.append(metric.rule)

    group_text = " ".join(_strip_quotes(group_phrase).split())
    group = _resolve(group_phrase, names)
    if group.name is None and len(group.candidates) > 1:
        return _clarify_columns(
            f"{_quote(shown(group_text))}",
            group.candidates,
            lambda c: request(metric.name or "", c),
        )
    if group.name is not None:  # a real column always wins over a time-unit word
        bad = _group_problem(group.name, types)
        if bad:
            return bad
        if trend:
            return Unsupported(
                "A trend needs a time unit (day, month, quarter, year) after 'by', not a column name.",
                tuple(names),
            )
        if group.rule:
            notes.append(group.rule)
        spec = _build(GroupAggregate, by=[group.name], metrics=[f"{metric.name}:{function}"], sort=f"{metric.name}:{function}")  # fmt: skip
        if isinstance(spec, Unsupported):
            return spec
        y = f"{metric.name}:{function}"
        return Plan(
            spec, "bar", group.name, y, _title_group(function, metric.name, group.name),
            f"{FUNCTION_WORDS[function].capitalize()} of {_quote(shown(metric.name))} for each "
            f"{_quote(shown(group.name))}, largest first.", tuple(notes),
        )  # fmt: skip
    unit_word = group_text.casefold()
    if unit_word in UNSUPPORTED_UNITS:
        return Unsupported(
            f"'by {unit_word}' is not supported; the time units are day, month, quarter and year."
        )
    if unit_word in TIME_UNITS:
        unit = TIME_UNITS[unit_word]
        date_col, problem = _date_column(
            types, unit, lambda d: f"{verb_text} {_quote(shown(metric.name or ''))} by {unit} using {_quote(shown(d))}"
        )  # fmt: skip
        if problem is not None:
            return problem
        assert date_col is not None
        notes.append(f"'by {unit}' uses the date column {_quote(shown(date_col))}")
        spec = _build(
            TimeTrend, date_column=date_col, metric=f"{metric.name}:{function}", freq=unit
        )
        if isinstance(spec, Unsupported):
            return spec
        y = f"{metric.name}:{function}"
        return Plan(
            spec, "line", "period", y,
            f"{FUNCTION_WORDS[function].capitalize()} of {shown(metric.name)} by {unit}",
            f"{FUNCTION_WORDS[function].capitalize()} of {_quote(shown(metric.name))} for each {unit} of "
            f"{_quote(shown(date_col))}, in time order.", tuple(notes),
        )  # fmt: skip
    if trend:
        return Unsupported(
            f"{_quote(shown(group_text))} is not a time unit; use day, month, quarter or year.",
            tuple(names),
        )
    many = " and " in group_text.casefold() or "," in group_text
    if many:
        return Unsupported(
            "Grouping by more than one column is not supported yet; name a single column.",
            tuple(names),
        )
    return _unknown(group_phrase, names, "the grouping")


def _build(model: Any, **fields: Any) -> Any:
    try:
        return model(**fields)
    except ValidationError as exc:
        return Unsupported(
            "The request does not form a valid operation: " + shown(exc.errors()[0]["msg"])
        )


def _top(rest: str, profile: ProfileResult) -> Outcome:
    types = _types(profile)
    names = list(types)
    parts = rest.split(None, 1)
    if len(parts) < 2:
        return Unsupported(
            "Say what to rank, for example 'Top 5 products by revenue'.", tuple(names)
        )
    first, tail = parts
    if not first.isdigit():
        if first.isascii() and first.lstrip("-").isdigit():
            return Unsupported(f"N must be a whole number from 1 to {MAX_TOP_N}.")
        return NeedsClarification(
            "How many should the top list show?",
            tuple(f"Top {n} {rest}" for n in (3, 5, 10)),
        )
    if not first.isascii():
        return Unsupported(f"N must be a whole number from 1 to {MAX_TOP_N}.")
    n = int(first)
    if not 1 <= n <= MAX_TOP_N:
        return Unsupported(
            f"N must be a whole number from 1 to {MAX_TOP_N}; you wrote {shown(first)}."
        )
    splits = _splits(tail)
    if not splits:
        return Unsupported(
            "Say what to rank by, for example 'Top 5 products by revenue'.", tuple(names)
        )
    first_failure: Outcome | None = None
    for group_phrase, metric_phrase in splits:
        outcome = _top_one(n, group_phrase, metric_phrase, types, names)
        if not isinstance(outcome, Unsupported):
            return outcome
        first_failure = first_failure or outcome
    assert first_failure is not None
    return first_failure


def _top_one(
    n: int, group_phrase: str, metric_phrase: str, types: dict[str, str], names: list[str]
) -> Outcome:
    def request(group: str, metric: str) -> str:
        return f"Top {n} {_quote(shown(group))} by {_quote(shown(metric))}"

    group, metric = _resolve(group_phrase, names), _resolve(metric_phrase, names)
    if group.name is None and len(group.candidates) > 1:
        return _clarify_columns(
            _quote(shown(_strip_quotes(group_phrase))), group.candidates,
            lambda c: request(c, _strip_quotes(metric_phrase)),
        )  # fmt: skip
    if metric.name is None and len(metric.candidates) > 1:
        return _clarify_columns(
            _quote(shown(_strip_quotes(metric_phrase))), metric.candidates,
            lambda c: request(_strip_quotes(group_phrase), c),
        )  # fmt: skip
    if group.name is None:
        return _unknown(group_phrase, names, "what to rank")
    if metric.name is None:
        return _unknown(metric_phrase, names, "the measure")
    bad = _group_problem(group.name, types) or _metric_problem(metric.name, types, "sum")
    if bad:
        return bad
    notes = ["'top' ranks by the sum of the measure for each group"]
    notes += [r for r in (group.rule, metric.rule) if r]
    label = f"{metric.name}:sum"
    spec = _build(TopN, by=group.name, metric=label, n=n)
    if isinstance(spec, Unsupported):
        return spec
    return Plan(
        spec, "bar", group.name, label, f"Top {n} {shown(group.name)} by sum of {shown(metric.name)}",
        f"The {n} {_quote(shown(group.name))} groups with the largest sum of {_quote(shown(metric.name))}, "
        "largest first.", tuple(notes),
    )  # fmt: skip


def normalise(text: object) -> str | Unsupported:
    """The request text ready for the grammar, or an ``Unsupported`` for text that is not a request."""
    if not isinstance(text, str):
        return Unsupported("A request must be text.")
    if _CONTROL.search(text):
        return Unsupported("The request contains control characters; write it as plain text.")
    clean = " ".join(text.split()).rstrip(".!? ").strip()
    if not clean:
        return Unsupported("The request is empty. Try 'Sum revenue by region'.")
    if len(clean) > MAX_REQUEST_CHARS:
        return Unsupported(f"The request is longer than {MAX_REQUEST_CHARS} characters.")
    return clean


HELP = (
    "Supported requests: 'Compare <measure> by <column>', 'Sum|Average|Median|Minimum|Maximum <measure> by "
    "<column>', 'Trend of <measure> by day|month|quarter|year', 'Top <N> <column> by <measure>', and "
    "'Show as bar|line|table chart'. Filters, joins and several group columns are not supported yet."
)


def plan_request(text: object, profile: ProfileResult, *, current: Plan | None = None) -> Outcome:
    """Turn one request into a ``Plan`` / ``ChartChange`` / ``NeedsClarification`` / ``Unsupported``.

    ``current`` is the analysis a chart change would apply to (the last plan of the session); ``None``
    means there is nothing to re-draw yet. Pure function of its arguments: no clock, no I/O."""
    clean = normalise(text)
    if isinstance(clean, Unsupported):
        return clean
    if profile.rows == 0 or profile.columns == 0:
        return Unsupported("The data has no rows or no columns, so there is nothing to analyse.")
    chart = _CHART.match(clean)
    if chart:
        return _chart_change(chart["kind"].lower(), current)
    if m := _TREND.match(clean):
        return _aggregate("trend", m["rest"], profile, trend=True)
    if m := _TOP.match(clean):
        return _top(m["rest"], profile)
    if m := _AGG.match(clean):
        return _aggregate(m["verb"], m["rest"], profile)
    return Unsupported(f"I could not read {_quote(shown(clean))} as a request. " + HELP)


def _chart_change(kind: str, current: Plan | None) -> Outcome:
    if current is None:
        return Unsupported(
            "There is no analysis to re-draw yet. Ask for one first, for example 'Sum revenue by region'."
        )
    want: ChartKind = kind  # type: ignore[assignment]
    if want == "line" and current.spec.op != "time_trend":
        return Unsupported(
            "A line chart is for a trend over time; this analysis is grouped by category. "
            "Use 'Show as bar chart' or 'Show as table', or ask for 'Trend of <measure> by month'."
        )
    return ChartChange(
        want, f"Same numbers, drawn as a {want}{' chart' if want != 'table' else ''}."
    )
