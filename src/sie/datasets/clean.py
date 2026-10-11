"""Track C: an explicit, logged cleaning plan. See design section 7."""

from __future__ import annotations

from sie.datasets.models import CleanPlan, CleanResult, Dataset


def clean(ds: Dataset, plan: CleanPlan) -> CleanResult:
    """Apply the plan in the fixed order. Never changes ``ds``. Raises ``RejectedTooMany`` over the
    plan's ``max_reject_share`` and ``OperationError`` for an unknown column."""
    raise NotImplementedError
