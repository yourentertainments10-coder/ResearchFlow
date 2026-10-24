"""One complete request: raw copy, load, profile, clean, operations, atomic publish.

This is the orchestration the ``sie`` commands call and any other caller (a future web service or a
research planner) can call the same way. It reads no clock and no environment: the caller supplies the
bytes, the directories and ``generated_at``. Nothing is published unless every step succeeded.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sie.datasets.calc import run_operation
from sie.datasets.clean import clean
from sie.datasets.export import verify_directory, write_outputs
from sie.datasets.load import open_dataset
from sie.datasets.models import (
    SERIALISATION_VERSION,
    CleanPlan,
    DatasetError,
    Limits,
    LoadOptions,
    OperationError,
)
from sie.datasets.profile import profile as build_profile
from sie.datasets.provenance import result_sha256
from sie.datasets.store import publish, remove_stale_temp, run_name, store_raw


@dataclass(frozen=True)
class RunOutcome:
    directory: Path
    already_present: bool
    input_sha256: str
    raw_copy: Path
    manifest: dict[str, Any]

    def summary(self) -> dict[str, Any]:
        warnings = [w for op in self.manifest["operations"] for w in op["warnings"]]
        return {
            "directory": str(self.directory),
            "already_present": self.already_present,
            "input_sha256": self.input_sha256,
            "raw_copy": str(self.raw_copy),
            "files": self.manifest["outputs"],
            "warnings": warnings,
            "completeness": self.manifest["completeness"],
        }


def exit_code_for(error: DatasetError) -> int:
    """1: the file or data is unusable (parse, limits, rejects over the threshold, integrity);
    2: the request itself is invalid."""
    return 2 if isinstance(error, OperationError) else 1


def parameters_hash(
    options: LoadOptions,
    limits: Limits,
    plan: CleanPlan | None,
    specs: list[Any],
    *,
    with_profile: bool,
    examples: bool,
) -> str:
    return result_sha256(
        {
            "options": options.model_dump(mode="json"),
            "limits": limits.to_dict(),
            "plan": None if plan is None else plan.model_dump(mode="json"),
            "specs": [s.model_dump(mode="json") for s in specs],
            "profile": with_profile,
            "examples": examples,
            "serialisation_version": SERIALISATION_VERSION,
        }
    )


def run_request(
    data: bytes,
    name: str,
    *,
    uploads_root: Path,
    out_root: Path,
    generated_at: str,
    options: LoadOptions | None = None,
    limits: Limits | None = None,
    plan: CleanPlan | None = None,
    specs: list[Any] | None = None,
    with_profile: bool = True,
    examples: bool = True,
) -> RunOutcome:
    """Run the request and publish ``out_root/<input sha 12>-<parameters sha 8>/`` atomically.

    The input bytes are parsed first (so a bad file stores nothing), then kept as a hash-addressed raw
    copy under ``uploads_root``. A request that already has a verified output directory is not run
    again. Raises ``DatasetError`` subclasses; on any error nothing is published."""
    opts = options or LoadOptions()
    lim = limits or Limits()
    operations = list(specs or [])
    ds = open_dataset(data, name, opts, lim)
    stored = store_raw(data, name, uploads_root)
    final = run_name(
        ds.source.sha256,
        parameters_hash(opts, lim, plan, operations, with_profile=with_profile, examples=examples),
    )
    remove_stale_temp(out_root)
    existing = out_root / final
    if existing.exists():
        manifest = verify_directory(existing)
        return RunOutcome(existing, True, ds.source.sha256, stored.path, manifest)

    profile_result = build_profile(ds, examples=examples) if with_profile else None
    cleaning = clean(ds, plan) if plan is not None else None
    target = cleaning.dataset if cleaning is not None else ds
    results = [run_operation(target, spec, cleaning=cleaning) for spec in operations]

    def writer(temp: Path) -> None:
        write_outputs(temp, ds, lim, profile_result, cleaning, results, generated_at=generated_at)

    directory, present = publish(out_root, final, writer)
    manifest = verify_directory(directory)
    return RunOutcome(directory, present, ds.source.sha256, stored.path, manifest)
