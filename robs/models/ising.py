"""Rolling Ising couplings via pseudo-likelihood."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy.optimize import minimize
from scipy.special import expit


@dataclass
class IsingFitResult:
    couplings: pd.DataFrame
    fields: pd.Series
    magnetization: pd.Series
    energy: pd.Series


def _pseudo_loglik(params: np.ndarray, spins: np.ndarray) -> float:
    n_spins = spins.shape[1]
    j_raw = params[: n_spins * (n_spins - 1) // 2]
    h = params[n_spins * (n_spins - 1) // 2 :]

    j_mat = np.zeros((n_spins, n_spins))
    idx = 0
    for i in range(n_spins):
        for j in range(i + 1, n_spins):
            j_mat[i, j] = j_raw[idx]
            j_mat[j, i] = j_raw[idx]
            idx += 1

    ll = 0.0
    for t in range(spins.shape[0]):
        s = spins[t]
        for i in range(n_spins):
            local_field = h[i] + np.sum(j_mat[i] * s) - j_mat[i, i] * s[i]
            p = expit(2 * local_field)
            ll += np.log(p) if s[i] > 0 else np.log(1 - p)
    return -ll


def fit_ising_window(spin_window: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    values = spin_window.to_numpy(dtype=float)
    n_samples, n_spins = values.shape
    if n_samples < 5:
        return np.zeros((n_spins, n_spins)), np.zeros(n_spins)

    x0 = np.zeros(n_spins * (n_spins - 1) // 2 + n_spins)
    result = minimize(_pseudo_loglik, x0, args=(values,), method="L-BFGS-B")
    params = result.x

    j_mat = np.zeros((n_spins, n_spins))
    idx = 0
    for i in range(n_spins):
        for j in range(i + 1, n_spins):
            j_mat[i, j] = params[idx]
            j_mat[j, i] = params[idx]
            idx += 1
    h = params[idx:]
    return j_mat, h


def rolling_ising(spins: pd.DataFrame, window: int = 120) -> IsingFitResult:
    codes = list(spins.columns)
    m_vals = []
    e_vals = []
    j_frames = []
    h_frames = []

    for end in range(window, len(spins) + 1):
        chunk = spins.iloc[end - window : end]
        j_mat, h = fit_ising_window(chunk)
        s_last = chunk.iloc[-1].to_numpy(dtype=float)
        m = float(np.mean(s_last))
        energy = -float(s_last @ j_mat @ s_last) - float(np.dot(h, s_last))
        m_vals.append(m)
        e_vals.append(energy)
        j_frames.append(j_mat)
        h_frames.append(h)

    index = spins.index[window - 1 :]
    magnetization = pd.Series(m_vals, index=index, name="magnetization")
    energy = pd.Series(e_vals, index=index, name="energy")
    fields = pd.DataFrame(h_frames, index=index, columns=codes)
    couplings = pd.Series(j_frames, index=index, name="J")
    return IsingFitResult(couplings=couplings, fields=fields.mean(), magnetization=magnetization, energy=energy)
