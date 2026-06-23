"""Experience-based trading rules (configurable)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class Action(str, Enum):
    HOLD = "HOLD"
    BUY = "BUY"
    SELL = "SELL"
    FLAT = "FLAT"


@dataclass
class Signal:
    rule: str
    action: Action
    trade_ticker: str
    reason: str
    metadata: dict[str, Any]


@dataclass
class PairSpreadRule:
    name: str = "pair_spread"
    enabled: bool = True
    ticker_a: str = "HK.800000"
    ticker_b: str = "HK.03188"
    spread_entry_pct: float = 0.15
    spread_exit_pct: float = 0.05
    require_same_sign: bool = False
    trade_ticker: str = "HK.03188"
    _in_position: bool = False
    _side: Action = Action.HOLD

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> PairSpreadRule:
        tickers = cfg.get("tickers", ["HK.800000", "HK.03188"])
        return cls(
            enabled=bool(cfg.get("enabled", True)),
            ticker_a=str(tickers[0]),
            ticker_b=str(tickers[1]),
            spread_entry_pct=float(cfg.get("spread_entry_pct", 0.15)),
            spread_exit_pct=float(cfg.get("spread_exit_pct", 0.05)),
            require_same_sign=bool(cfg.get("require_same_sign", False)),
            trade_ticker=str(cfg.get("trade_ticker", tickers[1])),
        )

    def evaluate(self, features: dict[str, Any]) -> Signal | None:
        if not self.enabled:
            return None

        spread = features.get("spread_pct")
        if spread is None:
            return None

        same_sign = features.get("same_sign")
        if self.require_same_sign and same_sign is False:
            return None

        ret_a = features.get("ret_a")
        ret_b = features.get("ret_b")

        if not self._in_position:
            if spread >= self.spread_entry_pct:
                self._in_position = True
                self._side = Action.BUY
                return Signal(
                    self.name,
                    Action.BUY,
                    self.trade_ticker,
                    f"spread {spread:.4f}% >= entry {self.spread_entry_pct}",
                    {"spread_pct": spread, "ret_a": ret_a, "ret_b": ret_b},
                )
            if spread <= -self.spread_entry_pct:
                self._in_position = True
                self._side = Action.SELL
                return Signal(
                    self.name,
                    Action.SELL,
                    self.trade_ticker,
                    f"spread {spread:.4f}% <= -entry {-self.spread_entry_pct}",
                    {"spread_pct": spread, "ret_a": ret_a, "ret_b": ret_b},
                )
            return Signal(self.name, Action.HOLD, self.trade_ticker, "waiting", {"spread_pct": spread})

        if abs(spread) <= self.spread_exit_pct:
            side = Action.FLAT
            self._in_position = False
            self._side = Action.HOLD
            return Signal(
                self.name,
                side,
                self.trade_ticker,
                f"spread {spread:.4f}% within exit band",
                {"spread_pct": spread},
            )

        return Signal(self.name, Action.HOLD, self.trade_ticker, "in position", {"spread_pct": spread})


@dataclass
class PointBreakoutRule:
    name: str = "point_breakout"
    enabled: bool = True
    symbol: str = "HK.MHImain"
    threshold_pts: float = 20.0
    exit_threshold_pts: float = 10.0
    trade_ticker: str = "HK.MHImain"
    _in_position: bool = False
    _side: Action = Action.HOLD
    _entry_price: float | None = None

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> PointBreakoutRule:
        symbol = str(cfg.get("symbol", "HK.MHImain"))
        return cls(
            enabled=bool(cfg.get("enabled", True)),
            symbol=symbol,
            threshold_pts=float(cfg.get("threshold_pts", 20)),
            exit_threshold_pts=float(cfg.get("exit_threshold_pts", 10)),
            trade_ticker=str(cfg.get("trade_ticker", symbol)),
        )

    def evaluate(self, features: dict[str, Any]) -> Signal | None:
        if not self.enabled:
            return None

        if not features.get("breakout") and not self._in_position:
            return Signal(self.name, Action.HOLD, self.trade_ticker, "no breakout", features)

        direction = features.get("direction")
        price = features.get("price")

        if not self._in_position and features.get("breakout"):
            self._in_position = True
            self._entry_price = float(price) if price is not None else None
            if direction == "LONG":
                self._side = Action.BUY
                return Signal(self.name, Action.BUY, self.trade_ticker, "point breakout up", features)
            if direction == "SHORT":
                self._side = Action.SELL
                return Signal(self.name, Action.SELL, self.trade_ticker, "point breakout down", features)

        if self._in_position and self._entry_price is not None and price is not None:
            adverse = self._entry_price - float(price)
            if self._side == Action.BUY:
                adverse = float(price) - self._entry_price
            if adverse <= -self.exit_threshold_pts:
                self._in_position = False
                self._side = Action.HOLD
                self._entry_price = None
                return Signal(self.name, Action.FLAT, self.trade_ticker, "exit threshold hit", features)

        return Signal(self.name, Action.HOLD, self.trade_ticker, "holding breakout trade", features)


def build_rules(cfg: dict[str, Any]) -> list[Any]:
    rules_cfg = cfg.get("rules", {})
    rules: list[Any] = []
    if "pair_spread" in rules_cfg:
        rules.append(PairSpreadRule.from_config(rules_cfg["pair_spread"]))
    if "point_breakout" in rules_cfg:
        rules.append(PointBreakoutRule.from_config(rules_cfg["point_breakout"]))
    return rules
