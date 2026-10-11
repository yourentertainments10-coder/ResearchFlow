"""Types shared by every part of ``sie.datasets``. Pure data: no I/O, no CLI, no database.

Requests are validated data (pydantic models that serialise to JSON), never code: a caller can only
describe *which* operation to run with *which* parameters (docs/DATASET_ANALYTICS_DESIGN.md section 4).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

if TYPE_CHECKING:  # pandas is only needed for annotations here
    import pandas as pd

SERIALISATION_VERSION = (
    1  # bump when provenance.canonical_json changes (recorded in every manifest)
)

NULL_LABEL = "(null)"
OTHER_LABEL = "(other)"


# --- errors ----------------------------------------------------------------------------------------


class DatasetError(ValueError):
    """Base class: the data or the request cannot be used. Never raised for a bug."""


class ParseError(DatasetError):
    """The file is malformed. ``location`` says where (row, column, line) when known."""

    def __init__(self, message: str, location: str | None = None) -> None:
        super().__init__(f"{message} ({location})" if location else message)
        self.location = location


class LimitExceeded(DatasetError):
    """A resource limit was hit. ``limit`` names it (for example ``max_rows``)."""

    def __init__(self, limit: str, message: str) -> None:
        super().__init__(f"limit {limit}: {message}")
        self.limit = limit


class OperationError(DatasetError):
    """An operation or cleaning request is invalid (unknown column, wrong type, bad parameter)."""


class RejectedTooMany(DatasetError):
    """Cleaning rejected more rows than ``max_reject_share`` allows; nothing is published."""


class IntegrityError(DatasetError):
    """A stored file or an invariant does not match its recorded hash or count."""


# --- limits and options ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Limits:
    """Resource limits. Defaults from the design; every value used is recorded in the manifest."""

    max_bytes: int = 25 * 1024 * 1024
    max_rows: int = 1_000_000
    max_columns: int = 500
    max_cells: int = 5_000_000
    max_cell_chars: int = 32_768
    parse_seconds: float = 60.0
    zip_max_members: int = 1_000
    zip_max_uncompressed: int = 200 * 1024 * 1024
    zip_max_ratio: int = 100  # compressed:uncompressed, applied to members over zip_ratio_min_bytes
    zip_ratio_min_bytes: int = 1024 * 1024
    max_json_depth: int = 64

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


class LoadOptions(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    sheet: str | None = None  # Excel only; default is the first visible sheet
    delimiter: str = Field(default=",", min_length=1, max_length=1)
    encoding: str = "utf-8"  # utf-8 and utf-8-sig need no flag; anything else must be stated
    na_values: tuple[str, ...] = ()  # extra null markers; default null is only the empty string
    ragged: Literal["error", "reject"] = "error"
    rename_duplicates: bool = False


class SourceRef(BaseModel):
    """Where a dataset came from. Same shape for an upload, a fetched table or a derived one."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["upload", "fetched", "derived"] = "upload"
    name: str = ""  # as given by the caller, kept only as data
    sha256: str = ""
    size_bytes: int = 0
    url: str | None = None
    retrieved_at: str | None = (
        None  # ISO 8601, supplied by the caller (never read from a clock here)
    )


@dataclass
class Dataset:
    """A loaded table.

    ``frame`` has one row per source row, the index is the 1-based ``row_id`` (position in the source
    after the header; JSON: array index plus one), and every column has dtype ``object`` holding the
    values as the source typed them (CSV: str, empty becomes ``None``; Excel and JSON: their own types).
    Nothing is coerced at load. ``rejects`` holds rows the loader could not use (ragged rows when asked).
    ``notes`` are loader findings with counts (renamed headers, formula cells, merged ranges, ...).
    """

    frame: pd.DataFrame
    source: SourceRef
    options: LoadOptions = field(default_factory=LoadOptions)
    rejects: pd.DataFrame | None = None
    notes: list[dict[str, Any]] = field(default_factory=list)

    @property
    def rows(self) -> int:
        return len(self.frame)

    @property
    def columns(self) -> list[str]:
        return [str(c) for c in self.frame.columns]


# --- profile ---------------------------------------------------------------------------------------

ColumnTypeName = Literal[
    "boolean", "integer", "decimal", "date", "datetime", "text", "mixed", "empty"
]


class ColumnProfile(BaseModel):
    name: str
    type: ColumnTypeName
    non_null: int
    nulls: int
    null_share: float
    distinct: int
    type_counts: dict[str, int] = Field(default_factory=dict)  # per value type, for mixed columns
    min: str | None = None
    max: str | None = None
    examples: list[dict[str, str]] | None = (
        None  # [{"row_id": "7", "value": "..."}]; None = omitted
    )
    hints: list[str] = Field(default_factory=list)


class ProfileResult(BaseModel):
    rows: int
    columns: int
    duplicate_rows: int
    empty_columns: list[str]
    constant_columns: list[str]
    column_profiles: list[ColumnProfile]
    notes: list[dict[str, Any]] = Field(default_factory=list)  # loader notes
    limitations: list[str] = Field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


# --- cleaning --------------------------------------------------------------------------------------

_COL = Annotated[str, Field(min_length=1)]


class NumberFormat(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    column: _COL
    decimal: Literal[".", ","] = "."
    thousands: Literal["", ",", ".", " ", "'"] = ""

    @field_validator("thousands")
    @classmethod
    def _differs(cls, v: str, info) -> str:
        if v and v == info.data.get("decimal"):
            raise ValueError("thousands separator must differ from the decimal separator")
        return v


class DateFormat(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    column: _COL
    format: _COL  # a strptime format, for example %d/%m/%Y


class DedupSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    subset: list[_COL] | None = None  # None = every column


class CleanPlan(BaseModel):
    """What to do. Order of application is fixed (design section 7), not the order written here."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    trim: list[_COL] | Literal["all"] | None = None
    drop_empty_columns: list[_COL] = Field(default_factory=list)
    parse_number: list[NumberFormat] = Field(default_factory=list)
    parse_date: list[DateFormat] = Field(default_factory=list)
    drop_duplicates: DedupSpec | None = None
    max_reject_share: float = Field(default=0.10, ge=0.0, le=1.0)


@dataclass
class CleanResult:
    dataset: Dataset  # cleaned: typed columns where parse_* ran, index still row_id
    rejects: pd.DataFrame  # row_id, operation, column, original_value, reason
    duplicates: pd.DataFrame  # row_id, duplicate_of
    log: list[dict[str, Any]]  # one entry per operation run, in the order run
    plan: dict[str, Any]  # the plan with the order actually used
    rows_in: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan": self.plan,
            "log": self.log,
            "rows_in": self.rows_in,
            "rows_cleaned": self.dataset.rows,
            "rows_rejected": len(self.rejects),
            "rows_duplicate": len(self.duplicates),
        }


# --- operations -----------------------------------------------------------------------------------

AGG_FUNCTIONS = ("sum", "mean", "median", "min", "max", "count", "distinct")
_METRIC_RE = re.compile(r"^(?P<column>.+):(?P<function>" + "|".join(AGG_FUNCTIONS) + r")$")


def parse_metric(text: str) -> tuple[str, str]:
    """``"revenue:sum"`` -> ``("revenue", "sum")``. The split is on the *last* colon."""
    match = _METRIC_RE.match(text)
    if not match:
        raise ValueError(f"metric must look like column:function with function in {AGG_FUNCTIONS}")
    return match["column"], match["function"]


class _Spec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Describe(_Spec):
    op: Literal["describe"] = "describe"
    columns: list[_COL] | None = None


class GroupAggregate(_Spec):
    op: Literal["group_aggregate"] = "group_aggregate"
    by: list[_COL] = Field(min_length=1, max_length=3)
    metrics: list[str] = Field(min_length=1, max_length=20)
    sort: str | None = None  # a metric label (column:function) to sort by, descending

    @field_validator("metrics")
    @classmethod
    def _valid(cls, v: list[str]) -> list[str]:
        for item in v:
            parse_metric(item)
        return v


class TopN(_Spec):
    op: Literal["top_n"] = "top_n"
    by: _COL
    metric: str
    n: int = Field(default=10, ge=1, le=1000)
    order: Literal["desc", "asc"] = "desc"

    @field_validator("metric")
    @classmethod
    def _valid(cls, v: str) -> str:
        parse_metric(v)
        return v


class TimeTrend(_Spec):
    op: Literal["time_trend"] = "time_trend"
    date_column: _COL
    metric: str = "rows"  # "rows" or column:function
    freq: Literal["day", "month", "quarter", "year"] = "month"
    start: str | None = None  # ISO date, inclusive
    end: str | None = None  # ISO date, inclusive

    @field_validator("metric")
    @classmethod
    def _valid(cls, v: str) -> str:
        if v != "rows":
            parse_metric(v)
        return v


class Correlation(_Spec):
    op: Literal["correlation"] = "correlation"
    columns: list[_COL] = Field(min_length=2, max_length=20)
    method: Literal["pearson", "spearman"] = "pearson"


class ValueCounts(_Spec):
    op: Literal["value_counts"] = "value_counts"
    column: _COL
    top: int = Field(default=50, ge=1, le=1000)
    normalize: bool = False


OperationSpec = Annotated[
    Describe | GroupAggregate | TopN | TimeTrend | Correlation | ValueCounts,
    Field(discriminator="op"),
]
OPERATION_MODELS: dict[str, type[_Spec]] = {
    "describe": Describe,
    "group_aggregate": GroupAggregate,
    "top_n": TopN,
    "time_trend": TimeTrend,
    "correlation": Correlation,
    "value_counts": ValueCounts,
}


def list_operations() -> dict[str, Any]:
    """Every operation with its JSON parameter schema, so a caller can discover what is possible."""
    return {name: model.model_json_schema() for name, model in OPERATION_MODELS.items()}


def parse_spec(data: dict[str, Any]) -> _Spec:
    """Validate a JSON-able dict into the right spec. Unknown operations and extra fields are errors."""
    name = data.get("op")
    model = OPERATION_MODELS.get(name) if isinstance(name, str) else None
    if model is None:
        raise OperationError(f"unknown operation {name!r}; use one of {sorted(OPERATION_MODELS)}")
    try:
        return model.model_validate(data)
    except ValueError as exc:  # pydantic.ValidationError is a ValueError
        raise OperationError(f"invalid parameters for {name}: {exc}") from exc


@dataclass
class Provenance:
    operation: str
    parameters: dict[str, Any]
    input_sha256: str
    rows_in: int
    rows_used: int
    rows_excluded_null: dict[str, int] = field(
        default_factory=dict
    )  # column -> rows dropped for null
    cleaning: dict[str, int] = field(
        default_factory=dict
    )  # rows rejected/duplicate by clean, if any
    result_sha256: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "parameters": self.parameters,
            "input_sha256": self.input_sha256,
            "rows_in": self.rows_in,
            "rows_used": self.rows_used,
            "rows_excluded_null": self.rows_excluded_null,
            "cleaning": self.cleaning,
            "result_sha256": self.result_sha256,
        }


@dataclass
class OperationResult:
    table: pd.DataFrame
    provenance: Provenance
    warnings: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)  # for example {"tied_at_cutoff": True}

    def to_dict(self) -> dict[str, Any]:
        from sie.datasets.provenance import canonical_table

        return {
            "table": canonical_table(self.table),
            "provenance": self.provenance.to_dict(),
            "warnings": self.warnings,
            "limitations": self.limitations,
            "extra": self.extra,
        }


STANDARD_LIMITATIONS = (
    "Single table only; no PDF, web or database input.",
    "Spreadsheet formulas are never evaluated; cached values are read.",
    "Numbers are float64 or int64; floats can differ in the last digits between library versions.",
    "Correlation shows association, not causation.",
    "The raw copy is made read-only as a guard against accidents; integrity rests on the recorded hash.",
    "Process memory is not capped; only row, column and cell counts are.",
)
