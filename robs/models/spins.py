"""Map continuous features to binary spins."""

from __future__ import annotations

import numpy as np
import pandas as pd


def encode_spins(returns: pd.Series, dead_zone: float = 0.0) -> pd.Series:
    values = returns.fillna(0.0)
    spins = np.sign(values).astype(int)
    if dead_zone > 0:
        spins[values.abs() < dead_zone] = 0
    spins[spins == 0] = 1
    return pd.Series(spins, index=returns.index, name="spin")


def spin_matrix(price_frames: dict[str, pd.Series], window: int = 20, dead_zone: float = 0.0) -> pd.DataFrame:
    cols = {}
    for code, prices in price_frames.items():
        ret = prices.pct_change().rolling(window).mean()
        cols[code] = encode_spins(ret, dead_zone=dead_zone)
    return pd.DataFrame(cols).dropna()
