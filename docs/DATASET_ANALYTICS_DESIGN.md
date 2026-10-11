# Design: generic analytics for an uploaded file (step 2 of the generalisation plan)

Status: **proposed design, revision 2, waiting for the owner's approval before any code**
(`AGENTS.md` section 1.4). Context: ADR-039, `GENERALISATION_AUDIT.md` section 8 step 2.
Revision 2 (2026-10-11) adds the owner's seven requirements (sections 5 to 11) and the library-first
decision (section 4).

## 1. Requirement (restated)
A user gives one file (CSV, Excel or JSON table). The system profiles it, cleans it only through explicit
logged steps, runs deterministic calculations from a fixed list, and returns tables, a manifest and a
downloadable workbook. Every derived number can be traced to an operation, its parameters, its row counts
and the input file's hash. No model, no network, no cost.

## 2. Assumptions (correct me if wrong)
1. This step is a Python library plus a command line. No chat box, upload page or login.
2. One table per file. Excel: the first visible sheet unless `--sheet` is given. JSON: an array of flat objects.
3. Out of scope: PDF, web pages, databases, joins of several files, filters, user formulas, JSON Lines.
4. The Asian Games path is untouched; nothing here reads or writes the sports tables.
5. No database change. Uploaded files are kept as hash-addressed raw copies on disk (section 10); storing them in `raw_documents` is step 3.
6. Column names and values may be in any language; nothing is assumed to be English.

## 3. Impact analysis
| Area | Impact |
|------|--------|
| Database, migrations | none |
| Pipeline (`sie.pipeline`), analytics (`sie.analytics`) | none; the new package imports neither |
| Dashboard, site, reports | none |
| CLI | three new commands (section 12); existing ones unchanged |
| Dependencies | none new: pandas, openpyxl, pydantic are already core dependencies; CSV, JSON and ZIP checks use the standard library |
| Layout | one new package `src/sie/datasets/` (the only structural change; needs the owner's OK) |
| Tests | new unit tests and fixtures; the Asian Games golden tests, `sie acceptance` and the browser smoke test must stay green |
| Docs | `ARCHITECTURE.md` module list, `TESTING.md`, `CHANGELOG`, this file |

## 4. Architecture decision: library first, CLI second
`sie.datasets` must be usable later behind a research planner, an upload page or a report generator, so it
carries no command-line assumptions.
- **Pure functions over values.** The package takes bytes or a DataFrame and returns result objects. It does
  not print, read `sys.argv`, call `sys.exit`, import `typer`, or choose paths. The CLI is a thin adapter in
  `sie/cli.py` that turns arguments into the library's request objects and results into files and exit codes.
- **Requests are data, not code.** Every operation is a validated pydantic model (`OperationSpec`) that
  serialises to JSON. A future planner, form or model can only *emit these specs*; the engine never
  evaluates text as Python or SQL. `list_operations()` returns the registry with each operation's parameter
  schema so a caller can discover what is possible.
- **Any origin.** `Dataset` is built from `open_dataset(source_bytes, name, options)` (an upload) or
  `Dataset.from_frame(frame, origin)` (data a future fetcher already retrieved). Both carry a generic
  `SourceRef` (kind: upload, fetched or derived; name; sha256; optional URL and retrieval time) so
  provenance is the same whatever the source.
- **Results carry their own audit trail.** Every result object has `.to_dict()` with the numbers, the
  provenance record, the row counts and a `limitations` list. A UI or report reads those; it never has to
  rebuild them.
- **Storage is separate.** Raw copy and publishing live in `store.py` behind a small `RawStore` and
  `OutputStore` protocol (filesystem now; the database-backed raw store of step 3 plugs in without changing
  callers).
- **Enforced by a test:** `tests/unit/test_architecture_rules.py` gains a rule that `sie.datasets` imports
  nothing from `sie.cli`, `sie.pipeline`, `sie.analytics`, `typer` or `sie.db`.

Modules: `models.py` (specs, results, `SourceRef`, exceptions), `load.py`, `profile.py`, `clean.py`,
`calc.py` (operation registry), `provenance.py` (canonical serialisation, hashes), `export.py`, `store.py`.

## 5. What "deterministic" means here (requirement 1)
**Promise:** the same input bytes, the same parameters and the same library versions give the same
*logical result*: identical tables (values, column order, row order), identical result hash, identical
manifest apart from the fields listed below.
- **Canonical serialisation** (`provenance.canonical_json`): UTF-8, no BOM; JSON with sorted keys and
  separators `,` and `:`; tables as `{columns:[...], dtypes:[...], rows:[[...]]}` with rows in an explicitly
  defined order (every operation states its sort, section 6); integers as JSON integers; decimals as
  `format(x, ".15g")` strings; NaN, infinity and missing as `null`; dates as ISO 8601 strings; text
  Unicode-normalised to NFC. The result hash is SHA-256 of that text. The serialisation version is recorded in the
  manifest so a future change cannot be mistaken for a data change.
- **Byte identity is promised only for** the CSV outputs (UTF-8, `\n` line ends, fixed number format,
  fixed column order) and `manifest.json` (canonical JSON), **excluding** the fields `generated_at` and
  `tool_environment`, which are reported separately and are not part of any hash.
- **XLSX is not promised byte-identical.** The format stores timestamps and archive metadata, and the
  library may change its XML between versions. For the workbook the promise is logical: reading it back
  yields tables whose canonical hash equals the result hash (a test does this). We normalise what we can
  (fixed creator, fixed created and modified timestamps, sorted sheets, fixed ZIP member order and dates) and
  document that this is a convenience, not a guarantee.
- **Floating point:** results come from pandas and numpy on float64. Different library versions can differ in
  the last digits; formatting at 15 significant digits and recording the library versions bounds that. The
  promise names the versions: "same versions, same result".
- **What breaks determinism on purpose:** nothing in the engine uses time, randomness, locale or
  environment settings in a result. The raw-copy path and `generated_at` are outside the hash.

## 6. Calculation commands, complete (requirement 2)
Operations are an enumerated registry; each has a typed spec and a fixed behaviour. None executes
user-supplied Python, SQL or expressions. Column names are matched exactly (no regex, no case folding);
an unknown or duplicate-ambiguous name is an error that lists the available columns.

**General rules for all operations**
- *Nulls* (section 11 defines what is null) are excluded from the metric of an operation and counted:
  every result row-set carries `rows_in`, `rows_used`, `rows_excluded_null` and per-column null counts used.
  `count` counts non-null values; `rows` counts all rows of a group.
- *Numeric operations* require a numeric column. A text column is **never** coerced; it is an error that
  names the column and its detected type and suggests `clean --parse-number`.
- *Overflow and division:* integer sums that would overflow int64 raise an error (no wraparound). Division by
  zero, or a statistic on fewer values than it needs, gives `null` plus a warning, never `inf` or an error.
- *Empty results:* a valid request over zero usable rows returns an empty table with `rows_used = 0` and a
  warning. It is not an error. Invalid requests are errors (exit 2 on the CLI, `OperationError` in the library).
- *Order is always defined:* every result states its sort; the final tie-break is the group key in Unicode
  code-point order, then the original row number. No result depends on input row order unless the operation
  says so.
- *Every result* returns `table`, `provenance` (operation, parameters, input hash, rows in and used,
  versions) and `limitations`.

| Operation | Parameters | Behaviour and edge rules |
|-----------|------------|--------------------------|
| `describe` | `columns` (default all) | Per column: count, nulls, distinct, min, max; numeric also mean, median, standard deviation (sample, `ddof=1`; null if fewer than 2 values), p25, p75 (linear interpolation, method named in provenance). Text columns get length statistics. Output order = column order of the file |
| `group_aggregate` | `by` (1 to 3 columns), `metrics` (list of `column:function`, function in sum, mean, median, min, max, count, distinct), `sort` (default `by` ascending) | Null group keys form their own group labelled `(null)`, never dropped. `sum`/`mean` need numeric columns; `min`/`max`/`count`/`distinct` also work on text and dates. Empty groups cannot occur. Output sorted by group keys (code-point order) unless `sort` names a metric (descending), tie-break the keys |
| `top_n` | `by` (1 column), `metric` (`column:function`), `n` (1 to 1000, default 10), `order` (desc or asc) | Same grouping as above, then rank. Sort by metric then group key, so the result is deterministic. Rank uses the "minimum" method: equal values share a rank. If the n-th and (n+1)-th groups have equal metrics the table carries `tied_at_cutoff = true` and **includes all tied groups** rather than cutting one arbitrarily (so it can return more than n rows; the count is stated) |
| `time_trend` | `date_column`, `metric` (`column:function` or `rows`), `freq` (day, month, quarter or year), optional `start`/`end` | Date column must be a real date type (ISO or parsed by `clean --date`; ambiguous text is an error). Periods use the calendar, week starts are not offered. Output sorted by period ascending. **Periods with no rows are listed with `rows = 0` and a null metric, not filled with zero and not interpolated.** Rows with a null date are counted in `rows_excluded_null` |
| `correlation` | `columns` (2 to 20 numeric), `method` (pearson or spearman; default pearson) | For each pair, only rows where both values exist are used (pairwise); the table gives `n_used` per pair. Fewer than 3 usable pairs or a constant column gives `null` and a warning. Output is the upper triangle as rows `(a, b, coefficient, n_used)` in file column order. Correlation is reported as association only; the limitations list says so |
| `value_counts` | `column`, `top` (default 50, max 1000), `normalize` (default false) | Counts each distinct value including a `(null)` row, sorted by count descending then value code-point order; if more than `top` values exist, the remainder is one row `(other)` with its count, so totals still add up. Values are compared exactly (case and spacing matter; use `clean --trim` first) |

## 7. Cleaning, exact behaviour (requirement 3)
Cleaning is a **plan**: a list of named operations applied to a copy. The original data and the raw file are
never changed. Nothing runs unless it is in the plan.

- **Fixed order, independent of how the plan was written:** (1) `trim` whitespace in named or all text
  columns; (2) `drop_empty_columns` (named columns only, and only if entirely null); (3) `parse_number`;
  (4) `parse_date`; (5) `drop_duplicates`. Reason: dates and numbers are parsed before duplicates are
  compared, so `" 5 "` and `"5"` are one value; trim comes first so parsing sees clean text. The plan's
  JSON lists the order that was actually used.
- **Row identity.** Every row gets a `row_id` when the file is read: the 1-based position in the source
  (CSV and Excel: the data row number counting from the first row after the header; JSON: the array index
  plus one). It is kept as the table index through every operation and written to every output
  (`row_id` column in cleaned data, rejects and duplicate reports). It never changes, so any row of any output
  points back to a row of the file.
- **Parse failures (`parse_number`, `parse_date`).** `parse_number COL:DECIMAL:THOUSANDS` accepts only that
  decimal separator and thousands separator (for example `COL:,:.` for `1.234,56`), an optional sign, no
  currency symbols, no exponent unless the format is plain; `parse_date COL:FORMAT` takes an explicit
  strptime format and nothing else (no guessing, no day-first heuristics). A value that does not parse is
  **not** converted to null. Its whole row moves to the **rejects table**: `row_id`, `operation`, `column`,
  `original_value`, `reason`. Reason codes: `NOT_A_NUMBER`, `WRONG_SEPARATORS`, `OUT_OF_RANGE`,
  `NOT_A_DATE`, `DATE_OUT_OF_RANGE`, `AMBIGUOUS`. A null (empty) value is not a failure; it stays null.
- **Accepted and rejected rows.** The cleaned table holds only rows that passed every requested operation. The
  rejects table holds every removed row with its first failure (the operation order above decides which is
  "first"); the same row never appears in both. A reject threshold (default 10 percent of rows,
  `--max-reject-share`) stops the run with exit 1 and **publishes nothing**, because a large reject share
  usually means the wrong format was given.
- **Duplicates.** `drop_duplicates` removes rows whose values are identical in every column (or in named
  `subset` columns), keeps the lowest `row_id`, and writes a `duplicates` table: `row_id`, `duplicate_of`.
- **Log.** Each operation writes one entry: name, parameters, `rows_before`, `rows_after`, `rows_rejected`,
  `cells_changed`, plus reason counts. Invariant checked by a test and at run time: `rows_in = rows_cleaned +
  rows_rejected + rows_duplicate`, and `rows_before` of each step equals `rows_after` of the previous one. A
  violated invariant aborts the run.
- `trim` changes only leading and trailing whitespace (Unicode `str.strip`); it never alters inner spaces.

## 8. Parsing edge cases (requirement 4)
Every policy below fails with a specific error that names the location, or follows the stated rule; none
guesses silently.

**Common limits (defaults, configurable, recorded in the manifest):** file 25 MB, rows 1,000,000, columns 500,
total cells 5,000,000, characters per cell 32,768, parse time budget 60 s (checked every 10,000 rows). The file
is read once into memory (bounded by the limit) and that copy is hashed, stored and analysed; the path is
never re-read (section 10).

**CSV**
- UTF-8 or UTF-8 with BOM only; any other encoding needs `--encoding` and is recorded. Undecodable bytes are
  an error naming the byte offset. NUL bytes are an error.
- The delimiter is a comma unless `--delimiter` is given. It is **not sniffed** (that is guessing). If the
  header parses to a single column that contains `;` or a tab, the error says so and suggests the option.
- **Ragged rows** (field count differs from the header): by default the file fails with row number, expected
  and found counts. With `--ragged reject` those rows go to the rejects table (reason `RAGGED_ROW`) and the
  rest is used.
- **Duplicate or empty column names:** by default an error listing the names and positions. With
  `--rename-duplicates` they become `name`, `name__2`, `name__3`, and empty headers `column_N`; every rename
  is logged and written to the manifest. Names are compared after trimming whitespace.
- Quoted fields with embedded newlines and delimiters follow standard CSV rules; a malformed quote is an
  error with the row. Cells starting with `=` are read as plain text (nothing is evaluated on read).
- A header-only file loads with zero rows (profile works; calculations report `rows_used = 0`).

**Excel (`.xlsx` only)**
- `.xls`, `.xlsb` and `.xlsm` are rejected. Any workbook containing `vbaProject.bin` is rejected.
- **Formula cells are never evaluated.** The stored cached value is read; a formula with no cached value
  becomes null and is counted as `formula_without_cached_value`. The number of formula cells is reported.
  External links and embedded objects are ignored with a warning in the manifest.
- Merged cells: only the top-left cell holds a value (others null); the number of merged ranges is reported.
  Hidden sheets are not selectable by default; hidden rows and columns are read and counted as hidden.
- Dates come from the cell's date type using the workbook's epoch (1900 or 1904), recorded in the manifest.
- **Archive limits (ZIP-bomb defence).** The file size limit alone is not enough: a small archive can expand
  enormously. Before opening it as a workbook the loader opens it as a ZIP and checks: member count at most
  1,000; no nested archives; the declared uncompressed size of each member and in total (default total
  200 MB); a compression ratio of at most 100:1 for any member over 1 MB. Declared sizes can be forged, so it
  also **decompresses each member in chunks with a running byte counter and stops at the limit**, discarding
  the data (the cost is bounded by the caps). Only then is openpyxl called, read-only. The sheet's declared
  `dimension` is not trusted; rows, columns and cells are counted while reading and enforced.
  The limit that triggered is named in the error. Memory is not capped by the process; the row, column and
  cell caps bound it, and this is stated in the limitations.

**JSON**
- Strict parser: `NaN`, `Infinity` and `-Infinity` are rejected; **duplicate keys in an object are an error**
  (the standard parser would keep the last one silently); a malformed document reports line and column; trailing text after the
  document is an error; maximum nesting depth 64 (a `RecursionError` is caught and reported, not crashed).
- The document must be an array of objects. Nested objects or arrays inside a row are rejected with their
  path (flattening would be a guess). Different objects may have different keys: the column set is the union
  in order of first appearance; a missing key is null and counted per column.
- Numbers keep their lexical form for classification (`1.0` is decimal, `1` integer); integers beyond int64
  are an error, not a float.

## 9. Spreadsheet safety and numeric correctness (requirement 5)
Formula-injection protection and correct numbers are separate decisions, made **by the value's type, not its
text**.
- **Numeric values are never altered.** A number (integer or decimal type) is written as a real number: in
  XLSX as a numeric cell, in CSV as a plain numeral. `-250` stays `-250`; it sorts, sums and charts correctly.
  A CSV has no cell types, and a spreadsheet reads a bare `-250` as a number, which is what we want.
- **Text values are neutralised.** A value of text type whose first character is `=`, `+`, `-`, `@`, tab or
  carriage return is exported so a spreadsheet will not treat it as a formula: in CSV the value is prefixed
  with a single quote `'`; in XLSX it is stored as an explicit string cell (cell type "string") with the
  "quote prefix" style, never as a formula cell. Newlines inside text are preserved.
- Consequence, documented: a text cell such as `-250` (kept as text because its column is not typed numeric)
  or a phone number `+91 98765 43210` *is* neutralised and will show a leading quote in some CSV viewers. The
  fix for numbers is to type the column (`clean --parse-number`), not to weaken the protection.
- Neutralisation is a presentation step at export. It does not enter the canonical serialisation or the result
  hash; the manifest records how many cells were neutralised and in which output files.
- Tests: `-250` (integer) and `-12.5` (decimal) stay numeric and sort correctly after a round trip; `=1+1`,
  `+cmd`, `@SUM(A1)`, `-2+3` as text are neutralised; mixed columns are handled value by value; a header
  cell beginning with `=` is neutralised too.

## 10. No partial results, and raw-file preservation (requirement 6)
**Atomic publish.** All outputs are written to a temporary directory next to the target
(`<out>/.tmp-<run id>/`), flushed and `fsync`-ed, the manifest is verified against the files (sizes and
hashes), and only then is the directory renamed to its final name in one step. A failure at any point
deletes the temporary directory and publishes nothing; a crash leaves only a `.tmp-` directory, which the next
run removes when it is older than one hour (only names with that exact prefix, only inside the output
directory). The final directory name is `<input sha256, first 12>-<parameters hash, first 8>`, so rerunning the
same request finds the existing directory, verifies its manifest hash and reports "already present" instead
of writing again (idempotent). A directory that exists but fails verification is an error, never overwritten
silently.

**Raw copy.** The file is read once (bounded by the size limit); that exact byte string is hashed, written
and analysed; the original path is never read a second time (no check-then-use race). Input must be a
regular file; symbolic links, devices and pipes are refused. Stored at
`DATA_DIR/uploads/<sha256>/<safe name>` by writing a temporary file with exclusive creation and renaming it.
- **File name sanitising:** take the last path component only; Unicode NFC; replace every character outside
  letters, digits, `.`, `_`, `-` with `_`; strip leading dots; collapse to at most 100 characters; if nothing
  is left use `upload`. A name containing a path separator or `..` can therefore never leave the directory.
  The original name is kept only inside the manifest as an escaped string.
- **Hash verification:** the hash is recomputed from the stored file after writing; an existing directory for
  the same hash has its file re-hashed and must match, otherwise the run stops with an integrity error.
- **Read-only mode** (`0444` file, `0555` directory) protects against accidents and tools, nothing more. It
  does not stop the owner or root, so it is not treated as immutability; integrity comes from the hash being
  checked on every use and recorded in every manifest. Stated in the limitations.

## 11. Type inference and auditability (requirement 7)
**Null.** Only the empty string (after trimming) and JSON `null` are null by default. Words like `N/A`,
`NA`, `null`, `none`, `-` are **text** unless the caller passes `--na-values`; if they appear in a column the
profile adds a hint ("possible null markers"). This avoids silently turning data into missing.

**Types inferred per column from non-null values only** (all values must match for a single type):
| Type | Rule |
|------|------|
| `boolean` | only `true` or `false`, any case (not `yes/no`, not `0/1`) |
| `integer` | optional sign and digits, no leading zero unless the value is `0` (so `007` is **text**, flagged "looks like an identifier") ; no thousands separators; within int64 |
| `decimal` | optional sign, digits, one `.`, optional exponent. `1,5` and `1.234,5` are **text** with a hint "possible comma-decimal locale"; they are never read as numbers without `clean --parse-number` |
| `date` | ISO 8601 `YYYY-MM-DD` only; `datetime` ISO 8601 with time and optional offset. Anything else date-like (`03/04/2026`, `4 Mar 2026`) stays **text** with the hint "date-like, ambiguous format", and the profile reports which formats it would be valid under; it is never guessed |
| `text` | everything else |
| `mixed` | non-null values of more than one type; the profile gives the count of each type and the `row_id` examples; nothing is coerced. A column that is 99 percent integer and 1 percent text is still `mixed`, with the share |
| `empty` | all values null |
Excel cells and JSON values already have types (number, string, boolean, date); those types are kept and
checked against the same rules, so a column mixing numbers and text cells is `mixed`.

**Examples.** Up to 5 distinct values per column, in order of first appearance, each cut to 50 characters,
with their `row_id`. `--no-examples` omits them (use when values may be personal data). Values are never
written to logs, only to the profile and manifest the user asked for.

**Auditability of every derived number.** Every table that an operation returns has beside it, in the same
result: operation name, full parameters, input hash, `rows_in`, `rows_used`, `rows_excluded_null` (with the
columns responsible), rejects that were applied earlier by `clean` (counts), and the result hash. A single
number can be traced: workbook cell, to table, to operation record, to parameters, to input hash, to the
raw file.

**Manifest (`manifest.json`)** contains: schema version; input `SourceRef` (name as given, sanitised name,
size, sha256, origin); limits used; effective options (delimiter, encoding, sheet, na-values); profile summary;
cleaning plan, order and log; operations with provenance; completeness (null, rejected, duplicate, hidden,
merged and formula counts; `formula_without_cached_value`; neutralised cell counts); the result hashes;
`tool_environment` (this package's version, Python, pandas, numpy, openpyxl, pydantic versions) and
`generated_at` (both outside every hash); and `limitations`, a fixed list: single table only; no PDF or web;
spreadsheet formulas not evaluated; floats are float64 and can differ in the last digits between library
versions; correlation is association not causation; read-only raw copy is a guard not immutability; process
memory is not capped, only row, column and cell counts are.

## 12. Commands
A thin adapter over the library:
```
sie profile FILE [--sheet NAME] [--delimiter C] [--encoding E] [--na-values A,B] [--ragged error|reject]
                 [--rename-duplicates] [--no-examples] [--out DIR]
sie clean   FILE [profile options] [--trim] [--drop-empty-columns COL,..] [--parse-number COL:DEC:THOU ...]
                 [--parse-date COL:FORMAT ...] [--drop-duplicates [--subset COL,..]]
                 [--max-reject-share 0.10] --out DIR
sie calc    FILE [profile options] [clean options] --op describe | group_aggregate | top_n | time_trend
                 | correlation | value_counts  [operation arguments, section 6]  --out DIR
sie calc-spec FILE --spec spec.json --out DIR     # run operations given as JSON specs (what a planner would emit)
sie operations                                    # list operations and parameter schemas as JSON
```
Exit 0 success; 1 the file or data is unusable (parse failure, limits, rejects over the threshold, integrity);
2 invalid usage or an invalid operation request. `clean` and `calc` include the profile in the manifest.

## 13. Test plan
Fixtures under `tests/fixtures/datasets/` (small CSV, XLSX and JSON files, including deliberately hostile
ones created by test code, not committed binaries where avoidable). Expected values are written in the test
from the fixture rows by hand, never produced by the code under test.
- **Determinism (section 5):** run the same request twice, outputs and result hash identical; CSV and
  manifest bytes identical apart from the excluded fields; XLSX read back equals the result hash; changing one
  cell changes the input hash and the result hash; a different row order in the file does not change an
  order-independent operation; canonical serialisation test vectors (floats, NaN, dates, Unicode).
- **Operations (section 6):** one test per rule in the table: null handling and counts, text column rejected,
  overflow, division by zero, empty result, `top_n` ties with `tied_at_cutoff` and more than n rows,
  `time_trend` empty periods listed not filled, correlation pairwise counts and constant column, `value_counts`
  remainder row adds up, unknown column lists alternatives, spec validation rejects extra fields, no
  operation accepts free text that is executed (a test passes `__import__`-style and SQL-looking strings and
  proves they are only data).
- **Cleaning (section 7):** operation order is fixed whatever the plan order; row ids survive every step;
  rejects table content and reason codes; invariant `rows_in = cleaned + rejected + duplicates`; threshold
  stops without publishing; duplicates keep the lowest row id; `trim` leaves inner spaces; nothing runs unless
  requested; the input frame is not modified.
- **Parsing (section 8):** ragged rows (both policies), duplicate and empty headers (both policies), no
  delimiter sniffing, BOM, bad encoding, NUL bytes, header-only, formula cells (cached value used, none
  evaluated, missing cache counted), merged cells, `.xlsm` and macro workbook rejected; ZIP-bomb tests built
  in the test (high-ratio archive, forged declared size, too many members, nested archive, lying
  `dimension`), each stops at the right limit quickly; malformed JSON with line and column, duplicate keys,
  `NaN`, deep nesting, nested values, int beyond int64, differing key sets.
- **Spreadsheet safety (section 9):** numeric `-250` and `-12.5` round-trip as numbers and sort correctly;
  hostile text cells and headers neutralised in CSV and XLSX (cell type string, not formula); mixed columns
  value by value; the neutralised count is in the manifest and the result hash is unchanged by it.
- **Atomic publish and raw store (section 10):** a failure injected at each stage leaves no final directory
  and no `.tmp-` directory; a simulated crash leaves a stale `.tmp-` that the next run removes (and a
  non-matching directory is untouched); rerun is idempotent; a tampered existing result directory is an
  error; hostile file names (`../x`, absolute paths, `NUL`, long, non-Latin, empty) are sanitised and stay
  inside; symlink and FIFO inputs refused; the stored file's hash equals the input hash; a mismatching
  existing raw copy stops the run; the original path is read once (monkeypatch counts opens).
- **Type inference (section 11):** every row of the type table including `007`, `1,5`, `03/04/2026`,
  `N/A`, mixed columns with counts, Excel typed cells, examples limits and `--no-examples`, no values in logs
  (log capture).
- **Architecture (section 4):** the import rule test; the library runs in a process with `typer` blocked from
  import; the library can be driven entirely from `OperationSpec` JSON and gives the same result as the CLI.
- **CLI:** exit codes 0, 1, 2; outputs only inside `--out` and `DATA_DIR/uploads`; `sie operations` output
  validates against the registry.
- **Regression:** the existing Asian Games tests, `sie acceptance` figures and the browser smoke test stay
  unchanged. Static: `ruff check`, `ruff format --check`, type hints on public functions.

## 14. Acceptance criteria
1. `profile`, `clean` and `calc` (all six operations) work on committed sample CSV, XLSX and JSON files; the PR shows the actual outputs and manifest.
2. Every number in the outputs is reproduced by an independent pandas calculation in a test.
3. Two runs of the same request give the same logical result and byte-identical CSV and manifest (excluded fields aside); the raw copy's hash is unchanged and verified.
4. Missing, rejected, duplicate and ambiguous values are reported with counts and `row_id`s, never filled or guessed.
5. Hostile inputs (ZIP bombs, forged sizes, formula injection, bad names, malformed JSON) are stopped by named limits, quickly, with no partial output.
6. The library imports nothing from the CLI, pipeline, analytics, database or typer, and the same operations run from JSON specs.
7. All new and existing tests and lint pass in CI; no new dependency; no migration.
8. Docs updated; limitations listed in the manifest and in `TESTING.md`.

## 15. Work split so it can go faster (after approval)
First, **one hour of shared interface** (not parallel): `models.py` (specs, results, `SourceRef`, exceptions),
`provenance.py` (canonical serialisation and hashing with its test vectors) and the operation registry
skeleton. Then, in parallel branches or worktrees, merged one at a time into one feature branch:

| Track | Work | Needs |
|-------|------|-------|
| A | `load.py` (CSV, Excel, JSON, limits, ZIP defence) and parsing tests | interface |
| B | `profile.py` (types, nulls, hints, examples) and tests | interface; A's fixtures |
| C | `clean.py` (plan, order, row ids, rejects, log) and tests | interface |
| D | `calc.py` (six operations, ties, nulls) and tests | interface |
| E | `export.py` (CSV, XLSX, safety, manifest) and tests | interface |
| F | `store.py` (raw copy, sanitising, atomic publish, idempotence) and tests | interface |
| G | CLI adapter, `calc-spec`, `operations` and CLI tests | interface; integrates A to F last |
| H | Docs (`ARCHITECTURE.md`, `TESTING.md`, `CHANGELOG`, ADR-040 for the library-first rule) | any time |

Not parallel: the interface, the final integration in G, the full CI run. Outside the code, the owner-side
actions that block nothing here but unlock acceptance can run now: set the backup key variable and secret
(`docs/DEPLOYMENT.md` 3a), review PRs #23 to #25, open one scheduled refresh run and confirm the portal fetch
in its log, run the deployed-site smoke test.

## 16. Not decided here
Where uploaded files live in step 3 (`raw_documents` or a new table), authentication for uploads, the final
limit values, which charts to produce, filters on operations, multiple sheets or files, and the research
planner's own design. Each gets its own proposal.
