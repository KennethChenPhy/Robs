"""Plain observables for experience-based rules."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Deque

import pandas as pd


@dataclass
class RollingReturnTracker:
    window_sec: int
    poll_interval_sec: int
    prices: Deque[float] = field(default_factory=deque)

    def __post_init__(self) -> None:
        buffer_size = max(2, self.window_sec // self.poll_interval_sec)
        self.prices = deque(maxlen=buffer_size)

    def update(self, price: float) -> float | None:
        self.prices.append(price)
        if len(self.prices) < self.prices.maxlen:
            return None
        oldest = sum(self.prices) / len(self.prices)
        current = self.prices[-1]
        if oldest == 0:
            return None
        return ((current - oldest) / oldest) * 100.0


@dataclass
class PairSpreadFeatures:
    ticker_a: str
    ticker_b: str
    window_sec: int
    poll_interval_sec: int
    tracker_a: RollingReturnTracker = field(init=False)
    tracker_b: RollingReturnTracker = field(init=False)

    def __post_init__(self) -> None:
        self.tracker_a = RollingReturnTracker(self.window_sec, self.poll_interval_sec)
        self.tracker_b = RollingReturnTracker(self.window_sec, self.poll_interval_sec)

    def update(self, prices: dict[str, float]) -> dict[str, float | str | None]:
        ret_a = self.tracker_a.update(prices.get(self.ticker_a, 0.0)) if self.ticker_a in prices else None
        ret_b = self.tracker_b.update(prices.get(self.ticker_b, 0.0)) if self.ticker_b in prices else None
        spread = None
        same_sign = None
        neg_cor = False
        if ret_a is not None and ret_b is not None:
            spread = ret_a - ret_b
            same_sign = (ret_a >= 0 and ret_b >= 0) or (ret_a <= 0 and ret_b <= 0)
            neg_cor = (ret_a > 0 and ret_b < 0) or (ret_a < 0 and ret_b > 0)
        return {
            "ret_a": ret_a,
            "ret_b": ret_b,
            "spread_pct": spread,
            "same_sign": same_sign,
            "neg_correlation": neg_cor,
        }


@dataclass
class PointBreakoutFeatures:
    symbol: str
    threshold_pts: float
    ref_price: float | None = None
    accumulated_pts: float = 0.0

    def update(self, price: float) -> dict[str, float | bool | None]:
        if self.ref_price is None:
            self.ref_price = price
            return {
                "symbol": self.symbol,
                "price": price,
                "move_pts": 0.0,
                "breakout": False,
                "direction": None,
                "accumulated_pts": 0.0,
            }

        move = price - self.ref_price
        breakout = abs(move) >= self.threshold_pts
        direction = None
        if breakout:
            direction = "LONG" if move > 0 else "SHORT"
            self.accumulated_pts += move
            self.ref_price = price

        return {
            "symbol": self.symbol,
            "price": price,
            "move_pts": move,
            "breakout": breakout,
            "direction": direction,
            "accumulated_pts": self.accumulated_pts,
        }


def bar_returns(bars: pd.DataFrame, code: str, window: int = 20) -> pd.Series:
    subset = bars[bars["code"] == code]["close"].astype(float)
    if subset.empty:
        return pd.Series(dtype=float)
    return subset.pct_change().rolling(window).mean()
