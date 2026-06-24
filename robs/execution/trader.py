"""Order routing via Futu (paper or live)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from robs.config import trd_env_from_config, trd_env_name
from robs.data.futu_client import TradeClient
from robs.execution.risk import RiskManager
from robs.strategy.rules import Action, Signal


@dataclass
class Trader:
    cfg: dict[str, Any]
    trade_client: TradeClient | None
    risk: RiskManager
    default_qty: int = 1

    @property
    def paper(self) -> bool:
        return trd_env_name(self.cfg) != "REAL"

    def execute(self, signal: Signal, qty: int | None = None) -> dict[str, Any]:
        if signal.action == Action.HOLD:
            return {"status": "skipped", "reason": "HOLD"}

        order_qty = qty or self.default_qty
        side = signal.action.value
        if signal.action == Action.FLAT:
            if self.risk.position_shares > 0:
                side = "SELL"
                order_qty = abs(self.risk.position_shares)
            elif self.risk.position_shares < 0:
                side = "BUY"
                order_qty = abs(self.risk.position_shares)
            else:
                return {"status": "skipped", "reason": "already flat"}

        approved, reason = self.risk.approve_order(side, order_qty)
        if not approved:
            return {"status": "rejected", "reason": reason}

        if self.trade_client is None:
            self.risk.on_fill(side, order_qty)
            return {
                "status": "simulated_no_client",
                "paper": True,
                "code": signal.trade_ticker,
                "side": side,
                "qty": order_qty,
                "rule": signal.rule,
            }

        result = self.trade_client.place_market_order(
            code=signal.trade_ticker,
            qty=order_qty,
            side=side,
            trd_env=trd_env_from_config(self.cfg),
        )
        if result.get("ok") and result.get("filled", True):
            self.risk.on_fill(side, order_qty)
        result["rule"] = signal.rule
        result["signal_reason"] = signal.reason
        return result
