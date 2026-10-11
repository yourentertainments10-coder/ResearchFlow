"""Track B: column and table profile. See design section 11."""

from __future__ import annotations

from sie.datasets.models import Dataset, ProfileResult


def profile(ds: Dataset, *, examples: bool = True) -> ProfileResult:
    """Types, nulls, distinct counts, min and max, duplicates, constants, hints and examples."""
    raise NotImplementedError
