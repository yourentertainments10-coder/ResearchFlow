"""Generic, deterministic analytics for one uploaded or fetched table (ADR-039, ADR-040).

A library first: no printing, no ``sys.exit``, no command-line or database assumptions. The ``sie``
commands are a thin adapter over these functions. Requests are validated data (``OperationSpec``),
never code.
"""

from sie.datasets.calc import run_operation
from sie.datasets.clean import clean
from sie.datasets.load import from_frame, open_dataset
from sie.datasets.models import (
    CleanPlan,
    CleanResult,
    Dataset,
    DatasetError,
    IntegrityError,
    LimitExceeded,
    Limits,
    LoadOptions,
    OperationError,
    OperationResult,
    ParseError,
    ProfileResult,
    Provenance,
    RejectedTooMany,
    SourceRef,
    list_operations,
    parse_spec,
)
from sie.datasets.profile import profile

__all__ = [
    "CleanPlan",
    "CleanResult",
    "Dataset",
    "DatasetError",
    "IntegrityError",
    "LimitExceeded",
    "Limits",
    "LoadOptions",
    "OperationError",
    "OperationResult",
    "ParseError",
    "ProfileResult",
    "Provenance",
    "RejectedTooMany",
    "SourceRef",
    "clean",
    "from_frame",
    "list_operations",
    "open_dataset",
    "parse_spec",
    "profile",
    "run_operation",
]
