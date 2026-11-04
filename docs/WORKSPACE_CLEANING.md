# Workspace cleaning (Slice A)

Status: implemented on branch `feature/workspace-cleaning` (stacked on PR #44). This is a new note; no
older document was edited.

## What a person can do

On the workspace page, "Clean your data" lists suggestions from the profile and a small form for manual
steps. Nothing is applied until the person presses Apply, and every step can be undone.

| Step | What it does | Notes |
| --- | --- | --- |
| Fill missing | median, mean, most frequent value, or a constant you give | median and mean need a numeric column; a constant for a numeric column must be a number; the value used is written in the log |
| Remove rows with a missing value | drops rows where chosen columns are blank | |
| Remove a column | drops one column | the last column cannot be dropped |
| Remove duplicate rows | keeps the first occurrence | reuses the engine's `clean` |
| Text to number | converts a text column to numbers with stated separators (`1,234.5` or `1.234,5`) | strict: if any value does not parse, nothing changes and the step is refused |
| Flag outliers | counts values outside Q1 - 1.5 IQR .. Q3 + 1.5 IQR, lists the first 20 row ids | changes nothing |
| Remove outliers | removes those rows | only when asked; the page asks to confirm |

## Rules

* The loaded file is never altered. Steps run on a copy (`apply_steps`), in the order approved.
* Suggestions (`suggest`) only propose. Thresholds: a column at least 50 % empty is suggested for removal,
  a numeric column with fewer blanks for median fill, a text column for most-frequent fill, exact repeated
  rows for removal, text that reads as numbers (not identifiers with leading zeros) for conversion,
  numeric columns with fence outliers for flagging.
* Every step writes a log entry with counts and the filled value; `describe` turns it into one sentence
  (for example "2 missing age values were filled with the median (29).") with every number formatted by
  `sie.answer` through `workspace/figs.py`.
* The log appears on the page, in the HTML report ("Data cleaning"), in `workspace.json`
  (`cleaning_steps`, `cleaning_steps_verification`) and in the session history as `clean`, `clean_undo`
  and `clean_rejected` steps.
* After a step, every earlier request is asked again on the cleaned data (history marks those steps
  `auto`), so no card shows numbers from before the change. Analysis ids are never reused.

## Independent check

`clean_verify.verify_cleaning` re-does the steps with plain pandas (`to_numeric`, `duplicated`,
`Series.quantile`, vectorised masks) and compares the cleaned table cell by cell and every row count in the
log. A step whose check fails is refused (`clean_rejected`) and changes nothing. If the check ever fails
for a built workspace, every analysis in it is `unverified` and is not exported.

## Replay

`replay` re-applies recorded cleaning steps (validated as typed steps, never code), skips `auto` steps and
compares every recorded step, including the logged counts and values, with what the replay produced.

## Limits

* Only plain numbers are converted; currency symbols, percent signs and dates are not (dates use the
  engine's `parse_date` through a `CleanPlan`).
* Mode ties are broken by the smallest text.
* Outlier rules need at least 8 numbers and a non-zero interquartile range.
* The server keeps one in-memory session; undo rebuilds from the loaded bytes.
