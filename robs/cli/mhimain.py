#!/usr/bin/env python3
"""Standalone HK.MHImain trader — max one contract long or short.

Position rules:
  flat (0):  allowed HOLD, BUY (open long), SELL (open short)
  long (+1): allowed HOLD, SELL (close) — no BUY
  short (-1): allowed HOLD, BUY (cover) — no SELL
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from robs.config import load_config, trd_env_from_config, trd_env_name
from robs.data.futu_client import QuoteClient, TradeClient, endpoints_from_config
from robs.execution.alarm import play_panic_alarm
from robs.execution.market_guard import allow_market_order
from robs.execution.order_gate import OrderGate
from robs.execution.position import UnitPositionBook
from robs.execution.position_sync import (
    apply_broker_position,
    fetch_broker_position,
    live_pnl_points,
    refresh_broker_position,
)
from robs.execution.quote_staleness import (
    QuoteFreshness,
    assess_quote_freshness,
    is_new_entry,
    stale_threshold_sec,
)
from robs.execution.risk import RiskManager
from robs.execution.trade_unlock import (
    TradeUnlockSession,
    maybe_create_unlock_session,
    warn_short_trade_unlock_window,
)
from robs.log_config import setup_logging
from robs.strategy.mhimain import MHImainStrategy
from robs.strategy.rules import Action, Signal
from robs.strategy.trend import TrendMode, prompt_trend_mode

LOG = logging.getLogger("robs.mhimain")


@dataclass
class _StaleLogGate:
    """Emit at most one stale warning per (poll_stale, data_stale, event) episode."""

    _last: tuple[bool, bool, str] | None = None

    def should_log(self, fresh: QuoteFreshness, event: str) -> bool:
        if not fresh.block_entries:
            self._last = None
            return False
        key = (fresh.poll_stale, fresh.data_stale, event)
        if key == self._last:
            return False
        self._last = key
        return True


_stale_log_gate = _StaleLogGate()


def _log_quote_stale(fresh: QuoteFreshness, *, event: str = "quote_stale") -> None:
    if not _stale_log_gate.should_log(fresh, event):
        return
    LOG.warning(
        "quote stale",
        extra={
            "event": event,
            "poll_stale": fresh.poll_stale,
            "data_stale": fresh.data_stale,
            "data_age_sec": fresh.data_age_sec,
            "threshold_sec": fresh.threshold_sec,
            "data_time": fresh.data_time_raw,
        },
    )


def _alert_panic_pause(strategy: MHImainStrategy, price: float) -> None:
    guard = strategy.panic_guard
    move = guard.move_pts
    window = guard.window_sec
    wait = guard.wait_min
    LOG.critical(
        "panic pause",
        extra={
            "event": "panic_pause",
            "price": price,
            "move_pts": move,
            "window_sec": window,
            "wait_min": wait,
        },
    )
    play_panic_alarm()


def _trade_row(quote: QuoteClient, symbol: str, row) -> Any:
    """Prefer snapshot bid/ask for market-order guard when quote row omits them."""
    _, enriched = quote.quote_for_trade(symbol, row=row)
    return enriched if enriched is not None else row


def _compact_signal(reason: str) -> str:
    if reason == "order pending":
        return "waiting fill"
    if reason.startswith("holding"):
        head = reason.split(" (", 1)[0]
        if "|" in reason:
            parts = [p.strip() for p in reason.split("|")]
            triggers = [p for p in parts if p.startswith(("cut ", "profit ", "panic"))]
            if triggers:
                return f"{head} ({' | '.join(triggers)})"
        return head
    return reason


def _session_pnl_pts(strategy: MHImainStrategy, position: UnitPositionBook, price: float) -> float:
    return strategy.pnl_baseline.session_total_pnl(
        strategy.entry_price, price, position.position
    )


def _order_line(
    side: str,
    qty: int,
    price: float,
    result: dict,
    *,
    pos_after: int | None = None,
    cum_pnl: float | None = None,
) -> str:
    q = int(qty)
    if result.get("status") in ("rejected", "skipped") or not result.get("ok"):
        reason = str(result.get("reason") or result.get("error") or "failed")
        if "market blocked" in reason:
            text = f"{side} x{q} blocked (slippage)"
        else:
            text = f"{side} x{q} rejected"
    elif result.get("filled") or result.get("status", "").upper().startswith("FILLED"):
        text = f"{side} x{q} filled @ {price:.0f}"
    else:
        oid = result.get("order_id") or "?"
        text = f"{side} x{q} pending #{oid}"
    if pos_after is not None:
        text += f" → pos={pos_after:+d}"
    if cum_pnl is not None:
        text += f" | cum {cum_pnl:+.0f}pts"
    return text


def _log_order(
    side: str,
    qty: int,
    price: float,
    result: dict,
    *,
    pos_after: int | None = None,
    cum_pnl: float | None = None,
) -> None:
    LOG.info(
        _order_line(side, qty, price, result, pos_after=pos_after, cum_pnl=cum_pnl),
        extra={
            "event": "order",
            "side": side,
            "qty": qty,
            "price": price,
            "pos_after": pos_after,
            "cum_pnl_pts": cum_pnl,
            "status": result.get("status"),
            "ok": result.get("ok"),
        },
    )


def _print_status(
    symbol: str,
    price: float,
    position: UnitPositionBook,
    signal_reason: str,
    update_time: str | None = None,
    *,
    strategy: MHImainStrategy | None = None,
    diff: float | None = None,
    total_change: float | None = None,
) -> None:
    ts = update_time or datetime.now().strftime("%H:%M:%S")
    move_tag = ""
    if diff is not None and total_change is not None:
        move_tag = f" {diff:+.0f}|{total_change:+.0f}"
    pnl_tag = ""
    entry_tag = ""
    if strategy is not None:
        if strategy.entry_price is not None:
            entry_tag = f" e{strategy.entry_price:.0f}"
        pnl = live_pnl_points(strategy, position, price)
        if pnl is not None:
            pnl_tag = f" P/L{pnl:+.0f}"
    LOG.info(
        f"[{ts}] {price:.0f}{entry_tag} {position.contracts:+d}{pnl_tag}{move_tag} — {_compact_signal(signal_reason)}",
        extra={
            "event": "poll",
            "symbol": symbol,
            "price": price,
            "contracts": position.contracts,
            "entry_price": strategy.entry_price if strategy else None,
            "pnl_pts": live_pnl_points(strategy, position, price) if strategy else None,
            "diff_pts": diff,
            "total_change_pts": total_change,
            "data_time": update_time,
            "signal": signal_reason,
        },
    )


@dataclass
class PollDisplayGate:
    """Only emit status lines when price moves threshold pts from last shown quote."""

    threshold_pts: float
    ref_price: float | None = None
    total_change: float = 0.0

    def note_price(self, price: float) -> tuple[bool, float]:
        if self.ref_price is None:
            self.ref_price = price
            return False, 0.0
        diff = price - self.ref_price
        if abs(diff) < self.threshold_pts:
            return False, diff
        self.total_change += diff
        self.ref_price = price
        return True, diff


def _process_signal(
    cfg: dict,
    trade: TradeClient,
    position: UnitPositionBook,
    strategy: MHImainStrategy,
    risk: RiskManager,
    signal,
    symbol: str,
    quote_row,
    price: float,
    order_gate: OrderGate,
    trade_unlock: TradeUnlockSession | None = None,
    *,
    skip_auth_check: bool = False,
    force_flat: bool = False,
    quote_freshness: QuoteFreshness | None = None,
) -> None:
    if order_gate.pending:
        outcome = order_gate.try_resolve(trade, cfg, symbol, position, strategy, risk, price)
        if outcome == "filled":
            side = order_gate.side or "?"
            qty = int(order_gate.qty)
            _finalize_filled_order(strategy, risk, position, price, order_gate)
            cum = _session_pnl_pts(strategy, position, price)
            _log_order(
                side, qty, price,
                {"ok": True, "filled": True, "status": "FILLED"},
                pos_after=position.contracts,
                cum_pnl=cum,
            )
        elif outcome == "failed":
            LOG.warning("order cancelled — retry later", extra={"event": "order_cancelled"})
            strategy.rearm_entry_if_flat(position)
        if order_gate.pending:
            return

    if signal.action == Action.HOLD:
        return

    if not force_flat and risk.killed:
        LOG.error("kill switch active", extra={"event": "kill_switch", "reason": risk.kill_reason})
        return

    if (
        not force_flat
        and quote_freshness is not None
        and quote_freshness.block_entries
        and is_new_entry(position.contracts, signal.action)
    ):
        LOG.warning(
            "entry blocked: stale quote",
            extra={
                "event": "entry_blocked",
                "reason": "stale_quote",
                "action": signal.action.value,
                "poll_stale": quote_freshness.poll_stale,
                "data_stale": quote_freshness.data_stale,
            },
        )
        return

    if not skip_auth_check:
        if trade_unlock is not None and not trade_unlock.ensure_authorized(
            cfg, trade, position_contracts=position.contracts
        ):
            if trade_unlock.is_expired() and position.contracts != 0:
                LOG.warning("order blocked: auth expiry flatten pending", extra={"event": "entry_blocked", "reason": "auth_expiry"})
            else:
                LOG.warning("order blocked: trade locked", extra={"event": "entry_blocked", "reason": "trade_locked"})
            return

    result = execute_unit_order(
        cfg, trade, position, signal.action, symbol, quote_row=quote_row, trade_unlock=trade_unlock
    )
    side = position.resolve_order_side(signal.action)
    if side is None:
        return
    order_qty = position.order_qty(signal.action)

    if result.get("status") in ("rejected", "skipped") or not result.get("ok"):
        _log_order(side, order_qty, price, result, cum_pnl=_session_pnl_pts(strategy, position, price))
        if position.contracts == 0:
            strategy.rearm_entry_if_flat(position)
        return

    order_gate.mark_submitted(
        order_id=result.get("order_id"),
        side=side,
        signal_action=signal.action,
        qty=float(result.get("qty", order_qty)),
    )
    strategy.set_order_pending(True)

    if result.get("filled"):
        order_gate.try_resolve(trade, cfg, symbol, position, strategy, risk, price)
        _finalize_filled_order(strategy, risk, position, price, order_gate, fill_price=price)
        cum = _session_pnl_pts(strategy, position, price)
        _log_order(
            side, order_qty, price,
            {"ok": True, "filled": True, "status": "FILLED"},
            pos_after=position.contracts,
            cum_pnl=cum,
        )
        return

    _log_order(side, order_qty, price, result, cum_pnl=_session_pnl_pts(strategy, position, price))


def _finalize_filled_order(
    strategy: MHImainStrategy,
    risk: RiskManager,
    position: UnitPositionBook,
    price: float,
    order_gate: OrderGate,
    *,
    fill_price: float | None = None,
) -> None:
    action = order_gate.signal_action
    exit_price = fill_price if fill_price is not None else price
    entry_before = strategy.entry_price
    close_side = order_gate.side
    fill_qty = int(order_gate.qty)

    order_gate.clear()
    strategy.set_order_pending(False)

    if action is None:
        return

    was_cut_loss = action == Action.FLAT and strategy._pending_cut_loss
    was_take_profit = action == Action.FLAT and strategy._pending_take_profit
    is_close = was_cut_loss or was_take_profit or action == Action.FLAT

    if is_close and entry_before is not None and close_side is not None:
        position_sign = 1 if close_side == "SELL" else -1
        strategy.pnl_baseline.realize_on_close(entry_before, exit_price, position_sign)

    if is_close:
        strategy.on_position_closed(exit_price, was_cut_loss=was_cut_loss, was_take_profit=was_take_profit)
        position.contracts = 0
        position.reset_after_flat()
        risk.position_shares = 0
        strategy.rearm_entry_if_flat(position)
    elif action in (Action.BUY, Action.SELL):
        if position.contracts == 0 and close_side is not None:
            position.on_fill(close_side, fill_qty)
        if position.contracts != 0:
            strategy.on_new_entry(exit_price)
            risk.position_shares = position.contracts


def _cancel_pending_entry(
    order_gate: OrderGate,
    strategy: MHImainStrategy,
    position: UnitPositionBook,
    *,
    reason: str,
) -> None:
    if order_gate.pending and not order_gate.is_close_intent(position.contracts):
        LOG.warning(reason, extra={"event": "cancel_pending_entry"})
        order_gate.clear()
        strategy.set_order_pending(False)


def _resolve_pending_close(
    cfg: dict,
    trade: TradeClient,
    position: UnitPositionBook,
    strategy: MHImainStrategy,
    risk: RiskManager,
    symbol: str,
    price: float,
    order_gate: OrderGate,
    *,
    on_failed: str,
    on_failed_hook: Callable[[], None] | None = None,
) -> bool:
    """Resolve a pending close order. Returns True while still in flight."""
    if not order_gate.pending:
        return False
    if not order_gate.is_close_intent(position.contracts):
        return False

    outcome = order_gate.try_resolve(trade, cfg, symbol, position, strategy, risk, price)
    if outcome == "filled":
        side = order_gate.side or "?"
        qty = int(order_gate.qty)
        _finalize_filled_order(strategy, risk, position, price, order_gate)
        cum = _session_pnl_pts(strategy, position, price)
        _log_order(
            side, qty, price,
            {"ok": True, "filled": True, "status": "FILLED"},
            pos_after=position.contracts,
            cum_pnl=cum,
        )
    elif outcome == "failed":
        LOG.warning(on_failed, extra={"event": "close_cancelled"})
        if on_failed_hook is not None:
            on_failed_hook()
        strategy.rearm_entry_if_flat(position)
        return False
    return order_gate.pending


def _submit_forced_flat(
    cfg: dict,
    trade: TradeClient,
    position: UnitPositionBook,
    strategy: MHImainStrategy,
    risk: RiskManager,
    symbol: str,
    quote_row,
    price: float,
    order_gate: OrderGate,
    trade_unlock: TradeUnlockSession | None,
    *,
    rule: str,
    reason: str,
) -> bool:
    """Market-flat an open position; bypasses kill switch and auth lock."""
    if position.contracts == 0:
        return False
    if trade_unlock is not None:
        trade_unlock.ensure_broker_unlocked(trade)
    flat = Signal(rule, Action.FLAT, symbol, reason, {"price": price})
    _process_signal(
        cfg, trade, position, strategy, risk,
        flat, symbol, quote_row, price, order_gate, trade_unlock,
        skip_auth_check=True,
        force_flat=True,
    )
    return order_gate.pending or position.contracts != 0


def _handle_kill_switch(
    cfg: dict,
    trade: TradeClient,
    position: UnitPositionBook,
    strategy: MHImainStrategy,
    risk: RiskManager,
    symbol: str,
    quote_row,
    price: float,
    order_gate: OrderGate,
    trade_unlock: TradeUnlockSession | None,
) -> bool:
    """Flatten on kill switch before halting. Returns True to skip strategy signals."""
    if not risk.killed:
        return False

    if not risk._kill_close_announced:
        risk._kill_close_announced = True
        LOG.error(
            "kill switch — closing open position before halt",
            extra={"event": "kill_switch", "reason": risk.kill_reason},
        )

    _cancel_pending_entry(
        order_gate,
        strategy,
        position,
        reason="kill switch — cancelling pending entry order",
    )

    if _resolve_pending_close(
        cfg, trade, position, strategy, risk, symbol, price, order_gate,
        on_failed="  kill-switch close cancelled — retrying",
    ):
        return True

    if position.contracts != 0 and _submit_forced_flat(
        cfg, trade, position, strategy, risk, symbol, quote_row, price, order_gate, trade_unlock,
        rule="kill",
        reason="kill switch — close before halt",
    ):
        return True

    return False


def _handle_auth_expiry(
    cfg: dict,
    trade: TradeClient,
    position: UnitPositionBook,
    strategy: MHImainStrategy,
    risk: RiskManager,
    symbol: str,
    quote_row,
    price: float,
    order_gate: OrderGate,
    trade_unlock: TradeUnlockSession,
) -> bool:
    """Expiry shutdown: flatten first, then lock. Returns True to skip strategy signals."""
    if not trade_unlock.is_expired():
        return False

    if order_gate.pending and not order_gate.is_close_intent(position.contracts):
        _cancel_pending_entry(
            order_gate,
            strategy,
            position,
            reason="auth expired — cancelling pending entry order before flatten",
        )

    if _resolve_pending_close(
        cfg, trade, position, strategy, risk, symbol, price, order_gate,
        on_failed="  auth-expiry close cancelled — retrying",
        on_failed_hook=trade_unlock.note_close_order_failed,
    ):
        return True

    if position.contracts != 0:
        trade_unlock.announce_expiry_close()
        if not trade_unlock.ensure_broker_unlocked(trade):
            LOG.warning(
                "broker trade unlock failed — retrying expiry close",
                extra={"event": "auth_expiry", "reason": "broker_unlock_failed"},
            )
        if _submit_forced_flat(
            cfg, trade, position, strategy, risk, symbol, quote_row, price, order_gate, trade_unlock,
            rule="auth",
            reason="auth expired — close before trade lock",
        ):
            if not order_gate.pending and position.contracts != 0:
                trade_unlock.note_close_order_failed()
            return True

    if trade_unlock.ensure_authorized(cfg, trade, position_contracts=0):
        return False

    return True


def execute_unit_order(
    cfg: dict,
    trade: TradeClient,
    position: UnitPositionBook,
    action: Action,
    symbol: str,
    quote_row=None,
    *,
    trade_unlock: TradeUnlockSession | None = None,
) -> dict:
    ok, reason = position.validate_transition(action)
    if not ok:
        return {"status": "rejected", "reason": reason}

    side = position.resolve_order_side(action)
    if side is None:
        return {"status": "skipped", "reason": "HOLD"}

    order_qty = position.order_qty(action)

    max_slip = float(cfg.get("mhimain", {}).get("max_market_slippage_pts", 3))
    if quote_row is not None:
        allowed, slip_reason, last_px, market_px = allow_market_order(quote_row, side, max_slip)
        if not allowed:
            return {
                "status": "rejected",
                "reason": slip_reason,
                "last_price": last_px,
                "market_price": market_px,
            }

    result = trade.place_market_order(
        code=symbol,
        qty=order_qty,
        side=side,
        trd_env=trd_env_from_config(cfg),
    )
    result["qty"] = order_qty
    if not result.get("ok"):
        result["status"] = "rejected"
        err = str(result.get("error", "")).lower()
        if trade_unlock is not None and any(
            token in err for token in ("unlock", "解锁", "locked", "锁定")
        ):
            trade_unlock.invalidate()
    elif not result.get("filled"):
        result["status"] = "submitted"
    result["position_after"] = position.contracts
    return result


def _position_label(contracts: int) -> str:
    if contracts > 0:
        return f"long (+{contracts})"
    if contracts < 0:
        return f"short ({contracts})"
    return "flat (0)"


def _print_position_snapshot(
    symbol: str,
    strategy: MHImainStrategy,
    position: UnitPositionBook,
    live_price: float | None,
    *,
    title: str = "Position",
    broker_pnl_val: float | None = None,
) -> None:
    pnl = live_pnl_points(strategy, position, live_price) if live_price is not None else None
    parts = [title, _position_label(position.contracts)]
    if strategy.entry_price is not None:
        parts.append(f"entry {strategy.entry_price:.0f}")
    if live_price is not None:
        parts.append(f"last {live_price:.0f}")
    if pnl is not None:
        parts.append(f"P/L {pnl:+.0f}")
    if position.contracts != 0:
        bl = strategy.pnl_baseline
        parts.append(f"cut {bl.cut_loss_trigger():+.0f} profit {bl.take_profit_trigger():+.0f}")
    LOG.info(" ".join(parts), extra={"event": "position_snapshot", "title": title, "contracts": position.contracts})


def _print_bootstrap(
    symbol: str,
    strategy: MHImainStrategy,
    position: UnitPositionBook,
    live_price: float | None,
    *,
    broker_pnl_val: float | None = None,
) -> None:
    _print_position_snapshot(
        symbol,
        strategy,
        position,
        live_price,
        title="Startup",
        broker_pnl_val=broker_pnl_val,
    )


def _bootstrap_account_equity(trade: TradeClient, cfg: dict, risk: RiskManager) -> None:
    equity = trade.fetch_account_equity(cfg)
    if equity is None:
        fallback = float(cfg.get("risk", {}).get("account_equity", 0) or 0)
        if fallback > 0:
            equity = fallback
            LOG.info(
                f"Account equity: {equity:,.0f} HKD (from risk.account_equity config)",
                extra={"event": "account_equity", "equity_hkd": equity, "source": "config"},
            )
        else:
            LOG.warning(
                "could not read account equity — max_daily_loss_pct disabled",
                extra={"event": "account_equity", "source": "unavailable"},
            )
            return
    else:
        LOG.info(
            f"Account equity: {equity:,.0f} HKD (day-start baseline for max_daily_loss_pct)",
            extra={"event": "account_equity", "equity_hkd": equity, "source": "broker"},
        )
    risk.set_equity(equity)


def _refresh_account_equity(trade: TradeClient, cfg: dict, risk: RiskManager) -> None:
    if risk.day_start_equity <= 0:
        return
    equity = trade.fetch_account_equity(cfg)
    if equity is not None:
        risk.refresh_equity(equity)


def main() -> None:
    parser = argparse.ArgumentParser(description="HK.MHImain unit-position trader")
    parser.add_argument("--config", default="mhimain.yaml")
    parser.add_argument("--iterations", type=int, default=None)
    parser.add_argument(
        "--trend",
        choices=["bull", "bear", "uncertain"],
        default=None,
        help="Session trend (default: mhimain.default_trend in config)",
    )
    parser.add_argument(
        "--prompt-trend",
        action="store_true",
        help="Interactively choose trend at startup",
    )
    parser.add_argument(
        "--poll-threshold",
        type=float,
        default=None,
        help="Only print poll status when price moves this many pts (default: data.poll_display_threshold_pts)",
    )
    parser.add_argument(
        "--trade-password",
        default=None,
        help="Trade unlock password (REAL only; non-interactive/systemd — prefer FUTU_TRADE_PASSWORD env)",
    )
    parser.add_argument(
        "--log-format",
        choices=["json", "text"],
        default=None,
        help="Log format (default: logging.format in config)",
    )
    parser.add_argument(
        "--log-level",
        default=None,
        help="Log level (default: logging.level in config)",
    )
    args = parser.parse_args()

    cfg = load_config(args.config)
    setup_logging(cfg, level=args.log_level, fmt=args.log_format)
    mhi_cfg = cfg.get("mhimain", {})
    data_cfg = cfg.get("data", {})
    symbol = str(mhi_cfg.get("symbol", "HK.MHImain"))
    poll_interval = float(data_cfg.get("poll_interval_sec", 1))
    poll_display_threshold = float(
        args.poll_threshold
        if args.poll_threshold is not None
        else data_cfg.get("poll_display_threshold_pts", 20)
    )
    position_refresh_sec = float(mhi_cfg.get("position_refresh_sec", 60))
    ma_period = int(mhi_cfg.get("ma_period", 5))
    endpoints = endpoints_from_config(cfg)

    if args.prompt_trend:
        trend = prompt_trend_mode()
    else:
        trend_name = args.trend or mhi_cfg.get("default_trend", "uncertain")
        trend = TrendMode(str(trend_name).lower())

    strategy = MHImainStrategy.from_config(cfg, trend=trend)
    position = UnitPositionBook()
    risk = RiskManager({**cfg, "risk": {**cfg.get("risk", {}), "max_position_shares": 1}})

    LOG.info(
        "mhimain trader starting",
        extra={
            "event": "startup",
            "symbol": symbol,
            "trd_env": trd_env_name(cfg),
            "trend": trend.value,
            "poll_interval_sec": poll_interval,
            "poll_display_threshold_pts": poll_display_threshold,
            "ma_period": ma_period,
            "stale_threshold_sec": stale_threshold_sec(cfg, poll_interval),
        },
    )
    warn_short_trade_unlock_window(cfg)
    LOG.info(
        "strategy config",
        extra={
            "event": "startup_config",
            "cut_loss_pts": mhi_cfg.get("cut_loss_pts", 200),
            "take_profit_pts": mhi_cfg.get("take_profit_pts", 400),
            "cut_loss_min_hold_hours": mhi_cfg.get("cut_loss_min_hold_hours", 24),
            "reentry_minimum_hours": mhi_cfg.get("reentry_minimum_hours", 4),
            "reentry_move_pts": mhi_cfg.get("reentry_move_pts", 300),
            "reentry_trading_hours": mhi_cfg.get("reentry_trading_hours", 6),
            "panic_move_pts": mhi_cfg.get("panic_move_pts", 200),
            "panic_window_sec": mhi_cfg.get("panic_window_sec", 200),
            "panic_wait_min": mhi_cfg.get("panic_wait_min", 30),
        },
    )

    with QuoteClient(endpoints) as quote, TradeClient(endpoints, futures=True) as trade:
        ok, state = quote.global_state()
        if not ok:
            LOG.error("failed to connect to OpenD", extra={"event": "opend_connect", "ok": False})
            sys.exit(1)
        LOG.info("OpenD connected", extra={"event": "opend_connect", "state": str(state)})

        trade_unlock = maybe_create_unlock_session(
            cfg, trade, cli_password=args.trade_password
        )

        _bootstrap_account_equity(trade, cfg, risk)

        sub_ok, sub_msg = quote.subscribe_quote([symbol])
        if not sub_ok:
            LOG.warning(
                "quote subscribe failed",
                extra={"event": "quote_subscribe", "symbol": symbol, "detail": str(sub_msg)},
            )

        ret_ok, quote_data = quote.quote([symbol])
        quote_price = float(quote_data.iloc[0]["last_price"]) if ret_ok and len(quote_data) else None
        if quote_price is None:
            LOG.warning("no initial quote — waiting for live feed", extra={"event": "quote", "symbol": symbol})

        broker = fetch_broker_position(trade, symbol, cfg, quote_price=quote_price)
        apply_broker_position(broker, position, strategy)
        if broker.contracts != 0:
            strategy.on_broker_position_opened()
        strategy.sync_existing_position(broker.contracts != 0)
        risk.position_shares = broker.contracts

        ok_ma, ma5 = quote.daily_ma(symbol, period=ma_period)
        strategy.set_ma5(ma5)
        if ok_ma and ma5 is not None:
            LOG.info(f"MA{ma_period} (daily): {ma5:.1f}", extra={"event": "ma", "ma_period": ma_period, "ma": ma5})
        elif trend == TrendMode.UNCERTAIN:
            LOG.warning(
                f"could not fetch MA{ma_period}; uncertain entries need MA",
                extra={"event": "ma", "ma_period": ma_period, "ok": False},
            )

        _print_bootstrap(symbol, strategy, position, quote_price, broker_pnl_val=broker.pnl_val)

        order_gate = OrderGate()
        poll_display = PollDisplayGate(threshold_pts=poll_display_threshold)
        if quote_price is not None:
            poll_display.ref_price = quote_price

        # Try opening immediately when flat at launch
        if (
            position.contracts == 0
            and not strategy.cooldown.locked
            and not risk.killed
            and quote_price is not None
        ):
            launch_signal = strategy.update(quote_price, position)
            if launch_signal.action != Action.HOLD:
                launch_row = quote_data.iloc[0]
                launch_data_time = str(launch_row.get("data_time", ""))
                launch_fresh = assess_quote_freshness(
                    cfg,
                    poll_interval,
                    last_successful_poll_at=None,
                    data_time=launch_data_time,
                )
                if launch_fresh.block_entries and is_new_entry(position.contracts, launch_signal.action):
                    _log_quote_stale(launch_fresh, event="launch_blocked_stale")
                else:
                    LOG.info(
                        f"Launch: {launch_signal.action.value} — {launch_signal.reason}",
                        extra={
                            "event": "launch",
                            "action": launch_signal.action.value,
                            "reason": launch_signal.reason,
                        },
                    )
                    _, trade_row = quote.quote_for_trade(symbol, row=launch_row)
                    _process_signal(
                        cfg, trade, position, strategy, risk,
                        launch_signal, symbol, trade_row, quote_price, order_gate, trade_unlock,
                        quote_freshness=launch_fresh,
                    )

        count = 0
        last_poll_at = None
        last_position_refresh = time.monotonic()
        try:
            while args.iterations is None or count < args.iterations:
                ret_ok, data = quote.quote([symbol])
                if not ret_ok or data is None or len(data) == 0:
                    fail_fresh = assess_quote_freshness(
                        cfg,
                        poll_interval,
                        last_successful_poll_at=last_poll_at,
                        data_time=None,
                        poll_failed=True,
                    )
                    _log_quote_stale(fail_fresh, event="poll_failed")
                    if trade_unlock is not None and trade_unlock.mark_opend_unhealthy():
                        LOG.warning(
                            "OpenD unreachable — trading paused until quote path recovers",
                            extra={"event": "opend_unhealthy"},
                        )
                    LOG.warning(
                        "quote poll failed — check OpenD / quote subscription",
                        extra={"event": "quote_poll_failed", "symbol": symbol},
                    )
                    time.sleep(poll_interval)
                    count += 1
                    continue

                poll_now = datetime.now(timezone.utc)
                prev_poll_at = last_poll_at

                if trade_unlock is not None:
                    trade_unlock.on_opend_recovered(trade)

                row = data.iloc[0]
                price = float(row["last_price"])
                update_time = str(row.get("data_time", ""))
                quote_fresh = assess_quote_freshness(
                    cfg,
                    poll_interval,
                    last_successful_poll_at=prev_poll_at,
                    data_time=update_time,
                    now=poll_now,
                )
                last_poll_at = poll_now
                if quote_fresh.block_entries:
                    _log_quote_stale(quote_fresh, event="quote_stale")

                now_mono = time.monotonic()
                if now_mono - last_position_refresh >= position_refresh_sec:
                    if (
                        not order_gate.pending
                        and (trade_unlock is None or not trade_unlock.is_expired())
                    ):
                        broker = fetch_broker_position(trade, symbol, cfg, quote_price=price)
                        changed = refresh_broker_position(broker, position, strategy, risk, cfg=cfg)
                        if changed:
                            LOG.warning(
                                f"broker position changed → {_position_label(broker.contracts)}",
                                extra={
                                    "event": "position_refresh",
                                    "contracts": broker.contracts,
                                },
                            )
                            _print_position_snapshot(
                                symbol,
                                strategy,
                                position,
                                price,
                                title="Refresh",
                                broker_pnl_val=broker.pnl_val,
                            )
                    _refresh_account_equity(trade, cfg, risk)
                    last_position_refresh = now_mono

                if _handle_kill_switch(
                    cfg, trade, position, strategy, risk,
                    symbol, _trade_row(quote, symbol, row), price, order_gate, trade_unlock,
                ):
                    time.sleep(poll_interval)
                    count += 1
                    continue

                if trade_unlock is not None and _handle_auth_expiry(
                    cfg, trade, position, strategy, risk,
                    symbol, _trade_row(quote, symbol, row), price, order_gate, trade_unlock,
                ):
                    time.sleep(poll_interval)
                    count += 1
                    continue

                signal = strategy.update(price, position)
                if strategy.consume_panic_trigger():
                    _alert_panic_pause(strategy, price)
                cooldown_tag = ""
                if strategy.cooldown.locked:
                    cooldown_tag = " [COOLDOWN]"
                if strategy.panic_guard.active:
                    cooldown_tag += " [PANIC_PAUSE]"
                if order_gate.pending:
                    cooldown_tag += " [ORDER_PENDING]"
                cooldown_tag += quote_fresh.status_tag
                status = signal.reason
                if signal.action != Action.HOLD:
                    status = f"{signal.action.value}: {signal.reason}"

                show_poll, diff = poll_display.note_price(price)
                if show_poll:
                    _print_status(
                        symbol,
                        price,
                        position,
                        status + cooldown_tag,
                        update_time,
                        strategy=strategy,
                        diff=diff,
                        total_change=poll_display.total_change,
                    )

                if risk.killed and position.contracts == 0 and not order_gate.pending:
                    LOG.error(
                        "kill switch halt",
                        extra={"event": "kill_switch_halt", "reason": risk.kill_reason},
                    )
                    break

                _process_signal(
                    cfg, trade, position, strategy, risk,
                    signal, symbol, _trade_row(quote, symbol, row), price, order_gate, trade_unlock,
                    quote_freshness=quote_fresh,
                )

                time.sleep(poll_interval)
                count += 1
        except KeyboardInterrupt:
            LOG.info(
                "stopped",
                extra={"event": "shutdown", "final_contracts": position.contracts},
            )


if __name__ == "__main__":
    main()
