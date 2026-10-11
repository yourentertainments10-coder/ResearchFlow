# Design: generic analytics for an uploaded file (step 2 of the generalisation plan)

Status: **proposed design, waiting for the owner's approval before any code** (`AGENTS.md` section 1.4).
Context: ADR-039, `GENERALISATION_AUDIT.md` section 8 step 2.

## 1. Requirement (restated)
A user gives one file (CSV, Excel or JSON table). The system profiles it, cleans it only through
explicit logged steps, runs deterministic calculations from a fixed list, and returns tables, a
manifest and a downloadable workbook. Every number can be traced to an operation, its parameters and
the input file's hash. No model, no network, no cost.

## 2. Assumptions (correct me if wrong)
1. Command line first (`sie ...`); no chat box, upload web page or login in this step.
2. One table per file. Excel: the first sheet unless `--sheet` is given. JSON: a list of flat objects.
3. PDF, web pages, databases and several-file joins are out of scope.
4. The Asian Games path is untouched; nothing here reads or writes the sports tables.
5. No database change in this step. Uploaded files are kept as immutable raw copies on disk, named by
   content hash, with a manifest (rule 3 of `AGENTS.md`); storing them in `raw_documents` is step 3.
6. Language of column names and values is not assumed to be English.

## 3. Impact analysis
| Area | Impact |
|------|--------|
| Database, migrations | none |
| Pipeline (`sie.pipeline`) | none; the new package does not import it |
| Analytics (`sie.analytics`) | none; no sports code is reused or changed |
| Dashboard, site, reports | none |
| CLI | three new commands (section 5); existing ones unchanged |
| Dependencies | none new: pandas and openpyxl are already core dependencies; CSV and JSON use the standard library or pandas |
| Tests | new unit tests and fixtures; the browser smoke test and Asian Games golden tests must stay green |
| Docs | `ARCHITECTURE.md` module list, `TESTING.md`, `CHANGELOG`, this file |
| Layout | one new package `src/sie/datasets/` (the only structural change; needs the owner's OK) |

## 4. Design
Package `sie/datasets/`, each module one job, pure functions over data in memory:

| Module | Job |
|--------|-----|
| `load.py` | Read bytes into a `pandas.DataFrame` with limits: file size (default 25 MB), rows (default 1,000,000), columns (default 500); reject encrypted or macro workbooks; read Excel read-only without evaluating formulas; decode CSV as UTF-8 (with or without BOM), else report the encoding problem instead of guessing; JSON must be a list of flat objects. Fails with specific exceptions (`DatasetError` subclasses) |
| `profile.py` | Per column: inferred type (integer, decimal, text, date, boolean, mixed), null count and share, distinct count, min and max, examples, the share of values that fail the inferred type. Per table: rows, columns, exact duplicate rows, empty columns, constant columns. Type inference is conservative: ambiguous dates (`03/04/2026`) are reported as ambiguous, never guessed |
| `clean.py` | A fixed menu of operations (trim whitespace, drop exact duplicate rows, parse a named column as a date with a stated format, parse a named numeric column with a stated decimal and thousands separator, drop a named empty column). Each is applied only when requested; each appends a log entry (operation, column, rows affected). No imputation, no silent drops. Rows that fail a requested parse are kept in a rejects table with the reason (the quarantine idea from `AGENTS.md` rule 10) |
| `calc.py` | A whitelist of operations: `describe`, `group_aggregate` (sum, mean, median, min, max, count over named columns), `top_n`, `time_trend` (by day, month or year on a date column), `correlation` (pairs of numeric columns, with row counts used), `value_counts`. Each returns a table and a provenance record: operation, parameters, input hash, rows in and used, library versions. Nothing evaluates user text as code or SQL |
| `provenance.py` | The input hash, the ordered operation log, a deterministic result hash. Same input and same parameters give identical output and hash (rule 5, idempotent) |
| `export.py` | Write result tables as CSV and one XLSX plus a `manifest.json` (input hash, limits, profile summary, operation log, completeness: missing and rejected counts, limitations). Cells that start with `=`, `+`, `-` or `@` are written as text so a spreadsheet cannot run them |

Raw copy: before reading, the file is copied unchanged to `DATA_DIR/uploads/<sha256>/<original name>`
with a read-only mode; nothing in the pipeline writes to that path again.

Errors: parsers fail loudly with the row or cell located; a bad file never produces a partial result.
Logging: JSON lines through the existing `logging_setup`; no file contents in logs.

## 5. Commands
```
sie profile FILE [--sheet NAME] [--out DIR]
sie clean FILE --trim --drop-duplicates --date COL:FORMAT --number COL:DECIMAL:THOUSANDS ... --out DIR
sie calc FILE --op group_aggregate --by COL --metric COL:sum ... --out DIR
```
`clean` and `calc` run `profile` first and include it in the manifest. Exit 0 success, 1 the file is
unusable, 2 usage error.

## 6. Test plan
Unit tests with small fixtures (CSV, XLSX, JSON) committed under `tests/fixtures/datasets/`:
- load: BOM, wrong encoding reported, empty file, header-only file, ragged rows, over-limit file/rows/columns, XLSX with formulas (not evaluated), macro workbook rejected, JSON nested objects rejected.
- profile: every type, mixed column, ambiguous dates not guessed, duplicates, constants, empty columns, values that fail their type counted.
- clean: each operation, rejects table, log content, raw file untouched (hash compared before and after), no operation applied unless asked.
- calc: each operation against hand-computed expected values written in the test from the fixture rows (not from the code under test), empty groups, nulls excluded and counted, division and overflow edge cases.
- provenance and idempotency: run twice, outputs byte-identical; changing one cell changes the hash.
- export: CSV-injection cells neutralised, manifest completeness fields present.
- security: path traversal in the file name, huge JSON depth, zip-bomb-style XLSX rejected by size limits.
- CLI: exit codes, outputs created, nothing written outside `--out` and `DATA_DIR/uploads`.
- Regression: existing Asian Games tests, `sie acceptance` figures and the browser smoke test unchanged.
Static: `ruff check`, `ruff format --check`, type hints on public functions.

## 7. Acceptance criteria
1. The three commands work on a sample sales CSV, a sample XLSX and a sample JSON committed as fixtures; outputs and manifest shown in the PR.
2. Every number in the outputs is reproduced by an independent pandas calculation in a test.
3. Running twice gives identical files; the raw copy's hash is unchanged.
4. Missing, rejected and ambiguous values are reported, never filled.
5. All new and existing tests and lint pass in CI; no new dependency; no migration.
6. Docs updated; limitations listed (single table, no PDF, no web, command line only).

## 8. Work split so it can go faster (after approval)
The shared interface comes first (about an hour: dataclasses and exception types in
`datasets/__init__.py` and `provenance.py`, merged to the feature branch). After that these are
independent and can run in parallel, each in its own branch or worktree, merged one at a time into one
feature branch before a single PR:

| Track | Work | Needs |
|-------|------|-------|
| A | `load.py` and its tests and fixtures | interface |
| B | `profile.py` and its tests | interface and A's fixtures |
| C | `clean.py` and its tests | interface |
| D | `calc.py` and its tests | interface |
| E | `export.py`, manifest, CSV-injection tests | interface |
| F | CLI commands and CLI tests | interface; integrates A to E last |
| G | Docs (`ARCHITECTURE.md`, `TESTING.md`, `CHANGELOG`) | any time |

Not parallel: the interface, the final integration of F, and the CI run. Outside this code, the
owner-side actions that block nothing here but unlock acceptance can run now: set the backup key
variable and secret, merge or review PRs #23 to #25, open one scheduled refresh run and confirm the
portal fetch in its log, run the deployed-site smoke test.

## 9. Not decided here
Where uploaded files live in step 3 (`raw_documents` or a new table), authentication for uploads, the
size limits' final values, and which charts to produce. Each gets its own proposal.
