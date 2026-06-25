"""Thin wrappers around Futu OpenD quote and trade contexts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from futu import OpenQuoteContext, OpenSecTradeContext, RET_OK

from robs.config import trd_env_from_config


@dataclass
class FutuEndpoints:
    host: str = "127.0.0.1"
    port: int = 11111


def endpoints_from_config(cfg: dict[str, Any]) -> FutuEndpoints:
    futu_cfg = cfg.get("futu", {})
    return FutuEndpoints(
        host=str(futu_cfg.get("host", "127.0.0.1")),
        port=int(futu_cfg.get("port", 11111)),
    )


class QuoteClient:
    def __init__(self, endpoints: FutuEndpoints) -> None:
        self._ctx = OpenQuoteContext(host=endpoints.host, port=endpoints.port)

    def close(self) -> None:
        self._ctx.close()

    def __enter__(self) -> QuoteClient:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def global_state(self) -> tuple[bool, Any]:
        ret, data = self._ctx.get_global_state()
        return ret == RET_OK, data

    def subscribe_quote(self, tickers: list[str]) -> tuple[bool, Any]:
        """Subscribe live quote stream — required before get_stock_quote for futures."""
        from futu import SubType

        ret, data = self._ctx.subscribe(tickers, [SubType.QUOTE])
        return ret == RET_OK, data

    def snapshot(self, tickers: list[str]) -> tuple[bool, Any]:
        ret, data = self._ctx.get_market_snapshot(tickers)
        return ret == RET_OK, data

    def quote(self, tickers: list[str]) -> tuple[bool, Any]:
        ret, data = self._ctx.get_stock_quote(tickers)
        return ret == RET_OK, data

    def quote_for_trade(self, symbol: str, row: Any | None = None) -> tuple[bool, Any]:
        """Quote row with bid/ask filled from snapshot when the quote feed omits them."""
        if row is None:
            ret, data = self.quote([symbol])
            if not ret or data is None or len(data) == 0:
                return False, None
            row = data.iloc[0]
        else:
            row = row.copy() if hasattr(row, "copy") else row
        needs_book = (
            row.get("ask_price") is None
            or row.get("ask_price") != row.get("ask_price")
            or float(row.get("ask_price") or 0) <= 0
            or row.get("bid_price") is None
            or row.get("bid_price") != row.get("bid_price")
            or float(row.get("bid_price") or 0) <= 0
        )
        if not needs_book:
            return True, row
        ok_snap, snap = self.snapshot([symbol])
        if ok_snap and snap is not None and len(snap):
            snap_row = snap.iloc[0]
            row = row.copy()
            for col in ("ask_price", "bid_price"):
                val = snap_row.get(col)
                if val is not None and val == val and float(val) > 0:
                    row[col] = val
        return True, row

    def daily_ma(self, symbol: str, period: int = 5) -> tuple[bool, float | None]:
        from futu import AuType, KLType, RET_OK, SubType

        self._ctx.subscribe([symbol], [SubType.K_DAY])
        ret, data = self._ctx.get_cur_kline(symbol, num=period, ktype=KLType.K_DAY, autype=AuType.NONE)
        if ret != RET_OK or data is None or len(data) == 0:
            return False, None
        closes = data["close"].astype(float)
        if len(closes) < period:
            return False, None
        return True, float(closes.tail(period).mean())

    def resolve_front_contract(self, symbol: str) -> tuple[bool, str | None]:
        """Map HK.MHImain to HKEX spot month (local calendar; no OpenD per call)."""
        sym = str(symbol).upper()
        if sym != "HK.MHIMAIN":
            return True, symbol

        from robs.execution.contract_rollover import HKEXMHISpot

        spot = HKEXMHISpot.resolve()
        if spot is None:
            return False, None
        return True, spot.front

    def seed_mhi_ltd_overrides(self, codes: list[str]) -> dict[str, Any]:
        """One-shot OpenD fetch of last-trade dates for calendar refinement at startup."""
        from robs.execution.contract_rollover import parse_last_trade_date

        overrides: dict[str, Any] = {}
        for code in codes:
            ltd = self.contract_last_trade_time(code)
            if not ltd:
                continue
            day = parse_last_trade_date(ltd)
            if day is not None:
                overrides[code] = day
        return overrides

    def contract_last_trade_time(self, code: str) -> str | None:
        from futu import RET_OK

        from robs.execution.contract_rollover import normalize_last_trade_time

        ret, data = self._ctx.get_future_info(code)
        if ret != RET_OK or data is None or len(data) == 0:
            return None
        val = data.iloc[0].get("last_trade_time")
        return normalize_last_trade_time(str(val) if val is not None else None)


class TradeClient:
    def __init__(self, endpoints: FutuEndpoints, *, futures: bool = False) -> None:
        if futures:
            from futu import OpenFutureTradeContext

            self._ctx = OpenFutureTradeContext(host=endpoints.host, port=endpoints.port)
        else:
            self._ctx = OpenSecTradeContext(host=endpoints.host, port=endpoints.port)
        self._futures = futures

    def close(self) -> None:
        self._ctx.close()

    def __enter__(self) -> TradeClient:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def fetch_account_equity(self, cfg: dict[str, Any]) -> float | None:
        """Broker total_assets for daily loss kill switch."""
        trd_env = trd_env_from_config(cfg)
        ret, data = self._ctx.accinfo_query(trd_env=trd_env)
        if ret != RET_OK or data is None or len(data) == 0:
            return None
        total = data.iloc[0].get("total_assets")
        if total is None or str(total).strip().upper() in ("", "N/A", "NA", "NONE"):
            return None
        try:
            val = float(total)
        except (TypeError, ValueError):
            return None
        return val if val > 0 else None

    def place_market_order(
        self,
        code: str,
        qty: int,
        side: str,
        *,
        trd_env=None,
    ) -> dict[str, Any]:
        from futu import OrderType, RET_OK, TrdEnv, TrdSide

        if trd_env is None:
            trd_env = TrdEnv.SIMULATE

        trd_side = TrdSide.BUY if side.upper() == "BUY" else TrdSide.SELL
        ret, data = self._ctx.place_order(
            price=0,
            qty=qty,
            code=code,
            trd_side=trd_side,
            order_type=OrderType.MARKET,
            trd_env=trd_env,
        )
        ok = ret == RET_OK
        result: dict[str, Any] = {
            "ok": ok,
            "code": code,
            "qty": qty,
            "side": side,
            "trd_env": TrdEnv.to_string2(trd_env) if hasattr(TrdEnv, "to_string2") else str(trd_env),
        }
        if not ok:
            result["status"] = "rejected"
            result["error"] = str(data)
            return result

        if data is not None and len(data):
            row = data.iloc[0]
            order_status = str(row.get("order_status", ""))
            dealt_qty = float(row.get("dealt_qty") or 0)
            result.update(
                {
                    "status": order_status or "submitted",
                    "order_id": row.get("order_id"),
                    "order_status": order_status,
                    "dealt_qty": dealt_qty,
                    "dealt_avg_price": row.get("dealt_avg_price"),
                    "contract": row.get("code"),
                    "data": data,
                }
            )
            result["filled"] = "FILLED" in order_status.upper() or dealt_qty >= qty
        else:
            result["status"] = "submitted"
            result["filled"] = False
        return result
