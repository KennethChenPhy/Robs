"""Poll market snapshots from Futu OpenD."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Callable, Iterable

import pandas as pd

from robs.data.futu_client import QuoteClient


def snapshots_to_frame(data: pd.DataFrame) -> pd.DataFrame:
    rows = []
    now = datetime.now(timezone.utc)
    for _, row in data.iterrows():
        rows.append(
            {
                "timestamp": now,
                "code": row["code"],
                "last_price": float(row["last_price"]),
                "open_price": float(row.get("open_price", row["last_price"])),
                "high_price": float(row.get("high_price", row["last_price"])),
                "low_price": float(row.get("low_price", row["last_price"])),
                "volume": float(row.get("volume", 0)),
            }
        )
    return pd.DataFrame(rows)


class SnapshotPoller:
    def __init__(self, quote: QuoteClient, tickers: list[str]) -> None:
        self.quote = quote
        self.tickers = tickers
        self.last_poll_at: datetime | None = None

    def poll_once(self) -> pd.DataFrame:
        ok, data = self.quote.snapshot(self.tickers)
        if not ok or data is None or len(data) == 0:
            return pd.DataFrame()
        self.last_poll_at = datetime.now(timezone.utc)
        return snapshots_to_frame(data)

    def run(
        self,
        interval_sec: float,
        on_snapshot: Callable[[pd.DataFrame], None],
        max_iterations: int | None = None,
    ) -> None:
        count = 0
        try:
            while max_iterations is None or count < max_iterations:
                frame = self.poll_once()
                if not frame.empty:
                    on_snapshot(frame)
                count += 1
                time.sleep(interval_sec)
        except KeyboardInterrupt:
            return

    def is_stale(self, multiplier: float, interval_sec: float) -> bool:
        if self.last_poll_at is None:
            return True
        age = (datetime.now(timezone.utc) - self.last_poll_at).total_seconds()
        return age > interval_sec * multiplier
