"""Track D: the six operations. See design section 6."""

from __future__ import annotations

from sie.datasets.models import CleanResult, Dataset, OperationResult, parse_spec


def run_operation(
    ds: Dataset, spec: object, *, cleaning: CleanResult | None = None
) -> OperationResult:
    """Run one operation. ``spec`` is a spec model or a dict (validated with ``parse_spec``).
    ``cleaning`` only feeds the provenance counts. Raises ``OperationError`` for invalid requests."""
    raise NotImplementedError


__all__ = ["run_operation", "parse_spec"]
