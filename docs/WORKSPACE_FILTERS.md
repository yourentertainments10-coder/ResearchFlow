# Workspace filters ("only 2024", "exclude north")

A filter is a cleaning step (`FilterRows`), so it is applied on a copy of the data, logged, checked a second
way (`clean_verify`, plain pandas Series operations), undoable, replayed, and shown in the report. A request
in words is read with a small fixed grammar (`sie.workspace.filters`); typing it is the approval of that one step.

## What is understood (case-insensitive)

| Request | Step |
|---------|------|
| `only 2024` | the single date column between 2024-01-01 and 2024-12-31 (refused if there is no or more than one date column) |
| `only region is north`, `only region is north, south` | keep rows equal to / in the values |
| `only region is not north` | keep rows not equal to the value |
| `exclude north`, `exclude rows where region is north` | drop rows; the value must sit in exactly one text column |
| `only fare between 10 and 50`, `only date from 2024-01-01 to 2024-03-31` | numeric or ISO-date range |
| `only north` | as `exclude north`, but keeping the matching rows |

## What is refused, plainly

- An unknown or ambiguous column; a value that occurs in no row (it would filter nothing); a value that
  matches two spellings; a value found in two columns (name the column); more than 20 values.
- A filter that would leave no row; non-numeric or non-finite bounds; low above high; a range on text.
- Anything else (SQL, markup, formulas) is a message or a typed step, never executed.

## Limits

- No AND/OR across columns in one sentence: add one filter at a time (each is its own logged step).
- Filters narrow the analysed copy only; the loaded file and its hash never change.
- Rows from the original file are always the base: undo returns them.

Tests: `tests/unit/test_workspace_filters.py`, plus one HTTP test in `tests/unit/test_workspace_server.py`.
