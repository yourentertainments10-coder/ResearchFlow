"""May saved data answer this question and this operation? (ADR-046, review point on reuse)

A stored dataset is reused only when all of these hold, and the reason for each refusal is kept so the
run can say why it went looking for more:

* it belongs to this project (the store is per project label) and has not expired;
* it is fresh enough: retrieved within ``max_age_days`` (older data is "stale", not wrong, and is used only
  as a disclosed last resort when nothing newer can be found);
* its title, address or columns cover enough of the question's words;
* it has every column the requested operation names, so an operation never runs on a guess;
* its disclosed limitations (parsing flags, rejected rows) travel with it into the report; they do not
  block reuse, but a table whose flags say rows were rejected is reported as partial.

When nothing qualifies and nothing new can be fetched, the run asks for a URL or a file.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sie.evidence.discovery import assess_lead, terms

_COLUMN_KEYS = (
    "by", "column", "columns", "value", "values", "label", "metric", "metrics",
    "on", "key", "keys", "target", "x", "y", "date_column",
)  # fmt: skip


def required_columns(spec: dict) -> set[str]:
    """Column names an operation spec refers to. ``metric: "seats:sum"`` refers to ``seats``."""
    found: set[str] = set()

    def add(v) -> None:
        if isinstance(v, str):
            found.add(v.split(":", 1)[0].strip())
        elif isinstance(v, list | tuple):
            for item in v:
                add(item)

    for key in _COLUMN_KEYS:
        if key in spec:
            add(spec[key])
    found.discard("")
    found.discard("rows")  # the row-count metric names no column
    return found


@dataclass(frozen=True)
class ReuseDecision:
    id: str
    share: float
    sufficient: bool
    stale: bool
    reasons: tuple[str, ...]  # why it is not sufficient, or what is disclosed with it


def assess(
    meta: dict,
    question: str,
    spec: dict,
    *,
    now: datetime,
    max_age_days: int,
    check_columns: bool = True,
) -> ReuseDecision | None:
    """None when the dataset is unrelated to the question; otherwise a decision with reasons."""
    wanted = terms(question)
    if not wanted or meta.get("state") != "active":
        return None
    have = terms(" ".join([str(meta.get("page_url") or ""), str(meta.get("table_caption") or ""),
                           *meta["columns"], str((meta.get("extra") or {}).get("title", ""))]))  # fmt: skip
    share = round(len(wanted & have) / len(wanted), 4)
    if share < 0.5:
        return None
    reasons: list[str] = []
    stale = False
    # Place and year, judged on what the saved data states about itself (title, address, caption).
    _, unstated, mismatches = assess_lead(question, " ".join(
        [str(meta.get("page_url") or ""), str((meta.get("extra") or {}).get("title", "")),
         str(meta.get("table_caption") or "")]))  # fmt: skip
    if mismatches:
        reasons.extend(mismatches)
        return ReuseDecision(meta["key"], share, False, False, tuple(reasons))
    retrieved = meta.get("retrieved_at")
    if retrieved:
        age = now - datetime.fromisoformat(retrieved)
        if age > timedelta(days=max_age_days):
            stale = True
            reasons.append(f"retrieved {age.days} days ago (limit {max_age_days})")
    else:
        stale = True
        reasons.append("no retrieval time recorded")
    missing = sorted(required_columns(spec) - set(meta["columns"])) if check_columns else []
    if missing:
        reasons.append("lacks column(s) the operation needs: " + ", ".join(missing))
    sufficient = not stale and not missing
    flags = (meta.get("extra") or {}).get("flags") or []
    if sufficient and unstated:
        reasons.extend(unstated)
    if sufficient and flags:
        reasons.append("disclosed limitations: " + ", ".join(flags))
    return ReuseDecision(meta["key"], share, sufficient, stale, tuple(reasons))
