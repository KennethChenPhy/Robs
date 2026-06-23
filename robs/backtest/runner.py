"""Walk-forward backtest on stored bars."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from robs.strategy.engine import StrategyEngine
from robs.strategy.rules import Action


@dataclass
class BacktestResult:
    ok: bool
    total_bars: int
    trade_count: int
    pnl: float
    max_drawdown_pct: float
    trades: list[dict[str, Any]] = field(default_factory=list)
    reason: str = ""


@dataclass
class BacktestRunner:
    cfg: dict[str, Any]
    engine: StrategyEngine

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> BacktestRunner:
        return cls(cfg=cfg, engine=StrategyEngine(cfg))

    def run(self, bars: pd.DataFrame) -> BacktestResult:
        min_bars = int(self.cfg.get("backtest", {}).get("min_bars", 500))
        max_dd = float(self.cfg.get("backtest", {}).get("max_drawdown_pct", 15.0))
        min_trades = int(self.cfg.get("backtest", {}).get("min_trades", 1))

        if bars.empty:
            return BacktestResult(False, 0, 0, 0.0, 0.0, reason="no bars")

        total_bars = len(bars)
        if total_bars < min_bars:
            return BacktestResult(
                False,
                total_bars,
                0,
                0.0,
                0.0,
                reason=f"only {total_bars} bars, need {min_bars}",
            )

        primary = self.cfg.get("universe", {}).get("primary", "HK.03188")
        cash = 0.0
        position = 0
        entry_price = 0.0
        equity_curve: list[float] = [0.0]
        trades: list[dict[str, Any]] = []

        grouped = bars.groupby("bar_time", sort=True)
        for bar_time, group in grouped:
            prices = {str(row["code"]): float(row["close"]) for _, row in group.iterrows()}
            signals = self.engine.update_from_prices(prices)
            signal = self.engine.pick_action(signals)
            if signal is None:
                continue

            price = prices.get(signal.trade_ticker)
            if price is None:
                continue

            if signal.action == Action.BUY and position <= 0:
                if position < 0:
                    cash += (entry_price - price)
                position = 1
                entry_price = price
                trades.append({"action": "BUY", "price": price, "rule": signal.rule})
            elif signal.action == Action.SELL and position >= 0:
                if position > 0:
                    cash += (price - entry_price)
                position = -1
                entry_price = price
                trades.append({"action": "SELL", "price": price, "rule": signal.rule})
            elif signal.action == Action.FLAT and position != 0:
                if position > 0:
                    cash += (price - entry_price)
                else:
                    cash += (entry_price - price)
                position = 0
                entry_price = 0.0
                trades.append({"action": "FLAT", "price": price, "rule": signal.rule})

            mark = cash
            if position > 0:
                mark += price - entry_price
            elif position < 0:
                mark += entry_price - price
            equity_curve.append(mark)

        peak = 0.0
        max_drawdown = 0.0
        for eq in equity_curve:
            peak = max(peak, eq)
            if peak > 0:
                dd = (peak - eq) / peak * 100.0
                max_drawdown = max(max_drawdown, dd)

        trade_count = len([t for t in trades if t["action"] in ("BUY", "SELL")])
        pnl = equity_curve[-1]
        ok = max_drawdown <= max_dd and trade_count >= min_trades
        reason = "passed" if ok else f"drawdown={max_drawdown:.2f}% trades={trade_count}"

        return BacktestResult(ok, total_bars, trade_count, pnl, max_drawdown, trades, reason)
