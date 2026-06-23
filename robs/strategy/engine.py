"""Orchestrate features, rules, and signals."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from robs.strategy.features import PairSpreadFeatures, PointBreakoutFeatures
from robs.strategy.rules import Action, Signal, build_rules


@dataclass
class StrategyEngine:
    cfg: dict[str, Any]
    rules: list[Any] = field(default_factory=list)
    pair_features: PairSpreadFeatures | None = None
    point_features: PointBreakoutFeatures | None = None

    def __post_init__(self) -> None:
        if not self.rules:
            self.rules = build_rules(self.cfg)

        rules_cfg = self.cfg.get("rules", {})
        if "pair_spread" in rules_cfg:
            ps = rules_cfg["pair_spread"]
            tickers = ps.get("tickers", ["HK.800000", "HK.03188"])
            self.pair_features = PairSpreadFeatures(
                ticker_a=str(tickers[0]),
                ticker_b=str(tickers[1]),
                window_sec=int(ps.get("window_sec", 60)),
                poll_interval_sec=int(ps.get("poll_interval_sec", self.cfg.get("data", {}).get("poll_interval_sec", 5))),
            )
        if "point_breakout" in rules_cfg:
            pb = rules_cfg["point_breakout"]
            self.point_features = PointBreakoutFeatures(
                symbol=str(pb.get("symbol", "HK.MHImain")),
                threshold_pts=float(pb.get("threshold_pts", 20)),
            )

    def update_from_prices(self, prices: dict[str, float]) -> list[Signal]:
        feature_map: dict[str, dict[str, Any]] = {}

        if self.pair_features is not None:
            feature_map["pair_spread"] = self.pair_features.update(prices)

        if self.point_features is not None:
            symbol = self.point_features.symbol
            if symbol in prices:
                feature_map["point_breakout"] = self.point_features.update(prices[symbol])

        signals: list[Signal] = []
        for rule in self.rules:
            name = getattr(rule, "name", rule.__class__.__name__)
            feats = feature_map.get(name, {})
            signal = rule.evaluate(feats)
            if signal is not None:
                signals.append(signal)
        return signals

    def update_from_snapshot_frame(self, frame: pd.DataFrame) -> list[Signal]:
        prices = {str(row["code"]): float(row["last_price"]) for _, row in frame.iterrows()}
        return self.update_from_prices(prices)

    def pick_action(self, signals: list[Signal]) -> Signal | None:
        priority = [Action.FLAT, Action.SELL, Action.BUY, Action.HOLD]
        for action in priority:
            for signal in signals:
                if signal.action == action and action != Action.HOLD:
                    return signal
        return signals[0] if signals else None
