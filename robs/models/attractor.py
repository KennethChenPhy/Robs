"""Delay embedding and simple regime clustering."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class AttractorResult:
    embedded: pd.DataFrame
    recurrence_distance: pd.Series
    regime: pd.Series


def delay_embed(series: pd.DataFrame, dim: int = 3, tau: int = 2) -> pd.DataFrame:
    if series.empty:
        return pd.DataFrame()
    values = series.to_numpy(dtype=float)
    n_rows, n_cols = values.shape
    rows = []
    index = []
    for t in range((dim - 1) * tau, n_rows):
        vec = []
        for d in range(dim):
            vec.extend(values[t - d * tau].tolist())
        rows.append(vec)
        index.append(series.index[t])
    cols = [f"e{i}" for i in range(len(rows[0]))] if rows else []
    return pd.DataFrame(rows, index=index, columns=cols)


def _kmeans(data: np.ndarray, k: int, max_iter: int = 50) -> np.ndarray:
    if len(data) == 0:
        return np.array([])
    rng = np.random.default_rng(0)
    centroids = data[rng.choice(len(data), size=min(k, len(data)), replace=False)]
    labels = np.zeros(len(data), dtype=int)
    for _ in range(max_iter):
        dists = ((data[:, None, :] - centroids[None, :, :]) ** 2).sum(axis=2)
        labels = dists.argmin(axis=1)
        new_centroids = np.array([data[labels == i].mean(axis=0) if np.any(labels == i) else centroids[i] for i in range(len(centroids))])
        if np.allclose(new_centroids, centroids):
            break
        centroids = new_centroids
    return labels


def analyze_attractor(state: pd.DataFrame, dim: int = 3, tau: int = 2, k: int = 3, knn: int = 5) -> AttractorResult:
    embedded = delay_embed(state, dim=dim, tau=tau)
    if embedded.empty:
        empty = pd.Series(dtype=float)
        return AttractorResult(embedded=embedded, recurrence_distance=empty, regime=empty)

    arr = embedded.to_numpy(dtype=float)
    distances = []
    for i in range(len(arr)):
        start = max(0, i - 100)
        history = arr[start:i]
        if len(history) == 0:
            distances.append(np.nan)
            continue
        d = np.linalg.norm(history - arr[i], axis=1)
        distances.append(float(np.partition(d, min(knn, len(d) - 1))[min(knn, len(d) - 1)]))

    recurrence = pd.Series(distances, index=embedded.index, name="recurrence_distance")
    labels = _kmeans(arr, k=k)
    regime = pd.Series(labels, index=embedded.index, name="regime")
    return AttractorResult(embedded=embedded, recurrence_distance=recurrence, regime=regime)
