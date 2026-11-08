# Workspace modeling (Slice B)

Status: implemented on branch `feature/workspace-modeling` (stacked on Slice A, PR #45). New note; no older
document was edited.

## Dependency: scikit-learn (approved by the owner on 2026-11-01, "all approved")

* Declared as the optional extra `modeling = ["scikit-learn>=1.5"]` in `pyproject.toml`; CI installs it
  (`.[dev,reports,browser,modeling]`). Everything else in the workspace works without it; asking for a model
  without it gives the message `Modeling needs scikit-learn. Install it with: pip install "sie[modeling]"`.
* Why: well-tested, free (BSD-3-Clause), local, deterministic with a seed, and the standard implementation of
  the models, cross-validation, preprocessing pipelines and permutation importance used here. Writing these
  by hand would be less reliable. It brings `scipy`, `joblib` and `threadpoolctl`; `numpy` is already there.
* No network use, no model download, no paid service. Nothing about the data leaves the machine.
* Scores are not taken on trust from the library: `model_verify.py` recomputes every one with numpy
  (see "Independent check").

## What it does

Page section "Predict something or find groups", or the request `Predict <column>` / `Find groups`.

1. **Problem type from the data.** Text or boolean target: classification (at most 20 classes). Numeric
   target with at most 10 different whole values: classification. Any other numeric target: regression.
   No target: clustering (k-means, 2 to 6 groups, best silhouette). The reason is stated.
2. **Leakage and unusable columns are left out, each with a stated reason:** identifiers (almost every value
   different, or named like an id), a column whose name contains the target name, a copy of the target, a
   column that maps each of its values to a single target value (only judged when each value has at least five
   rows, so chance does not trigger it), a numeric column that follows a numeric target with |r| >= 0.999,
   constant columns, columns at least 50 % empty, mixed-type columns, dates (other than the time column, see
   "Difficult data"), and free text (more than 20 different values whose typical value is several words).
3. **Honest split.** 100 rows or more: a fixed-seed 25 % hold-out (stratified for classes). Fewer: 5-fold
   cross-validation with a warning. More than 20,000 rows: a fixed-seed sample of 20,000, stated.
   Filling missing values (median, or most frequent) and scaling are steps *inside* each model's pipeline, so
   they are learned from training rows only.
4. **Comparison.** Baseline (most frequent class, or mean), logistic or linear regression, decision tree
   (depth 5), random forest (100 trees), gradient boosting. The best is chosen on cross-validation *inside the
   training rows* (macro F1, or RMSE), and every model is then reported on the held-out rows. This differs on
   purpose from "pick the best on the test set", which would make the reported score optimistic. If nothing
   beats the baseline, the report says so.
5. **Metrics.** Classification: accuracy, macro precision, macro recall, macro F1, ROC-AUC (one-vs-rest macro
   for more than two classes). Regression: RMSE, MAE, R squared. Clustering: silhouette, group sizes, per-group
   means.
6. **Explanation.** Permutation importance (how much the score drops when a column is shuffled), on the
   held-out rows (on the fitted rows for small data, and the note says so).
7. **Limits stated** in every report: patterns, not causes; scores are estimates from one split; no invented
   features. Warnings for small data, very unequal classes (accuracy misleads) and sampling.

## Independent check

`model_verify.verify_report` recomputes every reported score from the stored predictions without scikit-learn
metric code: accuracy, per-class precision/recall/F1, ROC-AUC from average ranks, RMSE/MAE/R squared, and the
silhouette from the full distance matrix. A difference above 2e-6 makes the report `unverified`; the page and
the HTML report then show the reason and no scores.

## Reproducibility

Same data and seed give the same `ModelReport.to_dict()` and `sha256()` (metrics rounded to six decimals; the
library version is recorded but kept out of the hash). The page and the request text use seed 0; other seeds
are available to code. Models are part of the session history (`model` steps, with the report hash), are
refitted after any cleaning step (marked `auto`), and replay compares the hashes.

## Limits

* Models are for exploration, not for deciding about people; the report says what they cannot show.
* Dates are never predictors. Time-ordered data can be tested on its latest rows ("Predict X over time");
  see [WORKSPACE_LARGE_DATA.md](WORKSPACE_LARGE_DATA.md).
* Tuning is minimal on purpose (fixed, small settings).
* Class labels in a numeric target are shown as whole numbers.
