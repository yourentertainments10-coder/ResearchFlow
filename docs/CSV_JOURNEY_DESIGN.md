# Design: CSV-first analysis journey (workspace milestone, step 2)

> **Status: PROPOSED, awaiting owner approval (AGENTS.md section 1.4). No code written for this yet.**
> Direction: [WORKSPACE_PRODUCT_DIRECTION.md](WORKSPACE_PRODUCT_DIRECTION.md) sections 2, 4, 9, 12. Builds on the merged `sie.datasets` library ([DATASET_ANALYTICS_DESIGN.md](DATASET_ANALYTICS_DESIGN.md)); nothing in it is redone.

## 1. Requirement (restated)
One verified journey: **upload a CSV -> profile and quality notes -> several charts, KPI cards and tables -> a natural-language change -> validated plan -> deterministic recompute -> verification -> export (chart, data, report).** The acceptance gate is the whole journey on a fresh file, including malformed, missing-value, unsafe, unsupported and reproducibility cases. Passing unit tests or a screenshot alone does not count.

## 2. What already exists (do not rebuild)
`sie.datasets`: safe load (limits, ZIP defence), type-aware profile, cleaning plan with rejects, six operations (`describe`, `group_aggregate`, `top_n`, `time_trend`, `correlation`, `value_counts`), `OperationSpec` JSON, canonical hashes, manifest, CSV/XLSX export with formula-injection protection, atomic publish. `sie.answer`: number lock. Missing: auto-analysis choice, charts, KPI cards, findings, planner, independent verification, report export, any UI.

## 3. Assumptions
1. Local-first: the app runs on the user's machine and uploads never leave it (direction section 8). Hosted deployment is a later, separate decision.
2. CSV only in the first slice; Excel and JSON reuse the same loader afterwards.
3. One table per project. No joins, no login, no sharing.
4. Natural language is **rule-based** (no model, no network, no cost). A model may later *propose* specs through the same validator; it is never the source of numbers.
5. The Asian Games pipeline, dashboard and acceptance tests are untouched.

## 4. Impact analysis
| Area | Impact |
|---|---|
| Database / migrations | none |
| Sports pipeline, analytics, `site/` | none; regression tests must stay green |
| Dependencies | **none new** (standard library HTTP server, existing pandas/openpyxl/pydantic) |
| Layout | one new package `src/sie/workspace/` (needs your OK) and a static `src/sie/web/workspace.html` |
| CLI | new `sie workspace serve` (local, 127.0.0.1 only) and `sie workspace run FILE` (headless, used by tests) |
| Docs | `ARCHITECTURE.md`, `TESTING.md`, `ROADMAP.md`, `CHANGELOG`, new ADR |

## 5. Design
Library first again; the server is a thin adapter. `sie.workspace` may import `sie.datasets` and `sie.answer` only (enforced by an architecture test; no `sie.db`, `sie.pipeline`, `sie.analytics`, `typer`).

| Module | Responsibility |
|---|---|
| `autoplan.py` | From a `ProfileResult`, choose a **fixed, documented** set of starter analyses by column types (e.g. date + numeric -> `time_trend`; categorical + numeric -> `top_n`/`group_aggregate`; any categorical -> `value_counts`; 2+ numeric -> `correlation`). Same profile gives the same plan. Emits `OperationSpec`s plus chart specs, never numbers. |
| `charts.py` | `ChartSpec` (typed: bar, line, table; later histogram, scatter) and a deterministic **SVG renderer** from a result table. No JS chart library; the same SVG is the on-screen chart and the export. |
| `cards.py` | KPI cards (row count, columns, missing share, duplicates, plus one headline metric per plan) as `{label, value, source_operation, row_ids_basis}`. |
| `findings.py` | Rule-based findings (data-quality warnings, top/bottom group, trend direction, strong correlation labelled association only). Numbers enter as `{F:id}` placeholders through the existing number lock. |
| `planner.py` | Grammar of supported intents ("compare/sum/average X by Y", "top N Y by X", "trend of X by month", "show as bar/line/table", "filter" is **out of scope**). Matches column names exactly (or by a stated, visible case-insensitive rule). Output is a validated `OperationSpec` + `ChartSpec`, or `NeedsClarification(question, options)` or `Unsupported(reason)`. Never guesses a metric or substitutes a column. |
| `verify.py` | Recomputes every card and chart number by an **independent plain-pandas path** (not the `calc.py` code) and compares; any mismatch marks the result *unverified* and blocks export of that item. |
| `session.py` | Workspace state: source hash, cleaning plan, list of (request text, spec, result hash) steps, so history replays to the same result hashes. |
| `report.py` | HTML report (self-contained, SVG inline): source, hashes, profile, quality warnings, each chart with its spec and metric definition, transformation log, verification status, limitations. |
| `server.py` | Stdlib `http.server` on 127.0.0.1: upload (size-capped), `GET` state, `POST` request text, download exports. Same limits as `sie.datasets`. No SQL box, no code evaluation, per-session temp dir, deletion endpoint. |
| `web/workspace.html` | One accessible, responsive page: Data Sources, profile, KPI cards, charts and tables as the main area, an "ask / change analysis" box in the same page, inspect panel (inputs, definitions, transformation log, warnings), export buttons. Chart changes use subtle transitions that respect `prefers-reduced-motion`. |

Metric definitions are written once in `docs/ANALYTICS_SPEC.md`-style form for the generic operations (sum, mean, count ... exactly as in section 6 of the datasets design). This closes the "metric definitions unfinished" item for the generic path.

## 6. Safety and failure behaviour
- Upload goes through `open_dataset` only: size, rows, columns, cells, encoding, NUL, ragged-row, duplicate-header and formula-cell rules already tested.
- Server binds 127.0.0.1, rejects non-local `Host`, serves only fixed routes, never reads a path from the request, escapes all text into HTML, sets a strict CSP (no external scripts).
- Unsupported or ambiguous requests produce a visible message, not a result. A request that would need a column that does not exist lists the available columns.
- Verification failure or a cleaning reject over the threshold stops the export and says why.
- Exports are generated from the computed result and carry source hash and limitations. No export format is listed as supported until it has its own acceptance test.

## 7. Delivery slices (each its own PR, none merged without you)
1. **Core, headless:** `autoplan`, `charts`, `cards`, `findings`, `verify`, `sie workspace run`; fixture CSV; tests for determinism and independent verification.
2. **Planner (implemented in its own PR, awaiting owner approval):** grammar, clarification/unsupported paths, session replay, tests including hostile text (SQL/Python-looking input stays data).
3. **UI + server:** the page, upload, inspect panel, exports (SVG chart, CSV, XLSX, HTML report); browser smoke test in CI (existing Playwright setup).
4. **Journey acceptance:** one scripted fresh-file test of the full journey plus the negative cases; evidence recorded in `ACCEPTANCE.md`.
PNG chart export (client-side canvas) and PDF report are **later** and not claimed until tested.

## 8. Test plan (summary)
Determinism (same file+requests -> same result hashes and byte-identical SVG/CSV), every number of every card/chart reproduced by an independent pandas calculation written by hand in the test, planner positive and negative cases, hostile uploads and requests, XSS in column names/values, server routes and Host checks, reduced-motion CSS present, browser smoke on desktop/mobile/light/dark, and regression: Asian Games tests, `sie acceptance`, existing dashboard smoke test unchanged. `ruff check` and `ruff format --check` stay clean.

## 9. Decisions needed from the owner
1. **Package and layout:** OK to add `src/sie/workspace/` and `src/sie/web/workspace.html`? (Recommended: yes.)
2. **UI delivery:** local stdlib server + static page with no new dependency (recommended), or FastAPI/another framework (new dependency, needs justification)?
3. **Natural language:** rule-based grammar only for now (recommended), a model later behind the same validator?
4. **Charts:** deterministic server-side SVG first; PNG and PDF later (recommended), or PNG required in slice 3?
5. **Filters** ("only 2024", "exclude X") are excluded from slice 1-2; add as a typed operation in a later slice?

## 10. Not decided here
Hosted deployment, accounts and sharing permissions, Excel/JSON upload in the UI, linking research datasets (Phase 4) into the workspace, any language-model integration.

## 9. Slice 2 decisions (planner and session; owner may change any of them)
- **"Compare M by G" means the sum of M per G.** Fixed and shown in every interpretation note; "average", "median", "minimum", "maximum" are explicit.
- **"Trend of M by U" / "Sum M by U"** (U = day, month, quarter, year) uses the dataset's single date column; several date columns ask which; none is unsupported. A real column named like the unit wins. Week and hour are unsupported.
- **Types:** measures must be integer or decimal; group columns text, yes/no or whole numbers; text is never converted silently.
- **"Top N G by M"** ranks by the sum of M; N is required (1 to 1000) and a missing N asks.
- **"Show as bar|line|table"** re-draws the current result without recomputing (same result hash); line is allowed only for a time trend.
- **Case:** exact name first, then case-insensitive with a visible note; several case-only matches ask.
- **Replay** re-asks the recorded request texts and compares specs and result hashes; recorded specs are never executed.
- Limits: 300 characters per request; English keywords only; one group column.
