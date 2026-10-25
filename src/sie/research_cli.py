"""``sie research discover | collect | ask | datasets``: a thin adapter over ``sie.research``.

Free sources only. ``discover`` and ``ask`` call registered free catalogues through the safe fetcher
(robots.txt, 2 s per host, cache first); ``collect`` fetches one URL you name. Nothing here writes to the
database. Collected tables are kept per ``--project`` under the configurable dataset directory (only where the source's
licence or a recorded policy permits, and only until they expire); the short-lived fetch cache and reports go
under ``DATA_DIR/research`` and ``--out``.
Exit codes: 0 done, 1 nothing usable or a refusal (the message says what to supply), 2 invalid request.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import typer

from sie.config import get_settings

research_app = typer.Typer(
    help="Free-source research: discover, collect, analyse.", no_args_is_help=True
)


def _fail(message: str, code: int = 2) -> typer.Exit:
    typer.echo(message, err=True)
    return typer.Exit(code=code)


def _paths(settings):
    root = settings.data_dir / "evidence"
    return root / "catalogues.csv", root / "source_authority.csv"


@research_app.command("discover")
def research_discover(
    question: str = typer.Argument(..., help="The research question."),
    limit: int = typer.Option(10, "--limit", min=1, max=50),
) -> None:
    """Search the registered free catalogues and rank the leads (cache first, no paid service)."""
    from sie.evidence import EvidenceError, FetchRefused
    from sie.research.pipeline import build_fetchers, discover_free

    settings = get_settings()
    catalogues, authority = _paths(settings)
    try:
        discovery_fetcher, _ = build_fetchers(settings)
        result, outcomes = discover_free(
            question, discovery_fetcher, catalogues, authority, limit=limit
        )
    except (EvidenceError, FetchRefused) as exc:
        raise _fail(str(exc), 1) from exc
    payload = result.to_dict() | {"providers": [o.to_dict() for o in outcomes]}
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))
    if not result.usable:
        typer.echo(
            "No usable lead. Give a URL to `sie research collect` or a file to `sie profile`.",
            err=True,
        )
        raise typer.Exit(code=1)


MANUAL_NOTE = (
    "supplied by URL, not found by discovery: whether it matches the question's place, year and "
    "scope was not assessed"
)
PROJECT = typer.Option("default", "--project", help="Research project label (own saved data).")


@research_app.command("collect")
def research_collect(
    url: str = typer.Argument(..., help="https URL of a CSV, JSON or HTML page with a table."),
    title: str = typer.Option("", "--title"),
    licence: str = typer.Option("", "--licence", help="Licence id the source states, e.g. uk-ogl."),
    project: str = PROJECT,
) -> None:
    """Fetch one page politely (cache first), turn its tables into datasets and store them."""
    from sie.evidence import EvidenceError, FetchRefused
    from sie.research.pipeline import build_fetchers, collect_url, make_store

    settings = get_settings()
    try:
        _, data_fetcher = build_fetchers(settings)
        stored, tables, stale = collect_url(
            url, data_fetcher, make_store(settings, project), title=title,
            licence=licence or None,
            limitations=(MANUAL_NOTE,),
        )  # fmt: skip
    except FetchRefused as exc:
        raise _fail(f"refused ({exc.code}): {exc}", 1) from exc
    except EvidenceError as exc:
        raise _fail(str(exc), 1) from exc
    for s, t in zip(stored, tables, strict=True):
        if not s.stored:
            typer.echo(f"not saved: {t.label} rows {t.rows} ({s.basis}); usable this run only")
            continue
        state = "already stored" if s.already_present else "stored"
        typer.echo(
            f"{state}: {s.id} {t.label} rows {t.rows} until {s.expires_at:%Y-%m-%d} "
            f"licence {t.licence or 'not stated'} flags {list(t.flags) or 'none'}"
        )
    if stale:
        typer.echo("note: an older cached copy was used because the live fetch failed", err=True)


@research_app.command("datasets")
def research_datasets(project: str = PROJECT) -> None:
    """List this project's saved datasets (reused for follow-up questions)."""
    from sie.research.pipeline import make_store

    for m in make_store(get_settings(), project).list_datasets():
        typer.echo(
            f"{m['key']}\t{m['rows']} rows\t{m['retrieved_at']}\texpires {m['expires_at'][:10]}"
            f"\t{m['licence'] or 'no licence'}\t{m['page_url']}"
        )


@research_app.command("prune")
def research_prune(
    execute: bool = typer.Option(False, "--execute", help="Delete (default is a dry run)."),
) -> None:
    """Remove expired saved datasets and expired fetch-cache entries."""
    from sie.research.cache import FetchCache
    from sie.research.pipeline import make_store

    settings = get_settings()
    result = make_store(settings).prune(execute=execute)
    typer.echo(
        f"{'removed' if execute else 'would remove'} {len(result.expired)} expired dataset(s)"
    )
    cache = FetchCache(
        settings.research_dir / "cache",
        retention=timedelta(days=settings.research_cache_retention_days),
    )
    if execute:
        typer.echo(f"removed {cache.prune()} expired cache file(s)")


@research_app.command("ask")
def research_ask(
    question: str = typer.Argument(..., help="The research question."),
    op: str = typer.Option(
        '{"op": "describe"}',
        "--op",
        help='Operation as JSON, e.g. {"op":"top_n","by":"party","metric":"seats:sum","n":5}.',
    ),
    clean_plan: str = typer.Option(
        "", "--clean", help="Cleaning plan as JSON (sie.datasets CleanPlan)."
    ),
    label: str = typer.Option(
        "", "--label-column", help="Result column naming each row (default: first)."
    ),
    value: str = typer.Option(
        "", "--value-column", help="Numeric result column to chart (default: second)."
    ),
    out: Path = typer.Option(Path("out/research"), "--out"),
    refresh: bool = typer.Option(
        False, "--refresh", help="Look for new data even if stored data covers it."
    ),
    project: str = PROJECT,
    conflict_key: str = typer.Option(
        "", "--conflict-key", help="Comma-separated columns identifying a row across sources."
    ),
    conflict_values: str = typer.Option(
        "", "--conflict-values", help="Comma-separated columns to compare across sources."
    ),
) -> None:
    """Reuse or collect data for a question, run one operation, write report.md, chart.svg, answer.json."""
    from sie.evidence import EvidenceError, FetchRefused
    from sie.research.collect import check_agreement
    from sie.research.pipeline import build_fetchers, make_store, run_research
    from sie.research.report import (
        ANALYSIS_ERRORS,
        AnalysisItem,
        analyze,
        build_report,
        default_columns,
        plan_from_dict,
    )

    settings = get_settings()
    try:
        spec = json.loads(op)
        plan = plan_from_dict(json.loads(clean_plan)) if clean_plan else None
    except ValueError as exc:
        raise _fail(f"invalid JSON: {exc}") from exc
    catalogues, authority = _paths(settings)
    try:
        discovery_fetcher, data_fetcher = build_fetchers(settings)
        run = run_research(
            question,
            store=make_store(settings, project),
            spec=spec,
            check_columns=plan is None,
            max_age_days=settings.evidence_web_max_age_days,
            discovery_fetcher=discovery_fetcher,
            data_fetcher=data_fetcher,
            catalogues_path=catalogues,
            authority_path=authority,
            refresh=refresh,
        )
    except (EvidenceError, FetchRefused) as exc:
        raise _fail(str(exc), 1) from exc
    for o in run.outcomes:
        if o.status != "ok":
            typer.echo(f"provider {o.provider}: {o.status}: {o.detail}", err=True)
    for p in run.problems:
        typer.echo(f"skipped: {p}", err=True)
    for d in run.reuse_rejected:
        typer.echo(f"saved dataset {d.id} not used: {'; '.join(d.reasons)}", err=True)
    if run.needs_manual_input:
        raise _fail(
            "No usable data was found from the free sources, and no saved data can answer this. "
            "Nothing paid was tried. Give a URL to `sie research collect` or a file to "
            "`sie profile`, then ask again.",
            1,
        )
    table = run.tables[0]
    try:
        split = lambda v: [c.strip() for c in v.split(",") if c.strip()] or None  # noqa: E731
        conflicts, agreement = check_agreement(
            run.tables, split(conflict_key), split(conflict_values)
        )
        result = analyze(table, spec, plan)
        default_label, default_value = default_columns(result.table)
        lab, val = label or default_label, value or default_value
        report = build_report(
            question, run.tables, [AnalysisItem(str(spec.get("op", "operation")), result, lab, val)],
            now=datetime.now(UTC), tiers=run.tiers, stale_urls=run.stale_urls,
            stale_datasets=run.stale_datasets, not_saved=run.not_saved,
            conflicts=conflicts, agreement_note=agreement, lead_limitations=run.lead_limitations,
            provider_notes=[f"{o.provider}: {o.status}" for o in run.outcomes if o.status != "ok"],
        )  # fmt: skip
    except (*ANALYSIS_ERRORS, EvidenceError, ValueError, KeyError) as exc:
        raise _fail(str(exc), 2) from exc
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.md").write_text(report.markdown, encoding="utf-8")
    (out / "answer.json").write_text(report.answer_json(), encoding="utf-8")
    if report.svg:
        (out / "chart.svg").write_text(report.svg, encoding="utf-8")
    typer.echo(
        f"report written to {out} ({'reused stored data' if run.reused else 'collected new data'})"
    )


def register(app: typer.Typer) -> None:
    app.add_typer(research_app, name="research")
