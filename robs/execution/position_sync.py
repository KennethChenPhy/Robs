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


_NON_NUMERIC = frozenset({"N/A", "NA", "NONE", "UNKNOWN", ""})


def _parse_float(val: Any) -> float | None:
    """Parse Futu field; None for N/A, blank, NaN, or non-numeric."""
    if val is None:
        return None
    if isinstance(val, str):
        if val.strip().upper() in _NON_NUMERIC:
            return None
    try:
        out = float(val)
    except (TypeError, ValueError):
        return None
    if out != out:
        return None
    return out


def _pick_cost(row: Any) -> float | None:
    for col in ("cost_price", "average_cost", "diluted_cost"):
        val = _parse_float(row.get(col))
        if val is not None and val > 0:
            return val
    return None


def _qty_to_signed(qty: float, position_side: str) -> int:
    if qty == 0:
        return 0
    n = int(abs(qty))
    side = str(position_side or "").upper().strip()
    if "SHORT" in side or side in ("S", "SELL"):
        return -n
    if qty < 0:
        return -n
    if "LONG" in side or side in ("B", "BUY"):
        return n
    return n


def _infer_signed_when_side_unknown(row: Any, n: int) -> int:
    """Best-effort short vs long when Futu reports position_side N/A."""
    if n == 0:
        return 0
    qty_f = _parse_float(row.get("qty")) or 0.0
    can_sell = row.get("can_sell_qty")
    if can_sell is not None:
        cs = _parse_float(can_sell)
        if cs is not None and qty_f > 0:
            if cs == 0:
                return -n
            if cs >= qty_f:
                return n
    entry = _pick_cost(row)
    nominal = _parse_float(row.get("nominal_price"))
    for pl_key in ("pl_val", "unrealized_pl"):
        pl_val = _parse_float(row.get(pl_key))
        if entry is not None and nominal is not None and pl_val is not None:
            entry_f, nom_f, pl_f = entry, nominal, pl_val
            if pl_f < 0 and nom_f > entry_f:
                return -n
            if pl_f < 0 and nom_f < entry_f:
                return n
            if pl_f > 0 and nom_f > entry_f:
                return n
            if pl_f > 0 and nom_f < entry_f:
                return -n
            long_pl = nom_f - entry_f
            short_pl = entry_f - nom_f
            if abs(pl_f - short_pl) < abs(pl_f - long_pl):
                return -n
            return n
    return n


def _signed_contracts_from_row(row: Any) -> int:
    """Signed contracts from a Futu position row (handles N/A position_side)."""
    qty = _parse_float(row.get("qty")) or 0.0
    side = str(row.get("position_side", "") or "")
    side_up = side.upper().strip()
    if side_up not in ("", "N/A", "NONE", "UNKNOWN"):
        return _qty_to_signed(qty, side)
    if qty < 0:
        return int(qty)
    n = int(abs(qty))
    if n == 0:
        return 0
    return _infer_signed_when_side_unknown(row, n)


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
        total_signed += _signed_contracts_from_row(row)
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


def _group_mhi_row_lists(data: Any, default_code: str) -> dict[str, list[Any]]:
    """Group broker rows by contract code."""
    grouped: dict[str, list[Any]] = {}
    for _, row in data.iterrows():
        code = str(row.get("code", default_code)).upper()
        if not is_hk_mhi_product_code(code):
            continue
        grouped.setdefault(code, []).append(row)
    return grouped


def _group_mhi_rows_by_code(data: Any, default_code: str) -> dict[str, Any]:
    """Group broker rows by contract code (one merged row per product)."""
    out: dict[str, Any] = {}
    for code, row_list in _group_mhi_row_lists(data, default_code).items():
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
    signed = _signed_contracts_from_row(row)
    qty = _parse_float(row.get("qty")) or 0.0
    entry = _pick_cost(row)
    if quote_price is not None:
        current = quote_price
    else:
        current = _parse_float(row.get("nominal_price"))

    pnl_points = 0.0
    if entry is not None and current is not None and signed != 0:
        if signed > 0:
            pnl_points = current - entry
        else:
            pnl_points = entry - current

    pnl_val = _parse_float(row.get("pl_val"))

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
    for code, row_list in _group_mhi_row_lists(data, symbol).items():
        px = (quote_prices or {}).get(code)
        if px is None and quote_prices and not is_named_mhi_contract(code):
            px = quote_prices.get(symbol)
        if len(row_list) > 1:
            broker = fetch_broker_position_for_code(trade, code, cfg, quote_price=px)
        else:
            broker = _broker_position_from_row(row_list[0], code, px)
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

    if new_pos != 0 and old_pos != new_pos:
        if old_pos == 0 or sign_changed:
            strategy.on_broker_position_opened()
        strategy.pnl_baseline.reset_on_new_entry()

    if new_pos == 0:
        strategy.rearm_entry_if_flat(position_book)

    return old_pos != new_pos
