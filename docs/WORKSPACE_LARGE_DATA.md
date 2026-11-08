# Large and difficult data (slice C)

What the workspace does when a file is too big to load whole, and when the data is awkward to model. The
rule throughout: **say what was done, never scale a sample up, never hide a limit, never let a score
come from rows the model saw.**

## Files larger than the normal limits

* A CSV above the load limits (25 MiB, 1,000,000 rows, 5,000,000 cells) is no longer simply refused in the
  workspace. It is read once as a stream, in chunks, by `sie.workspace.sampling.sample_csv`.
* Each row gets a key from `random.Random(seed).random()` in file order; the rows with the smallest keys are
  kept, up to 100,000 rows, 1,000,000 cells and half the byte limit. Only those rows are held in memory, so
  memory is bounded by the sample, not by the file. The result does not depend on the chunk size.
* The sample is written as an ordinary UTF-8 CSV and goes through the **same** loader, profile, cleaning,
  analysis and independent verification as any file.
* The whole file is hashed (SHA-256) and its rows counted while streaming. A **sample note** states the file
  rows, the sample rows, the seed and the hash. It is shown on the page and in the HTML report and is part of
  the recorded session, so replay on the same file reproduces the same sample.
* Nothing is scaled up: every figure is a figure *about the sample*, and the note says so.
* Hard stops stay: a stream above 2 GiB (the default cap) is refused with HTTP 413; a bad header, ragged
  rows, NUL bytes, over-long cells or an unreadable encoding are refused with the same reasons as the normal loader.
* Excel and JSON files above the limits are still refused (only CSV can be streamed).

## Dates written as text

`text_to_date` is a cleaning step for a text column whose values are all dates in **one listed format**
(`YYYY-MM-DD`, `DD/MM/YYYY`, `MM/DD/YYYY`, `DD-MM-YYYY`, `DD.MM.YYYY`, and the other formats in
`cleaning.DATE_FORMATS`). If a column fits several formats (day-first and month-first both fit `03/04/2024`)
each fitting format is suggested and the person chooses: **nothing is guessed**. If any value does not fit,
nothing changes. The step is independently re-checked with `pandas.to_datetime(format=..., errors="raise")`.

## Modeling difficult data

* **Test on the latest rows.** "Predict sales over time [using day]" (or the page's "Test on the latest rows
  by ..." choice) sorts the rows by the date column and trains on the earliest 75 %, tests on the latest 25 %.
  Models are compared by forward-chaining validation inside the earlier rows (each block predicted by models
  fitted only on earlier blocks). Random cross-validation is refused for this request because it would let a
  model learn from the future; under 100 rows the request is refused with that reason. Rows without a date are
  dropped and counted. The date column is used only for ordering, never as a predictor.
* **Many-valued categories.** A category column with more than 20 values is no longer thrown away: the 19 most
  common values *in the training rows* get their own column and all others share one "other" value (learned
  inside the pipeline, so the test rows cannot influence it). A warning names the column.
* **Free text.** A text column with more than 20 values whose typical value is 4+ words or 40+ characters is left
  out, with the reason. These models do not read sentences.
* **Unequal classes.** When the smallest class is under 10 % of the rows, logistic regression, the decision tree
  and the random forest use balanced class weights (rarer classes count more). Gradient boosting has no such
  option and is fitted as usual. The report states it, and models are still compared on macro F1 and against the
  baseline.

## Data notes

The findings list adds plain notes (each a verified figure): fewer than 100 rows; more columns than rows; a text
column with many different values; a date column (with the hint to ask for a model over time).

## Limits

* A sample describes the sample. A different seed gives a different sample; there is no confidence interval here.
* The sample is uniform random over rows: rare groups can be missing from it.
* The time split is one fixed split (75/25), not repeated rolling windows. Seasonal patterns that need a full
  cycle in the training rows are not checked.
* No text model, no image or geographic features.
