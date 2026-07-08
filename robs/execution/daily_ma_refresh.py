"""Refresh daily MA for uncertain-trend flat entries."""

from __future__ import annotations

import logging
from typing import Any, Protocol

from robs.strategy.mhimain import MHImainStrategy
from robs.strategy.trend import TrendMode

LOG = logging.getLogger("robs.mhimain")


class DailyMaQuote(Protocol):
    def daily_ma(self, symbol: str, period: int = 5) -> tuple[bool, float | None]: ...


def refresh_uncertain_daily_ma(
    quote: DailyMaQuote,
    strategy: MHImainStrategy,
    symbol: str,
    ma_period: int,
    *,
    entry_armed: bool,
    last_refresh_mono: float | None,
    now_mono: float,
    refresh_sec: float,
    force: bool = False,
) -> float | None:
    """
    Fetch latest daily MA when uncertain and flat entry is armed.
    Returns updated last_refresh_mono, or unchanged if skipped.
    """
    if strategy.trend != TrendMode.UNCERTAIN or not entry_armed:
        return last_refresh_mono
    if (
        not force
        and last_refresh_mono is not None
        and refresh_sec > 0
        and (now_mono - last_refresh_mono) < refresh_sec
    ):
        return last_refresh_mono

    ok_ma, ma = quote.daily_ma(symbol, period=ma_period)
    if not ok_ma or ma is None:
        LOG.warning(
            f"could not refresh MA{ma_period}; uncertain entry needs MA",
            extra={"event": "ma_refresh", "ma_period": ma_period, "ok": False},
        )
        return now_mono

    prev = strategy.ma5
    strategy.set_ma5(ma)
    if prev is None or abs(prev - ma) >= 0.05:
        LOG.info(
            f"MA{ma_period} (daily): {ma:.1f}",
            extra={"event": "ma_refresh", "ma_period": ma_period, "ma": ma, "prev_ma": prev},
        )
    return now_mono
