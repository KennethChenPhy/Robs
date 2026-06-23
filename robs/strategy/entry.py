"""Trend-based entry rules for MHImain when flat."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from robs.strategy.rules import Action, Signal
from robs.strategy.trend import TrendMode


@dataclass
class TrendEntry:
    trend: TrendMode
    ma5: float | None = None

    def evaluate(self, price: float, symbol: str) -> Signal | None:
        if self.trend == TrendMode.BULL:
            return Signal(
                "trend_entry",
                Action.BUY,
                symbol,
                "bull trend: open long",
                {"price": price, "trend": self.trend.value},
            )

        if self.trend == TrendMode.BEAR:
            return Signal(
                "trend_entry",
                Action.SELL,
                symbol,
                "bear trend: open short",
                {"price": price, "trend": self.trend.value},
            )

        if self.ma5 is None:
            return None

        if price < self.ma5:
            return Signal(
                "trend_entry",
                Action.BUY,
                symbol,
                f"uncertain: price {price:.1f} < MA5 {self.ma5:.1f} → long",
                {"price": price, "ma5": self.ma5, "trend": self.trend.value},
            )
        if price > self.ma5:
            return Signal(
                "trend_entry",
                Action.SELL,
                symbol,
                f"uncertain: price {price:.1f} > MA5 {self.ma5:.1f} → short",
                {"price": price, "ma5": self.ma5, "trend": self.trend.value},
            )

        return Signal(
            "trend_entry",
            Action.HOLD,
            symbol,
            f"uncertain: price {price:.1f} == MA5 {self.ma5:.1f}",
            {"price": price, "ma5": self.ma5},
        )
