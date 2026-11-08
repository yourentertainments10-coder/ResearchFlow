# Workspace journey acceptance (design slice 4)

Evidence for `docs/CSV_JOURNEY_DESIGN.md` section 7, slice 4: one scripted fresh-file run of the whole
journey, plus the negative cases. It is a new file; the older ledger `ACCEPTANCE.md` is not touched.

The test is `tests/unit/test_workspace_journey.py`. It starts the real local server on `127.0.0.1`
(free port), talks to it over HTTP, and uses a file generated from a fixed seed (262 rows, 7 columns:
text money with thousands marks, blanks, two exact repeats, `dd/mm/yyyy` text dates, a yes/no target).

## What one run shows

| Step | Checked |
|------|---------|
| Upload and profile | 262 rows, 7 columns, SHA-256 equals the bytes sent; cards, charts and findings appear unasked; cleaning is only *suggested* (nothing applied) |
| Ask in plain words | "Compare age by region" becomes a typed plan, is answered, and is verified |
| Clean, one approved step at a time | remove duplicates, text to number, fill price, fill age, text to date; every step verified independently; 262 loaded, 260 now; the loaded original is unchanged |
| Re-ask after cleaning | all analyses still verify on the cleaned data |
| Predict | baseline shown; hold-out score on unseen rows; report verified by the numpy re-computation |
| Predict on the latest rows | opt-in time split by the cleaned date column |
| Export | CSV, XLSX and HTML report are real files; report has no script; two requests give identical bytes |
| Undo and delete | undo removes the last step only; delete leaves no session |

## Negative cases in the same file

- Unverified analysis: not shown as numbers, export answers 409, the report says "Not included" and why.
- Bad uploads (NUL byte, empty, ragged rows): 422, and nothing half-loaded; oversized: 413.
- Markup in file name, column names and values never reaches the report as markup.
- Injection-looking request ("...; DROP TABLE users") is unsupported, not executed.
- A cleaning step on a missing column is recorded as rejected and changes nothing; a model on a missing
  column or on three rows is refused with a reason.
- Wrong `Host` or `Origin` is refused (403) on upload, ask, clean, model and export; unknown paths are 400/404.
- Determinism: same bytes + same approved steps give an identical report.

## How to run

`pytest tests/unit/test_workspace_journey.py` (needs the `modeling` extra, as CI installs). CI is the
authoritative run; the first local run here used a small stand-in runner because `pytest` is not installed
in this sandbox, so CI result for the exact head is recorded in the pull request, not here.

## Not covered (said plainly)

- The browser UI for cleaning and prediction is not driven by Playwright here; the existing browser test
  covers upload, ask, clarify, export, layout and theme only.
- Real multi-GB files, other encodings and Windows save/reuse remain untested.
- Phase 4 live-source acceptance (discover, collect, report) is a different journey and stays **unverified**.
- Filters ("only 2024"), PNG chart export and a PDF report are not built (design section 9).
