"""Sync unit position and P/L baseline from Futu at startup and periodic refresh."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from futu import RET_OK, TrdEnv

from robs.config import trd_env_name

from robs.data.futu_client import TradeClient


@dataclass
class BrokerPosition:
    code: str
    contracts: int  # signed: +n long, -n short, 0 flat
    qty: float
    entry_price: float | None
    current_price: float | None
    pnl_points: float
    pnl_val: float | None
    stock_name: str = ""

    @property
    def position(self) -> int:
        if self.contracts > 0:
            return 1
        if self.contracts < 0:
            return -1
        return 0

    @property
    def label(self) -> str:
        if self.contracts > 0:
            return f"long (+{self.contracts})"
        if self.contracts < 0:
            return f"short ({self.contracts})"
        return "flat (0)"


def _trd_env(cfg: dict[str, Any]) -> TrdEnv:
    return TrdEnv.REAL if trd_env_name(cfg) == "REAL" else TrdEnv.SIMULATE


def _pick_cost(row: Any) -> float | None:
    for col in ("cost_price", "average_cost", "diluted_cost"):
        val = row.get(col)
        if val is not None and val == val and float(val) > 0:
            return float(val)
    return None


def _qty_to_signed(qty: float, position_side: str) -> int:
    if qty == 0:
        return 0
    n = int(abs(qty))
    side = str(position_side).upper()
    if "SHORT" in side or qty < 0:
        return -n
    return n


def _is_hk_mhi(symbol: str) -> bool:
    return str(symbol).upper().startswith("HK.MHI")


def _match_position_rows(data: Any, symbol: str) -> Any:
    """Resolve broker rows for HK.MHI futures (main or named month)."""
    if data is None or len(data) == 0:
        return data
    codes = data["code"].astype(str)
    if symbol == "HK.MHImain":
        mhi = data[codes.str.startswith("HK.MHI")]
        return mhi if len(mhi) else data
    exact = data[codes == symbol]
    return exact if len(exact) else data


def fetch_broker_position(
    trade: TradeClient,
    symbol: str,
    cfg: dict[str, Any],
    quote_price: float | None = None,
) -> BrokerPosition:
    trd_env = _trd_env(cfg)
    ret, data = trade._ctx.position_list_query(code=symbol, trd_env=trd_env)

    if ret == RET_OK and (data is None or len(data) == 0) and _is_hk_mhi(symbol):
        ret, data = trade._ctx.position_list_query(trd_env=trd_env)
        if ret == RET_OK and data is not None and len(data):
            data = _match_position_rows(data, symbol)

    if ret != RET_OK or data is None or len(data) == 0:
        price = quote_price
        return BrokerPosition(
            code=symbol,
            contracts=0,
            qty=0,
            entry_price=None,
            current_price=price,
            pnl_points=0.0,
            pnl_val=None,
        )

    row = data.iloc[0]
    actual_code = str(row.get("code", symbol))
    qty = float(row.get("qty", 0))
    signed = _qty_to_signed(qty, str(row.get("position_side", "")))
    entry = _pick_cost(row)
    # Prefer the live quote over broker nominal_price — it updates every poll.
    if quote_price is not None:
        current = quote_price
    else:
        nominal = row.get("nominal_price")
        current = float(nominal) if nominal is not None and nominal == nominal else None

    pnl_points = 0.0
    if entry is not None and current is not None and signed != 0:
        if signed > 0:
            pnl_points = current - entry
        else:
            pnl_points = entry - current

    pl_val = row.get("pl_val")
    pnl_val = float(pl_val) if pl_val is not None and pl_val == pl_val else None

    return BrokerPosition(
        code=actual_code,
        contracts=signed,
        qty=qty,
        entry_price=entry,
        current_price=current,
        pnl_points=pnl_points,
        pnl_val=pnl_val,
        stock_name=str(row.get("stock_name", "")),
    )


def apply_broker_position(
    broker: BrokerPosition,
    position_book: Any,
    strategy: Any,
    *,
    bootstrap: bool = True,
) -> None:
    old_pos = position_book.contracts
    position_book.contracts = broker.contracts
    if broker.contracts == 0:
        position_book.reset_after_flat()
        strategy.entry_price = None
        strategy.position_opened_at = None
        if bootstrap:
            strategy.pnl_baseline.reset_on_new_entry()
        return

    new_entry = broker.entry_price
    if new_entry is None and broker.current_price is not None:
        new_entry = (
            broker.current_price - broker.pnl_points
            if broker.contracts > 0
            else broker.current_price + broker.pnl_points
        )

    if new_entry is not None and (
        bootstrap or strategy.entry_price is None or old_pos != broker.contracts
    ):
        strategy.entry_price = new_entry

    if bootstrap and strategy.entry_price is not None and broker.current_price is not None:
        strategy.pnl_baseline.bootstrap(
            strategy.entry_price, broker.current_price, broker.position
        )


def live_pnl_points(
    strategy: Any,
    position_book: Any,
    live_price: float,
) -> float | None:
    """P/L in points from strategy entry and the current quote (same source as exits)."""
    if position_book.contracts == 0 or strategy.entry_price is None:
        return None
    return strategy.pnl_baseline.pnl_points(
        strategy.entry_price, live_price, position_book.position
    )


def refresh_broker_position(
    broker: BrokerPosition,
    position_book: Any,
    strategy: Any,
    risk: Any,
    *,
    cfg: dict | None = None,
) -> bool:
    """Periodic broker sync: position state + min-hold / cooldown on manual open/close."""
    old_pos = position_book.contracts
    new_pos = broker.contracts
    entry_before = strategy.entry_price
    px = broker.current_price

    sign_changed = (
        old_pos != 0
        and new_pos != 0
        and ((old_pos > 0 and new_pos < 0) or (old_pos < 0 and new_pos > 0))
    )

    if old_pos != 0 and (new_pos == 0 or sign_changed):

        def _handle_manual_close() -> None:
            if entry_before is not None and px is not None:
                sign = 1 if old_pos > 0 else -1
                strategy.pnl_baseline.realize_on_close(entry_before, px, sign)
            exit_px = px if px is not None else (entry_before if entry_before is not None else 0.0)
            strategy.on_broker_position_closed(exit_px)

        _handle_manual_close()

    apply_broker_position(broker, position_book, strategy, bootstrap=False)
    risk.position_shares = broker.contracts

    if new_pos != 0 and (old_pos == 0 or sign_changed):
        strategy.on_broker_position_opened()

    if new_pos == 0:
        strategy.rearm_entry_if_flat(position_book)

    return old_pos != new_pos
