"""Canonical serialisation and hashing (docs/DATASET_ANALYTICS_DESIGN.md section 5).

The promise is logical determinism: same input bytes, same parameters, same library versions give the
same canonical text and therefore the same hash. Nothing here reads a clock, a locale or randomness.
"""

from __future__ import annotations

import hashlib
import json
import math
import platform
import unicodedata
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from sie.datasets.models import SERIALISATION_VERSION

if TYPE_CHECKING:
    import pandas as pd


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def format_float(value: float) -> str | None:
    """15 significant digits, ``None`` for NaN and infinities. ``3.0`` becomes ``"3"``."""
    if math.isnan(value) or math.isinf(value):
        return None
    return format(value, ".15g")


def normalise_scalar(value: Any) -> Any:
    """One value in canonical form: None, bool, int, str. Floats become strings (``format_float``)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    # numpy and pandas scalars without importing them at module load
    kind = type(value).__module__.split(".")[0]
    if kind in ("numpy", "pandas"):
        import pandas as pd

        if value is pd.NA or value is pd.NaT:
            return None
        if hasattr(value, "item") and not isinstance(value, pd.Timestamp):
            value = value.item()
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return format_float(value)
    if isinstance(value, datetime):  # includes pandas.Timestamp
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    return unicodedata.normalize("NFC", str(value))


def normalise(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {unicodedata.normalize("NFC", str(k)): normalise(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [normalise(v) for v in obj]
    return normalise_scalar(obj)


def canonical_json(obj: Any) -> str:
    """Sorted keys, no spaces, UTF-8 text (not ASCII-escaped), NFC strings."""
    return json.dumps(
        normalise(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def canonical_table(frame: pd.DataFrame) -> dict[str, Any]:
    """``{columns, dtypes, rows}`` in the frame's own row order. The index is not part of it."""
    return {
        "columns": [str(c) for c in frame.columns],
        "dtypes": [str(t) for t in frame.dtypes],
        "rows": [
            [normalise_scalar(v) for v in row] for row in frame.itertuples(index=False, name=None)
        ],
    }


def table_sha256(frame: pd.DataFrame) -> str:
    return sha256_bytes(canonical_json(canonical_table(frame)).encode("utf-8"))


def result_sha256(obj: Any) -> str:
    return sha256_bytes(canonical_json(obj).encode("utf-8"))


def tool_environment() -> dict[str, str]:
    """Versions recorded in the manifest. Outside every hash."""
    import numpy
    import openpyxl
    import pandas
    import pydantic

    import sie

    return {
        "sie": getattr(sie, "__version__", "unknown"),
        "python": platform.python_version(),
        "pandas": pandas.__version__,
        "numpy": numpy.__version__,
        "openpyxl": openpyxl.__version__,
        "pydantic": pydantic.VERSION,
        "serialisation_version": str(SERIALISATION_VERSION),
    }
