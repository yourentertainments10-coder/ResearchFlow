"""Predictive modeling for the workspace (scikit-learn): compare simple models honestly, on the cleaned copy.

What it does, in order
  1. pick the problem from the data: a target column gives classification (few classes) or regression
     (a measured number); no target gives clustering (groups);
  2. keep only columns that can be used without cheating: identifiers, copies of the target, columns that
     are a function of the target, constant, almost empty, free-text, mixed-type and date columns are
     left out, each with its reason;
  3. split once (a fixed seed): a 25 % hold-out for 100 rows or more, 5-fold cross-validation below that;
     missing values are filled and numbers scaled *inside* the model pipeline, so every fill value is
     learned from the training rows only;
  4. fit a baseline (always the most frequent class, or the mean) and logistic/linear regression, a
     decision tree, a random forest and gradient boosting; choose the best on the training rows
     (cross-validation inside the training part), then report every model on rows it never saw;
  5. explain the best model with permutation importance and state its limits;
  6. recompute every reported score from the stored predictions with plain numpy
     (``sie.workspace.model_verify``); a score that does not agree makes the report ``unverified``.

Deterministic: same data and seed give the same report and the same ``sha256()``. No network, no clock.
Needs scikit-learn (``pip install "sie[modeling]"``); everything else in the workspace works without it.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from sie.datasets.models import ColumnProfile, Dataset, OperationError, ProfileResult
from sie.datasets.provenance import result_sha256
from sie.datasets.types import is_null
from sie.workspace import figs
from sie.workspace.models import Verification

MIN_ROWS = 20  # fewer usable rows than this: refuse
SMALL_DATA = 100  # below this: cross-validation instead of a hold-out, and a warning
MAX_ROWS = 20_000  # more rows than this: a fixed-seed sample is used (and said)
MAX_CLASSES = 20
MAX_CATEGORIES = 20  # more distinct values than this: the most common are kept, the rest grouped
FREE_TEXT_WORDS = 4  # a text column whose typical value has this many words is free text
FREE_TEXT_CHARS = 40
TIME_SPLITS = 4  # forward-chaining validation blocks inside the earlier rows
ALMOST_EMPTY = 0.5
TEST_SIZE = 0.25
FOLDS = 5
CLUSTER_MAX_ROWS = 3000
CLUSTER_KS = (2, 3, 4, 5, 6)
ROUND = 6
IMBALANCE = 0.10  # smallest class share under this: warn that accuracy misleads
_ID_NAME = re.compile(r"(^|[^a-z])(id|uuid|index|idx|key|row|rownum)($|[^a-z])", re.I)

LIMITATIONS = (
    "A model finds patterns in this file; it does not show that one thing causes another.",
    "Scores are estimates from one split of this file. They can be lower on new data, especially when "
    "the data is small or its collection changes.",
    "Columns are used as they are after the cleaning you approved; no feature is invented.",
)


@dataclass(frozen=True)
class ModelSpec:
    target: str | None = None  # None means "find groups" (clustering)
    seed: int = 0
    time_column: str | None = None  # set: train on the earlier rows, test on the latest ones

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"target": self.target, "seed": self.seed}
        if self.time_column is not None:  # only when set, so earlier hashes stay the same
            out["time_column"] = self.time_column
        return out


@dataclass
class ModelReport:
    spec: ModelSpec
    problem: str  # classification | regression | clustering
    why: list[str]
    rows_used: int
    rows_dropped: dict[str, int]
    features: list[dict[str, str]]
    excluded: list[dict[str, str]]
    evaluation: dict[str, Any]
    models: list[dict[str, Any]]
    best: str | None
    importance: list[dict[str, Any]]
    warnings: list[str]
    clusters: dict[str, Any] | None = None
    limitations: list[str] = field(default_factory=lambda: list(LIMITATIONS))
    verification: Verification = field(default_factory=lambda: Verification("not_run"))
    library: dict[str, str] = field(default_factory=dict)  # not part of the hash

    def to_dict(self) -> dict[str, Any]:
        return {
            "spec": self.spec.to_dict(),
            "problem": self.problem,
            "why": self.why,
            "rows_used": self.rows_used,
            "rows_dropped": self.rows_dropped,
            "features": self.features,
            "excluded": self.excluded,
            "evaluation": self.evaluation,
            "models": self.models,
            "best": self.best,
            "importance": self.importance,
            "warnings": self.warnings,
            "clusters": self.clusters,
            "limitations": self.limitations,
            "verification": self.verification.to_dict(),
        }

    def sha256(self) -> str:
        return result_sha256(self.to_dict())


# -- text ------------------------------------------------------------------------------------------


def _n(value: Any, source: str, decimals: int = 0) -> str:
    return figs.show(
        figs.file_figure("m", value, label="model", file_sha256=source, decimals=decimals)
    )


def shown_number(value: float | int | None, source: str, decimals: int = 3) -> str:
    """A score as an exact figure (half-up rounding), or a dash."""
    if value is None:
        return "-"
    return _n(value, source, 0 if isinstance(value, int) else decimals)


# -- data preparation -------------------------------------------------------------------------------


def _null(value: Any, ds: Dataset) -> bool:
    return is_null(value, tuple(ds.options.na_values))


def _series(ds: Dataset, c: ColumnProfile) -> pd.Series:
    """One column as numbers (NaN for null) or as text (NaN for null)."""
    raw = [None if _null(v, ds) else v for v in ds.frame[c.name].tolist()]
    if c.type in ("integer", "decimal"):
        return pd.Series(
            pd.to_numeric(pd.Series(raw, index=ds.frame.index, dtype=object).map(_strip)),
            index=ds.frame.index,
            dtype="float64",
        )
    return pd.Series(
        [np.nan if v is None else _label(v) for v in raw], index=ds.frame.index, dtype=object
    )


def _strip(v: Any) -> Any:
    return v.strip() if isinstance(v, str) else v


def _label(v: Any) -> str:
    if isinstance(v, float) and v == int(v) and abs(v) < 1e15:
        return str(int(v))
    if hasattr(v, "item"):
        return _label(v.item())
    return str(v).strip()


def _looks_like_id_name(name: str) -> bool:
    return bool(_ID_NAME.search(name)) or name.lower().endswith("id")


def _exclusions(
    ds: Dataset,
    profile: ProfileResult,
    target: str | None,
    columns: dict[str, pd.Series],
    y: pd.Series | None,
    problem: str,
    rows: int,
    source: str,
    time_column: str | None = None,
) -> tuple[list[ColumnProfile], list[dict[str, str]]]:
    keep: list[ColumnProfile] = []
    out: list[dict[str, str]] = []

    def drop(name: str, reason: str) -> None:
        out.append({"column": name, "reason": reason})

    for c in profile.column_profiles:
        if c.name == target:
            continue
        s = columns.get(c.name)
        if c.type == "empty" or s is None:
            drop(c.name, "the column is empty")
        elif c.name == time_column:
            drop(c.name, "used only to put the rows in time order, not as a predictor")
        elif c.type in ("date", "datetime"):
            drop(
                c.name,
                'dates are not used as predictors (ask for the model "over time" to test on '
                "the latest rows)",
            )
        elif c.type == "mixed":
            drop(c.name, "the column mixes numbers and text; clean it first")
        elif s.isna().mean() >= ALMOST_EMPTY:
            drop(
                c.name,
                f"{_n(round(float(s.isna().mean()) * 100), source)}% of its values are missing",
            )
        elif s.nunique(dropna=True) <= 1:
            drop(c.name, "every filled value is the same, so it cannot help")
        elif target and target.lower() in c.name.lower() and len(target) >= 3:
            drop(
                c.name,
                f"its name contains the target name ({target}); it is probably derived from it",
            )
        elif c.type in ("text", "boolean") and s.nunique() >= 0.9 * rows and rows >= 10:
            drop(c.name, "almost every value is different; it looks like an identifier")
        elif c.type == "integer" and _looks_like_id_name(c.name) and s.nunique() >= 0.9 * rows:
            drop(
                c.name,
                "it looks like an identifier (named like an id, almost all values different)",
            )
        elif (
            c.type in ("integer", "text")
            and _looks_like_id_name(c.name)
            and s.nunique() > MAX_CATEGORIES
        ):
            drop(
                c.name,
                "it is named like an id and repeats across rows (the same person, user or item "
                "in several rows); it would let a model recognise rows instead of learning "
                "patterns. Scores can still be optimistic because the same id can be in both the "
                "training and the test rows: this tool does not yet split by id",
            )
        elif c.type == "text" and s.nunique() > MAX_CATEGORIES and _free_text(s):
            drop(
                c.name,
                f"free text ({_n(int(s.nunique()), source)} different values, typically several "
                "words); these models do not read sentences, so it is left out",
            )
        elif y is not None and _same_as_target(s, y):
            drop(c.name, "it is a copy of the target")
        elif y is not None and _determines_target(s, y, c.type, problem):
            drop(
                c.name,
                "every value of it maps to a single target value (or it follows the target "
                "almost exactly); it is probably derived from the target",
            )
        else:
            keep.append(c)
    return keep, out


def _free_text(s: pd.Series) -> bool:
    texts = s.dropna().astype(str)
    if texts.empty:
        return False
    words = float(texts.map(lambda v: len(v.split())).median())
    chars = float(texts.map(len).median())
    return words >= FREE_TEXT_WORDS or chars >= FREE_TEXT_CHARS


def _same_as_target(s: pd.Series, y: pd.Series) -> bool:
    a = s.map(lambda v: v if isinstance(v, str) else (None if pd.isna(v) else _label(v)))
    b = y.map(lambda v: None if pd.isna(v) else _label(v))
    both = a.notna() & b.notna()
    return bool(both.sum() >= 10 and (a[both] == b[both]).all())


def _determines_target(s: pd.Series, y: pd.Series, ctype: str, problem: str) -> bool:
    both = s.notna() & y.notna()
    if both.sum() < 20:
        return False
    s, y = s[both], y[both]
    if ctype in ("integer", "decimal") and problem == "regression":
        yn = pd.to_numeric(y, errors="coerce")
        if s.std() == 0 or yn.std() == 0:
            return False
        return bool(abs(float(np.corrcoef(s, yn)[0, 1])) >= 0.999)
    distinct = s.nunique()
    if distinct > MAX_CATEGORIES or len(s) / max(distinct, 1) < 5:
        return False  # many values with few rows each: a match would be chance
    if problem == "classification":
        pure = y.groupby(s.astype(str)).nunique().max() == 1
        return bool(pure and y.nunique() > 1 and distinct > 1)
    return False


# -- numeric helpers ----------------------------------------------------------------------------------


def _r(x: float | None) -> float | None:
    if x is None or (isinstance(x, float) and not math.isfinite(x)):
        return None
    return round(float(x), ROUND)


def classification_scores(
    y_true: np.ndarray, proba: np.ndarray, classes: list[str]
) -> dict[str, float | None]:
    from sklearn.metrics import (
        accuracy_score,
        precision_recall_fscore_support,
        roc_auc_score,
    )

    y_pred = np.array(classes, dtype=object)[np.argmax(proba, axis=1)]
    p, r, f, _ = precision_recall_fscore_support(
        y_true, y_pred, labels=classes, average="macro", zero_division=0
    )
    auc: float | None
    present = set(y_true.tolist())
    if len(classes) < 2 or not all(c in present for c in classes):
        auc = None
    elif len(classes) == 2:
        auc = float(roc_auc_score(y_true == classes[1], proba[:, 1]))
    else:
        auc = float(
            roc_auc_score(y_true, proba, multi_class="ovr", average="macro", labels=classes)
        )
    return {
        "accuracy": _r(accuracy_score(y_true, y_pred)),
        "precision": _r(p),
        "recall": _r(r),
        "f1": _r(f),
        "roc_auc": _r(auc),
    }


def regression_scores(y_true: np.ndarray, y_pred: np.ndarray) -> dict[str, float | None]:
    from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score

    r2: float | None = float(r2_score(y_true, y_pred)) if np.var(y_true) > 0 else None
    return {
        "rmse": _r(math.sqrt(mean_squared_error(y_true, y_pred))),
        "mae": _r(mean_absolute_error(y_true, y_pred)),
        "r2": _r(r2),
    }


@dataclass
class Evidence:
    """The stored predictions the independent check recomputes scores from (never serialized)."""

    kind: str
    scored: list[tuple[str, str, Any, Any, list[str] | None, dict[str, Any]]] = field(
        default_factory=list
    )  # (model, which, y_true, prediction, classes, reported)
    clusters: list[tuple[int, np.ndarray, np.ndarray, float | None]] = field(default_factory=list)


# -- the run ---------------------------------------------------------------------------------------


def _import_sklearn() -> None:
    try:
        import sklearn  # noqa: F401
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise OperationError(
            'Modeling needs scikit-learn. Install it with: pip install "sie[modeling]"'
        ) from exc


def _estimators(problem: str, seed: int, balanced: bool = False) -> list[tuple[str, Any]]:
    from sklearn.dummy import DummyClassifier, DummyRegressor
    from sklearn.ensemble import (
        GradientBoostingClassifier,
        GradientBoostingRegressor,
        RandomForestClassifier,
        RandomForestRegressor,
    )
    from sklearn.linear_model import LinearRegression, LogisticRegression
    from sklearn.tree import DecisionTreeClassifier, DecisionTreeRegressor

    if problem == "classification":
        weight = "balanced" if balanced else None
        return [
            ("Baseline (most frequent class)", DummyClassifier(strategy="most_frequent")),
            (
                "Logistic regression",
                LogisticRegression(max_iter=1000, random_state=seed, class_weight=weight),
            ),
            (
                "Decision tree",
                DecisionTreeClassifier(max_depth=5, random_state=seed, class_weight=weight),
            ),
            (
                "Random forest",
                RandomForestClassifier(
                    n_estimators=100, random_state=seed, n_jobs=1, class_weight=weight
                ),
            ),
            ("Gradient boosting", GradientBoostingClassifier(random_state=seed)),
        ]
    return [
        ("Baseline (mean)", DummyRegressor(strategy="mean")),
        ("Linear regression", LinearRegression()),
        ("Decision tree", DecisionTreeRegressor(max_depth=5, random_state=seed)),
        ("Random forest", RandomForestRegressor(n_estimators=100, random_state=seed, n_jobs=1)),
        ("Gradient boosting", GradientBoostingRegressor(random_state=seed)),
    ]


def _pipeline(estimator: Any, numeric: list[str], categorical: list[str]) -> Any:
    from sklearn.compose import ColumnTransformer
    from sklearn.impute import SimpleImputer
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import OneHotEncoder, StandardScaler

    parts = []
    if numeric:
        parts.append(
            (
                "num",
                Pipeline([("fill", SimpleImputer(strategy="median")), ("scale", StandardScaler())]),
                numeric,
            )
        )
    if categorical:
        parts.append(
            (
                "cat",
                Pipeline(
                    [
                        ("fill", SimpleImputer(strategy="most_frequent")),
                        (
                            "hot",
                            OneHotEncoder(
                                handle_unknown="infrequent_if_exist",
                                max_categories=MAX_CATEGORIES,
                                sparse_output=False,
                            ),
                        ),
                    ]
                ),
                categorical,
            )
        )
    return Pipeline([("prep", ColumnTransformer(parts)), ("model", estimator)])


def _classify_target(ds: Dataset, c: ColumnProfile, s: pd.Series, source: str) -> tuple[str, str]:
    distinct = int(s.nunique())
    if c.type in ("integer", "decimal"):
        values = s.dropna()
        whole = bool((values == values.round()).all())
        if whole and distinct <= 10:
            return "classification", (
                f"{c.name} is a number with only {_n(distinct, source)} different whole values, "
                "so it is treated as classes."
            )
        return (
            "regression",
            f"{c.name} is a measured number with {_n(distinct, source)} different values.",
        )
    if c.type in ("boolean", "text"):
        if distinct < 2:
            raise OperationError(f"{c.name} has only one value, so there is nothing to predict.")
        if distinct > MAX_CLASSES:
            raise OperationError(
                f"{c.name} has {distinct} different values; that is not a class label. "
                "Pick a column with at most 20 classes, or a numeric one."
            )
        return (
            "classification",
            f"{c.name} holds {_n(distinct, source)} labels, so it is treated as classes.",
        )
    raise OperationError(f"{c.name} cannot be used as a target (type {c.type}).")


def run_model(ds: Dataset, profile: ProfileResult, spec: ModelSpec) -> tuple[ModelReport, Evidence]:
    """Fit, compare and explain. Raises ``OperationError`` with a plain reason when it cannot."""
    _import_sklearn()
    import sklearn

    source = ds.source.sha256
    by_name = {c.name: c for c in profile.column_profiles}
    columns = {c.name: _series(ds, c) for c in profile.column_profiles if c.type != "empty"}
    if spec.target is None:
        return _cluster(ds, profile, spec, columns, source)
    if spec.target not in by_name:
        raise OperationError(
            f"There is no column {spec.target!r}. Columns are: {', '.join(by_name)[:300]}."
        )
    tcol = by_name[spec.target]
    if tcol.type == "empty" or spec.target not in columns:
        raise OperationError(f"{spec.target} is empty.")
    y_all = columns[spec.target]
    problem, why_target = _classify_target(ds, tcol, y_all.dropna(), source)
    why = [why_target]
    dropped: dict[str, int] = {}
    have = y_all.notna()
    if (~have).any():
        dropped["target_missing"] = int((~have).sum())
    index = y_all.index[have]
    stamps: pd.Series | None = None
    if spec.time_column is not None:
        tprof = by_name.get(spec.time_column)
        if (
            tprof is None
            or tprof.type not in ("date", "datetime")
            or spec.time_column not in columns
        ):
            raise OperationError(
                f"{spec.time_column!r} is not a date column, so the rows cannot be put in time order."
            )
        if spec.time_column == spec.target:
            raise OperationError("The target and the time column must be different columns.")
        stamps = pd.to_datetime(columns[spec.time_column], format="ISO8601", errors="coerce")
        no_time = stamps.loc[index].isna()
        if no_time.any():
            dropped["time_missing"] = int(no_time.sum())
            index = index[~no_time.to_numpy()]
    rows_total = len(index)
    if rows_total < MIN_ROWS:
        raise OperationError(
            f"Only {rows_total} rows have a value for {spec.target}; at least {MIN_ROWS} are needed."
        )
    warnings: list[str] = []
    if rows_total > MAX_ROWS:
        rng = np.random.RandomState(spec.seed)
        index = pd.Index(sorted(rng.choice(index.to_numpy(), size=MAX_ROWS, replace=False)))
        dropped["sampled_out"] = rows_total - MAX_ROWS
        warnings.append(
            f"The file has {_n(rows_total, source)} usable rows; a fixed-seed sample of "
            f"{_n(MAX_ROWS, source)} was used for speed. Scores describe that sample."
        )
    if stamps is not None:
        order = np.argsort(stamps.loc[index].to_numpy(), kind="stable")
        index = index[order]
    columns = {k: v.loc[index] for k, v in columns.items()}
    y_series = columns[spec.target]
    rows = len(index)
    if stamps is not None and rows < SMALL_DATA:
        raise OperationError(
            f"A test on the latest rows needs at least {SMALL_DATA} rows with a value and a date; "
            f"{rows} are usable. Cross-validation would let the model learn from the future, so it "
            "is not offered here."
        )
    keep, excluded = _exclusions(
        ds, profile, spec.target, columns, y_series, problem, rows, source, spec.time_column
    )
    if not keep:
        raise OperationError(
            "No column is usable as a predictor after leaving out identifiers, copies of the target "
            "and unusable columns: "
            + "; ".join(f"{e['column']} ({e['reason']})" for e in excluded)[:300]
        )
    numeric = [c.name for c in keep if c.type in ("integer", "decimal")]
    categorical = [c.name for c in keep if c.name not in numeric]
    for name in categorical:
        distinct = int(columns[name].nunique(dropna=True))
        if distinct > MAX_CATEGORIES:
            warnings.append(
                f"{name} has {_n(distinct, source)} different values: the "
                f"{_n(MAX_CATEGORIES - 1, source)} most common in the training rows are kept apart "
                "and all the others are grouped as one 'other' value."
            )
    X = pd.DataFrame({name: columns[name] for name in numeric + categorical})
    X = X.reset_index(drop=True)
    if problem == "classification":
        # "class_" prefix: scikit-learn mis-reads labels like "0" and "1" as numbers when class weights
        # are used; the labels are never shown, so the prefix is invisible
        y = np.array(["class_" + _label(v) for v in y_series.tolist()], dtype=object)
        classes = sorted({str(v) for v in y})
        counts = {c: int((y == c).sum()) for c in classes}
        smallest = min(counts.values())
        if smallest / rows < IMBALANCE:
            warnings.append(
                "The classes are very unequal "
                f"(the smallest is {_n(round(smallest * 100 / rows, 1), source, 1)}% of the rows), so accuracy "
                "alone is misleading; compare F1 and the baseline."
            )
    else:
        y = y_series.to_numpy(dtype="float64")
        classes = []
        smallest = rows
    if rows < SMALL_DATA:  # (the time-based test refuses this case above)
        warnings.append(
            f"Only {_n(rows, source)} rows: scores come from {FOLDS}-fold cross-validation and are noisy; "
            "treat small differences between models as noise."
        )
    balanced = bool(classes) and smallest / rows < IMBALANCE
    est = _estimators(problem, spec.seed, balanced)
    evidence = Evidence(problem)
    results: list[dict[str, Any]] = []
    folds = FOLDS if rows <= 5000 else 3

    def fold_splitter(labels: np.ndarray | None) -> Any:
        from sklearn.model_selection import KFold, StratifiedKFold

        if problem == "classification" and labels is not None:
            per = min(int(pd.Series(labels).value_counts().min()), folds)
            if per >= 2:
                return StratifiedKFold(n_splits=per, shuffle=True, random_state=spec.seed)
        return KFold(n_splits=folds, shuffle=True, random_state=spec.seed)

    def out_of_fold(pipe: Any, Xs: pd.DataFrame, ys: np.ndarray) -> Any:
        from sklearn.model_selection import cross_val_predict

        method = "predict_proba" if problem == "classification" else "predict"
        return cross_val_predict(
            pipe, Xs, ys, cv=fold_splitter(ys if classes else None), method=method
        )

    def forward_chain(pipe: Any, Xs: pd.DataFrame, ys: np.ndarray) -> tuple[np.ndarray, Any]:
        """Each later block predicted by a model fitted only on the rows before it."""
        from sklearn.base import clone
        from sklearn.model_selection import TimeSeriesSplit

        truth: list[np.ndarray] = []
        preds: list[np.ndarray] = []
        for fit_i, val_i in TimeSeriesSplit(n_splits=TIME_SPLITS).split(Xs):
            if classes and len(set(ys[fit_i].tolist())) < 2:
                continue  # a block cannot be learned from one class alone
            model = clone(pipe).fit(Xs.iloc[fit_i], ys[fit_i])
            if classes:
                got = model.predict_proba(Xs.iloc[val_i])
                full = np.zeros((len(val_i), len(classes)))
                for col, label in enumerate(model.classes_):
                    full[:, classes.index(str(label))] = got[:, col]
                preds.append(full)
            else:
                preds.append(np.asarray(model.predict(Xs.iloc[val_i]), dtype="float64"))
            truth.append(ys[val_i])
        if not truth:
            raise OperationError(
                "The earlier rows hold only one class in their first blocks, so models cannot be "
                "compared over time."
            )
        return np.concatenate(truth), np.concatenate(preds)

    def score(ys: np.ndarray, pred: Any) -> dict[str, float | None]:
        return classification_scores(ys, pred, classes) if classes else regression_scores(ys, pred)

    holdout = rows >= SMALL_DATA
    timed = stamps is not None
    if timed:
        assert stamps is not None
        n_test = max(1, round(rows * TEST_SIZE))
        train_idx, test_idx = np.arange(rows - n_test), np.arange(rows - n_test, rows)
        X_train, X_test = X.iloc[train_idx], X.iloc[test_idx]
        y_train, y_test = y[train_idx], y[test_idx]
        ordered = stamps.loc[index].reset_index(drop=True)
        evaluation: dict[str, Any] = {
            "method": "time_holdout",
            "time_column": spec.time_column,
            "train_rows": int(len(train_idx)),
            "test_rows": int(len(test_idx)),
            "train_until": ordered.iloc[len(train_idx) - 1].isoformat(),
            "test_from": ordered.iloc[len(train_idx)].isoformat(),
            "seed": spec.seed,
            "selection": "best score in forward-chaining validation inside the earlier rows (each "
            "block is predicted by models fitted only on rows before it); reported on the latest "
            "rows, which no model saw",
        }
        if ordered.iloc[len(train_idx) - 1] == ordered.iloc[len(train_idx)]:
            warnings.append(
                "Rows share the date at the edge between the earlier and the latest rows, so a few "
                "rows on the same date are on both sides."
            )
    elif holdout:
        from sklearn.model_selection import train_test_split

        stratify = y if classes and smallest >= 2 else None
        idx = np.arange(rows)
        try:
            train_idx, test_idx = train_test_split(
                idx, test_size=TEST_SIZE, random_state=spec.seed, stratify=stratify
            )
        except ValueError:
            train_idx, test_idx = train_test_split(idx, test_size=TEST_SIZE, random_state=spec.seed)
        train_idx, test_idx = np.sort(train_idx), np.sort(test_idx)
        X_train, X_test = X.iloc[train_idx], X.iloc[test_idx]
        y_train, y_test = y[train_idx], y[test_idx]
        evaluation = {
            "method": "holdout",
            "train_rows": int(len(train_idx)),
            "test_rows": int(len(test_idx)),
            "seed": spec.seed,
            "selection": "best score in cross-validation inside the training rows; reported on the "
            "held-out rows, which no model saw",
        }
    else:
        X_train, y_train, X_test, y_test = X, y, X, y
        evaluation = {
            "method": "cross_validation",
            "folds": folds,
            "rows": rows,
            "seed": spec.seed,
            "selection": f"best out-of-fold score in {folds}-fold cross-validation; every row is scored "
            "by a model that did not see it",
        }
    if balanced:
        evaluation["class_weights"] = (
            "balanced for logistic regression, decision tree and random forest (rarer classes count "
            "more); gradient boosting has no such option"
        )
    fitted: dict[str, Any] = {}
    for name, estimator in est:
        pipe = _pipeline(estimator, numeric, categorical)
        if timed:
            sel_true, pred_sel = forward_chain(pipe, X_train, y_train)
        else:
            sel_true, pred_sel = y_train, out_of_fold(pipe, X_train, y_train)
        selection = score(sel_true, pred_sel)
        evidence.scored.append((name, "selection", sel_true, pred_sel, classes or None, selection))
        if holdout:
            from sklearn.base import clone

            final = clone(pipe).fit(X_train, y_train)
            pred_test = final.predict_proba(X_test) if classes else final.predict(X_test)
            metrics = score(y_test, pred_test)
            evidence.scored.append((name, "test", y_test, pred_test, classes or None, metrics))
            fitted[name] = final
        else:
            metrics = selection
        results.append({"name": name, "selection": selection, "metrics": metrics})
    primary = "f1" if classes else "rmse"
    higher_is_better = bool(classes)

    def key(entry: dict[str, Any]) -> float:
        v = entry["selection"][primary]
        return -math.inf if v is None else (v if higher_is_better else -v)

    best_entry = max(results, key=key)  # first wins a tie: the baseline is listed first
    base_entry = results[0]
    best = best_entry["name"]
    for entry in results:
        entry["best"] = entry["name"] == best
    if best == base_entry["name"]:
        warnings.append(
            "No model beat the baseline (always predicting the most common class, or the mean). "
            "The columns may not hold a usable signal for this target."
        )
    importance: list[dict[str, Any]] = []
    if best != base_entry["name"]:
        importance, note = _importance(
            best,
            est,
            numeric,
            categorical,
            fitted,
            X,
            y,
            X_test,
            y_test,
            holdout,
            classes,
            spec.seed,
        )
        evaluation["importance_note"] = note
    why += [
        f"{_n(rows, source)} usable rows; {_n(len(keep), source)} columns used as predictors; "
        f"{_n(len(excluded), source)} left out (reasons below).",
    ]
    report = ModelReport(
        spec=spec,
        problem=problem,
        why=why,
        rows_used=rows,
        rows_dropped=dropped,
        features=[{"column": c.name, "type": c.type} for c in keep],
        excluded=excluded,
        evaluation=evaluation,
        models=results,
        best=best,
        importance=importance,
        warnings=warnings,
        library={"scikit-learn": sklearn.__version__},
    )
    from sie.workspace.model_verify import verify_report

    report.verification = verify_report(report, evidence)
    return report, evidence


def _importance(
    best: str,
    est: list[tuple[str, Any]],
    numeric: list[str],
    categorical: list[str],
    fitted: dict[str, Any],
    X: pd.DataFrame,
    y: np.ndarray,
    X_test: pd.DataFrame,
    y_test: np.ndarray,
    holdout: bool,
    classes: list[str],
    seed: int,
) -> tuple[list[dict[str, Any]], str]:
    from sklearn.base import clone
    from sklearn.inspection import permutation_importance

    if holdout:
        model = fitted[best]
        note = "Permutation importance on the held-out rows: how much the score drops when a column is shuffled."
    else:
        proto = dict(est)[best]
        model = clone(_pipeline(proto, numeric, categorical)).fit(X, y)
        note = (
            "Permutation importance on the same rows the model was fitted on (the data is small), "
            "so read it as a rough guide."
        )
    scoring = "f1_macro" if classes else "neg_root_mean_squared_error"
    result = permutation_importance(
        model, X_test, y_test, scoring=scoring, n_repeats=5, random_state=seed, n_jobs=1
    )
    order = sorted(
        range(len(X_test.columns)),
        key=lambda i: (-result.importances_mean[i], str(X_test.columns[i])),
    )
    rows = [
        {
            "column": str(X_test.columns[i]),
            "drop": _r(result.importances_mean[i]),
            "spread": _r(result.importances_std[i]),
        }
        for i in order[:10]
    ]
    return rows, note


# -- clustering -------------------------------------------------------------------------------------


def _cluster(
    ds: Dataset,
    profile: ProfileResult,
    spec: ModelSpec,
    columns: dict[str, pd.Series],
    source: str,
) -> tuple[ModelReport, Evidence]:
    import sklearn
    from sklearn.cluster import KMeans
    from sklearn.metrics import silhouette_score

    rows_all = len(ds.frame)
    keep, excluded = _exclusions(ds, profile, None, columns, None, "clustering", rows_all, source)
    numeric = [c.name for c in keep if c.type in ("integer", "decimal")]
    categorical = [c.name for c in keep if c.name not in numeric]
    if not numeric:
        raise OperationError("Finding groups needs at least one numeric column that is usable.")
    if rows_all < MIN_ROWS:
        raise OperationError(
            f"Only {rows_all} rows; at least {MIN_ROWS} are needed to look for groups."
        )
    warnings: list[str] = []
    index = list(range(rows_all))
    dropped: dict[str, int] = {}
    if rows_all > CLUSTER_MAX_ROWS:
        rng = np.random.RandomState(spec.seed)
        index = sorted(rng.choice(rows_all, size=CLUSTER_MAX_ROWS, replace=False).tolist())
        dropped["sampled_out"] = rows_all - CLUSTER_MAX_ROWS
        warnings.append(
            f"The file has {_n(rows_all, source)} rows; a fixed-seed sample of "
            f"{_n(CLUSTER_MAX_ROWS, source)} was used. Groups describe that sample."
        )
    X = pd.DataFrame({n: columns[n].iloc[index].to_numpy() for n in numeric + categorical})
    prep = _pipeline("passthrough", numeric, categorical).named_steps["prep"]
    matrix = np.asarray(prep.fit_transform(X), dtype="float64")
    if rows_all < SMALL_DATA:
        warnings.append(
            f"Only {_n(rows_all, source)} rows: groups found in so little data are tentative."
        )
    evidence = Evidence("clustering")
    results: list[dict[str, Any]] = []
    for k in CLUSTER_KS:
        if k >= len(matrix):
            continue
        model = KMeans(n_clusters=k, n_init=10, random_state=spec.seed).fit(matrix)
        labels = model.labels_
        sil = float(silhouette_score(matrix, labels)) if len(set(labels.tolist())) > 1 else None
        results.append(
            {
                "k": k,
                "silhouette": _r(sil),
                "sizes": [int(x) for x in np.bincount(labels, minlength=k)],
            }
        )
        evidence.clusters.append((k, matrix, labels.copy(), _r(sil)))
    best = max(results, key=lambda r: -math.inf if r["silhouette"] is None else r["silhouette"])
    chosen = next(ev for ev in evidence.clusters if ev[0] == best["k"])
    labels = chosen[2]
    sub = X.copy()
    sub["_g"] = labels
    profiles = []
    for g in range(best["k"]):
        part = sub[sub["_g"] == g]
        means = {n: _r(float(pd.to_numeric(part[n]).mean())) for n in numeric}
        common: dict[str, str] = {}
        for n in categorical:  # the value most rows of the group have, and how many of them
            present = part[n].dropna().astype(str).str.strip()
            present = present[present != ""]
            if len(present):
                counts = present.value_counts()
                top = sorted(counts[counts == counts.max()].index)[0]
                common[n] = f"{top} ({round(100 * int(counts[top]) / len(present))} %)"
        profiles.append({"group": g + 1, "rows": int(len(part)), "means": means, "common": common})
    sil = best["silhouette"]
    if sil is None or sil < 0.25:
        warnings.append(
            "The groups are weak: points are not much closer to their own group than to others "
            "(silhouette under 0.25). Treat them as a rough split, not as real segments."
        )
    why = [
        "No target was chosen, so the rows are split into groups of similar rows (k-means); the number "
        f"of groups ({_n(best['k'], source)}) is the one with the best silhouette between 2 and 6.",
        f"{_n(len(index), source)} rows; {_n(len(keep), source)} columns used; "
        f"{_n(len(excluded), source)} left out (reasons below).",
    ]
    report = ModelReport(
        spec=spec,
        problem="clustering",
        why=why,
        rows_used=len(index),
        rows_dropped=dropped,
        features=[{"column": c.name, "type": c.type} for c in keep],
        excluded=excluded,
        evaluation={"method": "none", "seed": spec.seed, "selection": "highest silhouette score"},
        models=results,
        best=f"{best['k']} groups",
        importance=[],
        warnings=warnings,
        clusters={"k": best["k"], "profiles": profiles},
        library={"scikit-learn": sklearn.__version__},
    )
    from sie.workspace.model_verify import verify_report

    report.verification = verify_report(report, evidence)
    return report, evidence


# -- requests in words ------------------------------------------------------------------------------

_PREDICT = re.compile(
    r"^\s*(?:predict|model|classify|forecast|estimate)\s+(?:the\s+)?(?P<col>.+?)\s*$", re.I
)
_OVER_TIME = re.compile(
    r"^(?P<col>.+?)\s+over\s+time(?:\s+(?:using|by|with|on)\s+(?:the\s+)?(?P<time>.+?))?\s*$", re.I
)
_GROUPS = re.compile(
    r"^\s*(?:cluster|segment|find\s+(?:groups|clusters|segments)|group\s+similar\s+rows)\b.*$", re.I
)


def _find_column(wanted: str, columns: list[str]) -> str | None | list[str]:
    wanted = wanted.strip().strip("\"'`").strip()
    exact = [c for c in columns if c == wanted]
    folded = [c for c in columns if c.lower() == wanted.lower()]
    found = exact or folded
    if len(found) == 1:
        return found[0]
    return found or None


def parse_model_request(
    text: str, columns: list[str], date_columns: list[str] | None = None
) -> ModelSpec | str | None:
    """A ``ModelSpec`` for "Predict <column>" (optionally "... over time [using <date column>]") or
    "Find groups", a plain message when a column is not found, or None when the text is not a
    modeling request."""
    if _GROUPS.match(text):
        return ModelSpec(None)
    m = _PREDICT.match(text)
    if not m:
        return None
    wanted = m["col"].strip()
    time_word: str | None = None
    timed = False
    over = _OVER_TIME.match(wanted)
    if over:
        timed = True
        wanted = over["col"].strip()
        time_word = over["time"]
    found = _find_column(wanted, columns)
    if found is None:
        return (
            f"There is no column named {wanted.strip(chr(34) + chr(39) + chr(96))[:60]!r}. "
            f"Columns are: {', '.join(columns)[:300]}."
        )
    if isinstance(found, list):
        return f"More than one column matches {wanted[:60]!r}; use its exact name."
    if not timed:
        return ModelSpec(found)
    dates = date_columns or []
    if time_word is not None:
        chosen = _find_column(time_word, columns)
        if not isinstance(chosen, str):
            return f"There is no single column named {time_word.strip()[:60]!r} to put the rows in order."
        if chosen not in dates:
            return f"{chosen} is not a date column, so it cannot put the rows in time order."
        return ModelSpec(found, time_column=chosen)
    if len(dates) == 1:
        return ModelSpec(found, time_column=dates[0])
    if not dates:
        return "There is no date column to put the rows in time order. Clean a text column to dates first."
    return (
        "There is more than one date column; say which one, for example: "
        f"Predict {found} over time using {dates[0]}. Date columns: {', '.join(dates)[:200]}."
    )
