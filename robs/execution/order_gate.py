"""Block new orders until the previous one is filled or terminal."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from futu import RET_OK

from robs.config import trd_env_from_config
from robs.data.futu_client import TradeClient
from robs.execution.contract_rollover import is_hk_mhi_product_code
from robs.execution.position_sync import fetch_broker_position, fetch_broker_position_for_code
from robs.strategy.rules import Action


def _terminal_failure(status: str) -> bool:
    upper = status.upper()
    return any(
        tag in upper
        for tag in ("CANCEL", "FAILED", "REJECT", "DISABLED", "DELETED", "INVALID")
    )


def _is_filled(status: str, dealt_qty: float, qty: float) -> bool:
    upper = status.upper()
    if "FILLED" in upper and "CANCEL" not in upper:
        return True
    return dealt_qty >= qty > 0


@dataclass
class OrderGate:
    """At most one live order; poll broker/order list until it completes."""

    pending: bool = False
    order_id: str | None = None
    side: str | None = None
    signal_action: Action | None = None
    qty: float = 1.0
    submitted_at: float | None = None
    order_code: str | None = None
    _logged_waiting: bool = False
    logged_submit: bool = False

    def mark_submitted(
        self,
        *,
        order_id: Any,
        side: str,
        signal_action: Action,
        qty: float = 1.0,
        order_code: str | None = None,
    ) -> None:
        self.pending = True
        self.order_id = str(order_id) if order_id is not None else None
        self.side = side.upper()
        self.signal_action = signal_action
        self.qty = qty
        self.order_code = order_code
        self.submitted_at = time.monotonic()
        self._logged_waiting = False
        self.logged_submit = False

    def clear(self) -> None:
        self.pending = False
        self.order_id = None
        self.side = None
        self.signal_action = None
        self.qty = 1.0
        self.order_code = None
        self.submitted_at = None
        self._logged_waiting = False
        self.logged_submit = False

    def is_close_intent(self, local_contracts: int) -> bool:
        if self.signal_action == Action.FLAT:
            return local_contracts != 0
        if local_contracts > 0 and self.side == "SELL":
            return True
        if local_contracts < 0 and self.side == "BUY":
            return True
        return False

    def waiting_message(self) -> str:
        oid = self.order_id or "?"
        return f"order pending ({self.side} id={oid})"

    def try_resolve(
        self,
        trade: TradeClient,
        cfg: dict,
        symbol: str,
        position: Any,
        strategy: Any,
        risk: Any,
        price: float,
    ) -> str | None:
        """
        Poll order/position state. Returns:
          'filled' — position synced, gate cleared
          'failed' — terminal failure, gate cleared
          None — still in flight
        """
        if not self.pending:
            return None

        trd_env = trd_env_from_config(cfg)

        if self.order_id:
            ret, orders = trade._ctx.order_list_query(
                order_id=self.order_id,
                trd_env=trd_env,
            )
            if ret == RET_OK and orders is not None and len(orders):
                row = orders.iloc[0]
                status = str(row.get("order_status", ""))
                dealt_qty = float(row.get("dealt_qty") or 0)
                if _is_filled(status, dealt_qty, self.qty):
                    self._sync_broker_position(trade, cfg, symbol, position, strategy, risk, price)
                    return "filled"
                if _terminal_failure(status):
                    self.clear()
                    strategy.set_order_pending(False)
                    return "failed"

        broker = self._fetch_broker_for_gate(trade, cfg, symbol, price)
        if self._broker_matches_intent(broker.contracts, position.contracts):
            self._sync_broker_position(trade, cfg, symbol, position, strategy, risk, price)
            return "filled"

        if not self._logged_waiting:
            self._logged_waiting = True
        return None

    def _fetch_broker_for_gate(
        self,
        trade: TradeClient,
        cfg: dict,
        symbol: str,
        price: float,
    ) -> Any:
        if self.order_code and is_hk_mhi_product_code(self.order_code):
            return fetch_broker_position_for_code(trade, self.order_code, cfg, quote_price=price)
        return fetch_broker_position(trade, symbol, cfg, quote_price=price)

    def _broker_matches_intent(self, broker_contracts: int, local_contracts: int) -> bool:
        if self.signal_action in (Action.BUY, Action.SELL) and local_contracts == 0:
            if self.side == "BUY":
                return broker_contracts >= self.qty
            if self.side == "SELL":
                return broker_contracts <= -self.qty
        if self.signal_action == Action.FLAT or (
            (self.side == "SELL" and local_contracts > 0)
            or (self.side == "BUY" and local_contracts < 0)
        ):
            return broker_contracts == 0
        return False

    def _sync_broker_position(
        self,
        trade: TradeClient,
        cfg: dict,
        symbol: str,
        position: Any,
        strategy: Any,
        risk: Any,
        price: float,
    ) -> None:
        broker = self._fetch_broker_for_gate(trade, cfg, symbol, price)
        position.contracts = broker.contracts
        if broker.contracts == 0:
            position.reset_after_flat()
        risk.position_shares = broker.contracts
