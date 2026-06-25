"""Sync unit position and P/L baseline from Futu at startup and periodic refresh."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd
from futu import RET_OK, TrdEnv

from robs.config import trd_env_name

from robs.data.futu_client import TradeClient
from robs.execution.contract_rollover import is_hk_mhi_product_code, is_named_mhi_contract


@dataclass
class BrokerPosition:
    code: str
    contracts: int  # signed: +n long, -n short, 0 flat
    qty: float
    entry_price: float | None
    current_price: float | None
    pnl_points: float
    pnl_val: float | None

    @property
    def position(self) -> int:
        if self.contracts > 0:
            return 1
        if self.contracts < 0:
            return -1
        return 0


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


def _empty_mhi_position(symbol: str, quote_price: float | None = None) -> BrokerPosition:
    return BrokerPosition(
        code=symbol,
        contracts=0,
        qty=0,
        entry_price=None,
        current_price=quote_price,
        pnl_points=0.0,
        pnl_val=None,
    )


def _match_position_rows(data: Any, symbol: str) -> Any:
    """Keep only HK.MHI* rows; never fall back to HTI/HSI/other products."""
    if data is None or len(data) == 0:
        return data
    codes = data["code"].astype(str)
    upper = codes.str.upper()
    mhi = data[upper.str.startswith("HK.MHI")]
    sym = str(symbol).upper()
    if sym == "HK.MHIMAIN":
        return mhi
    exact = data[upper == sym]
    if len(exact):
        return exact
    return mhi.iloc[0:0]


def _row_for_contract_code(data: Any, code: str) -> Any | None:
    """Pick the row for one exact contract code (not iloc[0] when many products)."""
    if data is None or len(data) == 0:
        return None
    want = str(code).upper()
    if "code" not in data.columns:
        return data.iloc[0] if len(data) == 1 else None
    codes = data["code"].astype(str).str.upper()
    matched = data[codes == want]
    if len(matched) == 0:
        return None
    if len(matched) == 1:
        return matched.iloc[0]
    return _aggregate_position_rows(matched, want)


def _aggregate_position_rows(rows: Any, code: str) -> Any:
    """Sum qty when the broker returns multiple rows for the same contract."""
    total_signed = 0.0
    first = rows.iloc[0]
    for _, row in rows.iterrows():
        total_signed += _qty_to_signed(
            float(row.get("qty", 0)),
            str(row.get("position_side", "")),
        )
    merged = first.copy()
    merged["code"] = code
    if total_signed == 0:
        merged["qty"] = 0.0
        merged["position_side"] = "NONE"
    elif total_signed > 0:
        merged["qty"] = float(total_signed)
        merged["position_side"] = "LONG"
    else:
        merged["qty"] = float(abs(total_signed))
        merged["position_side"] = "SHORT"
    return merged


def _fetch_broker_row(
    trade: TradeClient,
    code: str,
    cfg: dict[str, Any],
) -> Any | None:
    """Broker position row for one HK.MHI contract; falls back to full account list."""
    if not is_hk_mhi_product_code(code):
        return None
    trd_env = _trd_env(cfg)
    ret, data = trade._ctx.position_list_query(code=code, trd_env=trd_env)
    if ret == RET_OK and data is not None and len(data) > 0:
        row = _row_for_contract_code(data, code)
        if row is not None:
            return row
    ret, data = trade._ctx.position_list_query(trd_env=trd_env)
    if ret != RET_OK or data is None or len(data) == 0:
        return None
    filtered = _match_position_rows(data, code)
    return _row_for_contract_code(filtered, code)


def _group_mhi_rows_by_code(data: Any, default_code: str) -> dict[str, Any]:
    """Group broker rows by contract code (one merged row per product)."""
    grouped: dict[str, list[Any]] = {}
    for _, row in data.iterrows():
        code = str(row.get("code", default_code)).upper()
        if not is_hk_mhi_product_code(code):
            continue
        grouped.setdefault(code, []).append(row)
    out: dict[str, Any] = {}
    for code, row_list in grouped.items():
        if len(row_list) == 1:
            out[code] = row_list[0]
        else:
            out[code] = _aggregate_position_rows(pd.DataFrame(row_list), code)
    return out


def _broker_position_from_row(
    row: Any,
    symbol: str,
    quote_price: float | None,
) -> BrokerPosition:
    actual_code = str(row.get("code", symbol))
    qty = float(row.get("qty", 0))
    signed = _qty_to_signed(qty, str(row.get("position_side", "")))
    entry = _pick_cost(row)
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
    )


def fetch_broker_mhi_legs(
    trade: TradeClient,
    symbol: str,
    cfg: dict[str, Any],
    *,
    quote_prices: dict[str, float] | None = None,
) -> list[BrokerPosition]:
    """All open HK.MHI month positions (separate row per contract code)."""
    if str(symbol).upper() != "HK.MHIMAIN":
        px = (quote_prices or {}).get(symbol)
        one = fetch_broker_position(trade, symbol, cfg, quote_price=px)
        return [one] if one.contracts != 0 else []

    trd_env = _trd_env(cfg)
    ret, data = trade._ctx.position_list_query(code=symbol, trd_env=trd_env)
    if ret == RET_OK and (data is None or len(data) == 0):
        ret, data = trade._ctx.position_list_query(trd_env=trd_env)
    if ret != RET_OK or data is None or len(data) == 0:
        return []

    data = _match_position_rows(data, symbol)
    legs: list[BrokerPosition] = []
    for code, row in _group_mhi_rows_by_code(data, symbol).items():
        px = (quote_prices or {}).get(code)
        if px is None and quote_prices:
            px = quote_prices.get(symbol)
        broker = _broker_position_from_row(row, code, px)
        if broker.contracts != 0:
            legs.append(broker)
    return legs


def fetch_broker_position_for_code(
    trade: TradeClient,
    code: str,
    cfg: dict[str, Any],
    quote_price: float | None = None,
) -> BrokerPosition:
    """Broker position for one explicit contract code (e.g. HK.MHI2606)."""
    if not is_hk_mhi_product_code(code):
        return _empty_mhi_position(code, quote_price)
    row = _fetch_broker_row(trade, code, cfg)
    if row is None:
        return BrokerPosition(
            code=code,
            contracts=0,
            qty=0,
            entry_price=None,
            current_price=quote_price,
            pnl_points=0.0,
            pnl_val=None,
        )
    return _broker_position_from_row(row, code, quote_price)


def fetch_broker_position(
    trade: TradeClient,
    symbol: str,
    cfg: dict[str, Any],
    quote_price: float | None = None,
) -> BrokerPosition:
    trd_env = _trd_env(cfg)
    ret, data = trade._ctx.position_list_query(code=symbol, trd_env=trd_env)

    if ret == RET_OK and (data is None or len(data) == 0) and is_hk_mhi_product_code(symbol):
        ret, data = trade._ctx.position_list_query(trd_env=trd_env)
        if ret == RET_OK and data is not None and len(data):
            data = _match_position_rows(data, symbol)

    if ret != RET_OK or data is None or len(data) == 0:
        return _empty_mhi_position(symbol, quote_price)

    sym_upper = str(symbol).upper()
    if is_named_mhi_contract(sym_upper):
        row = _row_for_contract_code(data, sym_upper)
        if row is None:
            row = _fetch_broker_row(trade, sym_upper, cfg)
        if row is None:
            return _empty_mhi_position(symbol, quote_price)
        return _broker_position_from_row(row, sym_upper, quote_price)

    row = data.iloc[0]
    row_code = str(row.get("code", symbol))
    if not is_hk_mhi_product_code(row_code):
        return _empty_mhi_position(symbol, quote_price)
    px = quote_price
    if sym_upper == "HK.MHIMAIN" and len(data) > 1:
        # Legacy single-position callers: prefer front/near month (earliest expiry first).
        from robs.execution.contract_rollover import contract_month_key

        def _sort_key(idx_row: tuple[int, Any]) -> tuple[int, int]:
            code = str(idx_row[1].get("code", symbol))
            return contract_month_key(code) or (9999, 99)

        rows = sorted(enumerate(data.iterrows()), key=lambda t: _sort_key((t[0], t[1][1])))
        row = rows[0][1][1]
    actual_code = str(row.get("code", symbol))
    return _broker_position_from_row(row, actual_code, px)


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
    update_risk: bool = True,
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
    if update_risk:
        risk.position_shares = broker.contracts

    if new_pos != 0 and (old_pos == 0 or sign_changed):
        strategy.on_broker_position_opened()

    if new_pos == 0:
        strategy.rearm_entry_if_flat(position_book)

    return old_pos != new_pos
