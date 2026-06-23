"""Guard market orders when last price is too far from the executable quote."""

from __future__ import annotations

from typing import Any


def market_execution_price(row: Any, side: str) -> float:
    """Price a market order would likely hit: ask for BUY, bid for SELL."""
    last = float(row["last_price"])
    side = side.upper()
    if side == "BUY":
        ask = row.get("ask_price")
        if ask is not None and ask == ask and float(ask) > 0:
            return float(ask)
    elif side == "SELL":
        bid = row.get("bid_price")
        if bid is not None and bid == bid and float(bid) > 0:
            return float(bid)
    return last


def allow_market_order(
    row: Any,
    side: str,
    max_slippage_pts: float = 3.0,
) -> tuple[bool, str, float, float]:
    """
    Allow market order only if |last_price - market_price| < max_slippage_pts.

    Returns (allowed, reason, last_price, market_price).
    """
    last = float(row["last_price"])
    market_px = market_execution_price(row, side)
    diff = abs(last - market_px)
    if diff >= max_slippage_pts:
        return (
            False,
            f"market blocked: |last {last:.1f} − {side} {market_px:.1f}| = {diff:.1f}pts "
            f"(max {max_slippage_pts:.1f})",
            last,
            market_px,
        )
    return True, "ok", last, market_px
