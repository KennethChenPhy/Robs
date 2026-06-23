"""Aggregate snapshots into OHLCV bars."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Deque

import pandas as pd


def _bar_start(ts: datetime, interval: str) -> datetime:
    ts = ts.astimezone(timezone.utc)
    if interval == "1d":
        return ts.replace(hour=0, minute=0, second=0, microsecond=0)
    if interval == "1m":
        return ts.replace(second=0, microsecond=0)
    if interval.endswith("m"):
        minutes = int(interval[:-1])
        minute = (ts.minute // minutes) * minutes
        return ts.replace(minute=minute, second=0, microsecond=0)
    raise ValueError(f"Unsupported bar interval: {interval}")


@dataclass
class _BarBuilder:
    code: str
    bar_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    def update(self, price: float, volume: float) -> None:
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.close = price
        self.volume += volume

    def to_row(self) -> dict:
        return {
            "code": self.code,
            "bar_time": self.bar_time,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
        }


@dataclass
class BarAggregator:
    interval: str = "1m"
    _active: dict[str, _BarBuilder] = field(default_factory=dict)
    _history: dict[str, Deque[dict]] = field(default_factory=lambda: defaultdict(lambda: deque(maxlen=5000)))

    def ingest(self, snapshots: pd.DataFrame) -> pd.DataFrame:
        closed: list[dict] = []
        for _, row in snapshots.iterrows():
            code = str(row["code"])
            price = float(row["last_price"])
            volume = float(row.get("volume", 0))
            ts = pd.Timestamp(row["timestamp"]).to_pydatetime()
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            start = _bar_start(ts, self.interval)

            current = self._active.get(code)
            if current is None:
                self._active[code] = _BarBuilder(code, start, price, price, price, price, volume)
                continue

            if start != current.bar_time:
                closed.append(current.to_row())
                self._history[code].append(current.to_row())
                self._active[code] = _BarBuilder(code, start, price, price, price, price, volume)
            else:
                current.update(price, volume)

        if not closed:
            return pd.DataFrame()
        return pd.DataFrame(closed)

    def flush(self) -> pd.DataFrame:
        rows = [builder.to_row() for builder in self._active.values()]
        self._active.clear()
        return pd.DataFrame(rows) if rows else pd.DataFrame()

    def history_frame(self, code: str) -> pd.DataFrame:
        rows = list(self._history.get(code, []))
        active = self._active.get(code)
        if active is not None:
            rows.append(active.to_row())
        return pd.DataFrame(rows)

    @staticmethod
    def resample_daily(bars_1m: pd.DataFrame) -> pd.DataFrame:
        if bars_1m.empty:
            return bars_1m
        frames = []
        for code, group in bars_1m.groupby("code"):
            g = group.copy()
            g["bar_time"] = pd.to_datetime(g["bar_time"], utc=True)
            g = g.set_index("bar_time")
            daily = g.resample("1D").agg(
                {
                    "open": "first",
                    "high": "max",
                    "low": "min",
                    "close": "last",
                    "volume": "sum",
                }
            ).dropna(subset=["close"])
            daily = daily.reset_index()
            daily["code"] = code
            frames.append(daily)
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
