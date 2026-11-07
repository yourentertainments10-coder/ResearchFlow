"""Independent re-computation of every score in a model report, from the stored predictions, with numpy
and pandas only (no scikit-learn metric code).

Formulas written out here: accuracy; per-class precision, recall and F1 (0 when undefined) averaged over
classes; ROC-AUC from average ranks (Mann-Whitney), one-vs-rest macro for more than two classes; RMSE, MAE
and R squared; the silhouette of a clustering from the full distance matrix. Any difference larger than
1e-6 (the report rounds to six decimals) makes the report ``unverified``.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

from sie.workspace.models import Verification

if TYPE_CHECKING:
    from sie.workspace.modeling import Evidence, ModelReport

TOLERANCE = 2e-6


def _auc(positive: np.ndarray, score: np.ndarray) -> float | None:
    n_pos = int(positive.sum())
    n_neg = int(len(positive) - n_pos)
    if n_pos == 0 or n_neg == 0:
        return None
    ranks = pd.Series(score).rank(method="average").to_numpy()
    return float((ranks[positive].sum() - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg))


def classification(y_true: np.ndarray, proba: np.ndarray, classes: list[str]) -> dict[str, Any]:
    labels = np.array(classes, dtype=object)
    pred = labels[np.argmax(proba, axis=1)]
    precision, recall, f1 = [], [], []
    for c in classes:
        tp = float(np.sum((y_true == c) & (pred == c)))
        fp = float(np.sum((y_true != c) & (pred == c)))
        fn = float(np.sum((y_true == c) & (pred != c)))
        p = tp / (tp + fp) if tp + fp else 0.0
        r = tp / (tp + fn) if tp + fn else 0.0
        precision.append(p)
        recall.append(r)
        f1.append(2 * p * r / (p + r) if p + r else 0.0)
    present = set(y_true.tolist())
    auc: float | None
    if len(classes) < 2 or not all(c in present for c in classes):
        auc = None
    elif len(classes) == 2:
        auc = _auc(y_true == classes[1], proba[:, 1])
    else:
        parts = [_auc(y_true == c, proba[:, i]) for i, c in enumerate(classes)]
        auc = None if any(a is None for a in parts) else float(np.mean(parts))
    return {
        "accuracy": float(np.mean(y_true == pred)),
        "precision": float(np.mean(precision)),
        "recall": float(np.mean(recall)),
        "f1": float(np.mean(f1)),
        "roc_auc": auc,
    }


def regression(y_true: np.ndarray, y_pred: np.ndarray) -> dict[str, Any]:
    err = y_true - y_pred
    ss_tot = float(np.sum((y_true - np.mean(y_true)) ** 2))
    return {
        "rmse": math.sqrt(float(np.mean(err**2))),
        "mae": float(np.mean(np.abs(err))),
        "r2": None if ss_tot == 0 else 1 - float(np.sum(err**2)) / ss_tot,
    }


def silhouette(matrix: np.ndarray, labels: np.ndarray) -> float | None:
    groups = np.unique(labels)
    if len(groups) < 2:
        return None
    diff = matrix[:, None, :] - matrix[None, :, :]
    dist = np.sqrt((diff**2).sum(axis=2))
    scores = np.zeros(len(matrix))
    for i in range(len(matrix)):
        own = labels == labels[i]
        n_own = int(own.sum())
        if n_own == 1:
            continue  # a lone point scores 0
        a = dist[i][own].sum() / (n_own - 1)
        b = min(dist[i][labels == g].mean() for g in groups if g != labels[i])
        scores[i] = (b - a) / max(a, b) if max(a, b) > 0 else 0.0
    return float(scores.mean())


def _agree(reported: dict[str, Any], again: dict[str, Any]) -> list[str]:
    bad = []
    for key, value in again.items():
        got = reported.get(key)
        if value is None or got is None:
            if (value is None) != (got is None):
                bad.append(f"{key}: reported {got}, recomputed {value}")
        elif abs(float(got) - float(value)) > TOLERANCE:
            bad.append(f"{key}: reported {got}, recomputed {round(float(value), 6)}")
    return bad


def verify_report(report: ModelReport, evidence: Evidence) -> Verification:
    """Recompute everything the report shows from the stored predictions."""
    mismatches: list[str] = []
    checks = 0
    try:
        for name, which, y_true, pred, classes, reported in evidence.scored:
            again = (
                classification(np.asarray(y_true, dtype=object), np.asarray(pred), classes)
                if classes
                else regression(
                    np.asarray(y_true, dtype="float64"), np.asarray(pred, dtype="float64")
                )
            )
            checks += len(again)
            mismatches += [f"{name} ({which}) {m}" for m in _agree(reported, again)]
        for k, matrix, labels, reported_sil in evidence.clusters:
            again_sil = silhouette(matrix, labels)
            checks += 1
            if (again_sil is None) != (reported_sil is None) or (
                again_sil is not None and abs(again_sil - reported_sil) > TOLERANCE
            ):
                mismatches.append(
                    f"{k} groups: silhouette reported {reported_sil}, recomputed {again_sil}"
                )
    except Exception as exc:  # noqa: BLE001 - any failure of the second path means "not verified"
        return Verification(
            "unverified", checks, [], f"the independent score check failed: {exc}"[:300]
        )
    if mismatches:
        return Verification("unverified", checks, mismatches, "; ".join(mismatches[:3]))
    return Verification("verified", checks)
