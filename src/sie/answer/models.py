"""The answer contract: what an answer is made of, before any text is shown (ADR-041).

Five layers are kept apart (docs/PRODUCT_VISION.md section 6). A statement says which layer it belongs
to, and every number in it comes from a registered ``Figure`` through a ``{F:id}`` placeholder, never
from free text. ``sie.answer.verify`` enforces that. Pure data: no database, no network, no clock.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator

SCHEMA_VERSION = 1
ID_PATTERN = r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$"
_ID = re.compile(ID_PATTERN)


class Layer(StrEnum):
    VERIFIED_FACT = "verified_fact"  # read from the verified database
    DERIVED_ANALYSIS = (
        "derived_analysis"  # computed by a deterministic operation (or a user-file value)
    )
    INTERPRETATION = "interpretation"  # reasoning from the two layers above; adds no number
    EXTERNAL_EVIDENCE = "external_evidence"  # cited outside source, never "verified"
    LIMITATION = "limitation"  # what the answer does not cover or cannot be sure of


class SourceKind(StrEnum):
    DATABASE = "database"
    OPERATION = "operation"
    USER_FILE = "user_file"
    EVIDENCE = "evidence"


class GapKind(StrEnum):
    MISSING_DATA = "missing_data"
    PARTIAL_DATA = "partial_data"
    EXCLUDED_ROWS = "excluded_rows"
    STALE = "stale"
    CONFLICT = "conflict"
    UNVERIFIED = "unverified"
    UNSUPPORTED = "unsupported"


class RefusalCode(StrEnum):
    UNSUPPORTED_QUESTION = "unsupported_question"
    INSUFFICIENT_DATA = "insufficient_data"
    NO_SOURCE = "no_source"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def _check_id(value: str) -> str:
    if not _ID.match(value):
        raise ValueError(f"id {value!r} must match {ID_PATTERN}")
    return value


class Figure(_Frozen):
    """One number an answer may show, with where it came from. Floats are refused: use the adapters."""

    id: str
    value: Decimal
    decimals: int = Field(default=0, ge=0, le=9)  # digits shown; rounding is half up
    unit: str = Field(
        default="", max_length=16
    )  # "%" is appended directly, any other unit after a space
    label: str = Field(min_length=1, max_length=200)
    source_kind: SourceKind
    source_ref: str = Field(
        min_length=1, max_length=200
    )  # result hash, query id, file hash or evidence id
    provenance: dict[str, str] = Field(default_factory=dict)  # for example operation, input_sha256

    _id_ok = field_validator("id")(_check_id)

    @field_validator("value", mode="before")
    @classmethod
    def _exact_numbers_only(cls, v: Any) -> Any:
        if isinstance(v, (bool, float)):
            raise ValueError("a float or bool is not an exact figure; pass int, str or Decimal")
        if isinstance(v, str):
            try:
                v = Decimal(v.strip())
            except InvalidOperation as exc:
                raise ValueError(f"{v!r} is not a number") from exc
        if isinstance(v, Decimal) and not v.is_finite():
            raise ValueError("NaN and infinity are not figures")
        return v

    @field_serializer("value")
    def _ser_value(self, v: Decimal) -> str:
        return format(v.normalize() + 0, "f")


class EvidenceRef(_Frozen):
    """An outside source. Phase 1 defines the shape only; nothing here fetches anything."""

    id: str
    url: str = Field(pattern=r"^https?://\S+$", max_length=2000)
    title: str = Field(min_length=1, max_length=300)
    retrieved_at: datetime
    tier: int = Field(
        ge=1, le=5
    )  # docs/PRODUCT_VISION.md section 6: 1 official API ... 5 user-provided
    quote: str = Field(default="", max_length=300)  # a short excerpt, never a whole page

    _id_ok = field_validator("id")(_check_id)

    @field_validator("retrieved_at")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.utcoffset() is None:
            raise ValueError("retrieved_at must carry a time zone")
        return v

    @field_serializer("retrieved_at")
    def _ser_time(self, v: datetime) -> str:
        from datetime import UTC

        return v.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Gap(_Frozen):
    """Something known to be missing, partial, stale, in conflict or unverified. It must be disclosed."""

    id: str
    kind: GapKind
    message: str = Field(min_length=1, max_length=300)

    _id_ok = field_validator("id")(_check_id)


class Statement(_Frozen):
    """One sentence-sized claim. Numbers appear only as ``{F:figure_id}`` placeholders in ``text``."""

    layer: Layer
    text: str = Field(min_length=1, max_length=1000)
    evidence: tuple[str, ...] = ()
    gaps: tuple[str, ...] = ()
    # Numerals or number words that are written out on purpose (a year, "first"). Each must appear in
    # the text; they are part of the canonical output so an audit can see every one.
    declared_literals: tuple[str, ...] = ()


class Answer(_Frozen):
    schema_version: Literal[1] = SCHEMA_VERSION
    question: str = Field(min_length=1, max_length=2000)
    figures: tuple[Figure, ...] = ()
    evidence: tuple[EvidenceRef, ...] = ()
    gaps: tuple[Gap, ...] = ()
    statements: tuple[Statement, ...] = Field(min_length=1)


class Refusal(_Frozen):
    """ "Cannot answer from verified data" is a first-class result, never a guess."""

    schema_version: Literal[1] = SCHEMA_VERSION
    question: str = Field(min_length=1, max_length=2000)
    code: RefusalCode
    reason: str = Field(min_length=1, max_length=500)
    declared_literals: tuple[str, ...] = ()


def canonical_json(item: Answer | Refusal) -> str:
    """Sorted keys, no spaces, UTF-8 text: the same answer always gives the same string."""
    return json.dumps(
        item.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def answer_sha256(item: Answer | Refusal) -> str:
    return hashlib.sha256(canonical_json(item).encode("utf-8")).hexdigest()
