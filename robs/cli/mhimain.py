#!/usr/bin/env python3
"""Standalone HK.MHImain trader — front-month bot entries and optional next-month legs."""

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
from robs.execution.contract_rollover import (
    ContractRolloverManager,
    HK,
    contract_log_label,
    is_continuous_mhi,
    is_held_ahead_of_front,
    is_ltd_expiring_month_open_banned,
    is_named_mhi_contract,
    resolve_rollover_target,
    should_rollover,
)
from robs.execution.market_guard import allow_market_order
from robs.execution.mhi_portfolio import MHIPortfolio, parse_quote_batch
from robs.execution.order_gate import OrderGate
from robs.execution.position import UnitPositionBook
from robs.execution.position_sync import fetch_broker_mhi_legs, live_pnl_points
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


def _sync_front_context(
    portfolio: MHIPortfolio,
    quote: QuoteClient,
    symbol: str,
    front_contract: str | None,
) -> None:
    if is_continuous_mhi(symbol) and front_contract:
        ltd = quote.contract_last_trade_time(front_contract)
        if ltd is None and portfolio.front_last_trade_time:
            ltd = portfolio.front_last_trade_time
        portfolio.update_front_context(front_contract, ltd)
    else:
        portfolio.update_front_context(None, None)


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


@dataclass
class _RolloverSkipGate:
    """Emit at most one rollover-skip notice per (held, front, reason) episode."""

    _last: str | None = None

    def should_log(self, key: str) -> bool:
        if key == self._last:
            return False
        self._last = key
        return True

    def reset(self) -> None:
        self._last = None


_rollover_skip_gate = _RolloverSkipGate()


def _log_rollover_skip(
    held: str,
    front: str | None,
    *,
    reason: str,
    last_trade_time: str | None = None,
) -> None:
    front_label = contract_log_label(front) if front else "?"
    key = f"{held}|{front}|{reason}"
    if not _rollover_skip_gate.should_log(key):
        return
    held_label = contract_log_label(held)
    if reason == "held_ahead_of_front":
        msg = f"rollover skipped: held {held_label} ahead of front {front_label}"
    else:
        msg = f"rollover skipped: {reason}"
    LOG.info(
        msg,
        extra={
            "event": "rollover_skipped",
            "reason": reason,
            "held_contract": held,
            "front_contract": front,
            "last_trade_time": last_trade_time,
        },
    )


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
    contract: str | None = None,
    pos_after: int | None = None,
    cum_pnl: float | None = None,
) -> str:
    q = int(qty)
    contract_tag = f" {contract_log_label(contract)}" if contract else ""
    if result.get("status") in ("rejected", "skipped") or not result.get("ok"):
        reason = str(result.get("reason") or result.get("error") or "failed")
        if "market blocked" in reason:
            text = f"{side} x{q}{contract_tag} blocked (slippage)"
        else:
            text = f"{side} x{q}{contract_tag} rejected"
    elif result.get("filled") or result.get("status", "").upper().startswith("FILLED"):
        text = f"{side} x{q}{contract_tag} filled @ {price:.0f}"
    else:
        oid = result.get("order_id") or "?"
        text = f"{side} x{q}{contract_tag} pending #{oid}"
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
    contract: str | None = None,
    pos_after: int | None = None,
    cum_pnl: float | None = None,
) -> None:
    LOG.info(
        _order_line(
            side, qty, price, result,
            contract=contract,
            pos_after=pos_after,
            cum_pnl=cum_pnl,
        ),
        extra={
            "event": "order",
            "side": side,
            "qty": qty,
            "contract": contract_log_label(contract) or None,
            "order_code": contract,
            "price": price,
            "pos_after": pos_after,
            "cum_pnl_pts": cum_pnl,
            "status": result.get("status"),
            "ok": result.get("ok"),
        },
    )


@dataclass
class PollDisplayGate:
    """Only emit status lines when price moves threshold pts from last shown quote."""

    threshold_pts: float
    ref_price: float | None = None
    total_change: float = 0.0

    def note_price(self, price: float | None) -> tuple[bool, float]:
        if price is None:
            return False, 0.0
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
    order_code: str | None = None,
    bypass_entry_guards: bool = False,
    front_contract: str | None = None,
    front_last_trade_time: str | None = None,
    portfolio: MHIPortfolio | None = None,
) -> None:
    trade_code = order_code or symbol

    def _sync_portfolio_risk() -> None:
        if portfolio is not None:
            portfolio._sync_risk_shares(risk)

    if order_gate.pending:
        outcome = order_gate.try_resolve(trade, cfg, symbol, position, strategy, risk, price)
        if outcome == "filled":
            side = order_gate.side or "?"
            qty = int(order_gate.qty)
            contract = order_gate.order_code or trade_code
            _finalize_filled_order(strategy, risk, position, price, order_gate)
            _sync_portfolio_risk()
            cum = _session_pnl_pts(strategy, position, price)
            _log_order(
                side, qty, price,
                {"ok": True, "filled": True, "status": "FILLED"},
                contract=contract,
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
        and not bypass_entry_guards
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

    if (
        not force_flat
        and not bypass_entry_guards
        and is_new_entry(position.contracts, signal.action)
        and front_contract
        and is_ltd_expiring_month_open_banned(
            trade_code,
            front_contract,
            front_last_trade_time,
            now=datetime.now(HK),
        )
    ):
        LOG.warning(
            "entry blocked: expiring month not open for new entries on LTD (11:58–16:30)",
            extra={
                "event": "entry_blocked",
                "reason": "ltd_expiring_month",
                "order_code": trade_code,
                "front_contract": front_contract,
            },
        )
        strategy.rearm_entry_if_flat(position)
        return

    if (
        not force_flat
        and not bypass_entry_guards
        and is_new_entry(position.contracts, signal.action)
        and quote_row is None
    ):
        LOG.warning(
            "entry blocked: quote row unavailable",
            extra={
                "event": "entry_blocked",
                "reason": "no_quote_row",
                "order_code": trade_code,
            },
        )
        strategy.rearm_entry_if_flat(position)
        return

    if portfolio is not None:
        risk.position_shares = portfolio.total_signed_contracts()

    result = execute_unit_order(
        cfg, trade, position, signal.action, symbol, quote_row=quote_row,
        trade_unlock=trade_unlock, order_code=trade_code,
        risk=risk,
        portfolio_total_signed=portfolio.total_signed_contracts() if portfolio else None,
    )
    side = position.resolve_order_side(signal.action)
    if side is None:
        return
    order_qty = position.order_qty(signal.action)

    if result.get("status") in ("rejected", "skipped") or not result.get("ok"):
        _log_order(
            side, order_qty, price, result,
            contract=trade_code,
            cum_pnl=_session_pnl_pts(strategy, position, price),
        )
        if position.contracts == 0:
            strategy.rearm_entry_if_flat(position)
        return

    order_gate.mark_submitted(
        order_id=result.get("order_id"),
        side=side,
        signal_action=signal.action,
        qty=float(result.get("qty", order_qty)),
        order_code=trade_code,
    )
    strategy.set_order_pending(True)

    if result.get("filled"):
        order_gate.try_resolve(trade, cfg, symbol, position, strategy, risk, price)
        _finalize_filled_order(strategy, risk, position, price, order_gate, fill_price=price)
        _sync_portfolio_risk()
        cum = _session_pnl_pts(strategy, position, price)
        _log_order(
            side, order_qty, price,
            {"ok": True, "filled": True, "status": "FILLED"},
            contract=trade_code,
            pos_after=position.contracts,
            cum_pnl=cum,
        )
        return

    _log_order(
        side, order_qty, price, result,
        contract=trade_code,
        cum_pnl=_session_pnl_pts(strategy, position, price),
    )


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
        contract = order_gate.order_code or symbol
        _finalize_filled_order(strategy, risk, position, price, order_gate)
        cum = _session_pnl_pts(strategy, position, price)
        _log_order(
            side, qty, price,
            {"ok": True, "filled": True, "status": "FILLED"},
            contract=contract,
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


def _resolve_rollover_pending(
    cfg: dict,
    trade: TradeClient,
    position: UnitPositionBook,
    strategy: MHImainStrategy,
    risk: RiskManager,
    symbol: str,
    price: float,
    order_gate: OrderGate,
    rollover: ContractRolloverManager,
    *,
    on_opened: Callable[[str], None] | None = None,
) -> bool:
    """Resolve in-flight rollover order and advance the state machine."""
    if not order_gate.pending or not rollover.busy():
        return False

    outcome = order_gate.try_resolve(trade, cfg, symbol, position, strategy, risk, price)
    if outcome == "filled":
        side = order_gate.side or "?"
        qty = int(order_gate.qty)
        contract = order_gate.order_code or symbol
        _finalize_filled_order(strategy, risk, position, price, order_gate)
        cum = _session_pnl_pts(strategy, position, price)
        _log_order(
            side, qty, price,
            {"ok": True, "filled": True, "status": "FILLED"},
            contract=contract,
            pos_after=position.contracts,
            cum_pnl=cum,
        )
        if rollover.state.phase == "close" and position.contracts == 0:
            rollover.advance_after_close(0)
        elif rollover.state.phase == "open" and position.contracts != 0:
            opened = rollover.state.target_contract
            rollover.complete_if_opened(position.contracts)
            if on_opened is not None and opened:
                on_opened(opened)
    elif outcome == "failed":
        LOG.warning(
            "rollover order cancelled — retry later",
            extra={"event": "rollover_cancelled", "phase": rollover.state.phase},
        )
        strategy.rearm_entry_if_flat(position)
        rollover.reset()
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
    order_code: str | None = None,
) -> bool:
    """Market-flat an open position; bypasses kill switch and auth lock."""
    if position.contracts == 0:
        return False
    if trade_unlock is not None:
        trade_unlock.ensure_broker_unlocked(trade)
    close_code = order_code or symbol
    flat = Signal(rule, Action.FLAT, symbol, reason, {"price": price})
    _process_signal(
        cfg, trade, position, strategy, risk,
        flat, symbol, quote_row, price, order_gate, trade_unlock,
        skip_auth_check=True,
        force_flat=True,
        order_code=close_code,
    )
    return order_gate.pending or position.contracts != 0


def _handle_contract_rollover(
    cfg: dict,
    trade: TradeClient,
    quote: QuoteClient,
    position: UnitPositionBook,
    strategy: MHImainStrategy,
    risk: RiskManager,
    quote_symbol: str,
    quote_row,
    price: float,
    order_gate: OrderGate,
    trade_unlock: TradeUnlockSession | None,
    rollover: ContractRolloverManager,
    *,
    front_contract: str | None,
    held_contract: str | None,
    last_trade_time: str | None,
    on_opened: Callable[[str], None] | None = None,
    rows: dict[str, Any] | None = None,
    prices: dict[str, float] | None = None,
    rollover_open_skip: Callable[[str, int], bool] | None = None,
    on_rollover_open_skipped: Callable[[str, int], bool] | None = None,
) -> bool:
    """Close expiring month and reopen on front month. Returns True to skip strategy."""
    if not rollover.active():
        return False

    mhi_cfg = cfg.get("mhimain", {})
    on_last = bool(mhi_cfg.get("rollover_on_last_trade_day", True))

    rollover.note_held_contract(held_contract, position.contracts)

    if rollover.busy() and order_gate.pending:
        _resolve_rollover_pending(
            cfg, trade, position, strategy, risk, quote_symbol, price, order_gate, rollover,
            on_opened=on_opened,
        )
        return True

    if rollover.state.phase == "idle":
        if position.contracts == 0:
            _rollover_skip_gate.reset()
            return False
        held = held_contract or rollover.state.held_contract
        if not held or not is_named_mhi_contract(held):
            return False
        do_roll, reason = should_rollover(
            held,
            front_contract,
            last_trade_time,
            now=datetime.now(HK),
            on_last_trade_day=on_last,
        )
        if not do_roll:
            if front_contract and is_held_ahead_of_front(held, front_contract):
                _log_rollover_skip(held, front_contract, reason="held_ahead_of_front")
            return False
        target = resolve_rollover_target(
            held,
            front_contract,
            last_trade_time=last_trade_time,
            now=datetime.now(HK),
        )
        if not target:
            return False
        _cancel_pending_entry(
            order_gate,
            strategy,
            position,
            reason="rollover — cancelling pending entry order",
        )
        rollover.begin(
            held=held,
            front=target,
            direction=1 if position.contracts > 0 else -1,
            reason=reason,
        )
        LOG.warning(
            "contract rollover started",
            extra={
                "event": "contract_rollover",
                "phase": "close",
                "reason": reason,
                "held_contract": held,
                "target_contract": target,
                "direction": rollover.state.direction,
            },
        )

    if rollover.state.phase == "close":
        if position.contracts == 0:
            rollover.advance_after_close(0)
        else:
            close_code = rollover.state.held_contract or held_contract or quote_symbol
            flat = Signal(
                "rollover",
                Action.FLAT,
                quote_symbol,
                f"rollover close {close_code} ({rollover.state.reason})",
                {"price": price},
            )
            _process_signal(
                cfg, trade, position, strategy, risk,
                flat, quote_symbol, quote_row, price, order_gate, trade_unlock,
                skip_auth_check=True,
                force_flat=True,
                order_code=close_code,
            )
            return True

    if rollover.state.phase == "open":
        if position.contracts != 0:
            opened = rollover.state.target_contract
            rollover.complete_if_opened(position.contracts)
            if on_opened is not None and opened:
                on_opened(opened)
            return False
        open_code = rollover.state.target_contract or front_contract
        if not open_code:
            return True
        held = rollover.state.held_contract
        if held and open_code.upper() == held.upper():
            LOG.error(
                "contract rollover aborted — target same as expired contract",
                extra={
                    "event": "contract_rollover",
                    "phase": "open",
                    "held_contract": held,
                    "target_contract": open_code,
                },
            )
            rollover.reset()
            return True
        if rollover_open_skip and rollover_open_skip(open_code, rollover.state.direction):
            direction = rollover.state.direction
            LOG.warning(
                "contract rollover skip open — target month already held",
                extra={
                    "event": "contract_rollover",
                    "phase": "open",
                    "target_contract": open_code,
                    "direction": direction,
                },
            )
            absorbed = False
            if on_rollover_open_skipped is not None:
                absorbed = on_rollover_open_skipped(open_code, direction)
            rollover.complete_without_open()
            if absorbed and on_opened is not None:
                on_opened(open_code)
            return True
        if rows is not None and prices is not None:
            open_row, open_price = _leg_trade_context(quote, quote_symbol, open_code, rows, prices)
        else:
            open_row, open_price = quote_row, price
        action = Action.BUY if rollover.state.direction > 0 else Action.SELL
        open_sig = Signal(
            "rollover",
            action,
            quote_symbol,
            f"rollover open {open_code} ({rollover.state.reason})",
            {"price": open_price},
        )
        _process_signal(
            cfg, trade, position, strategy, risk,
            open_sig, quote_symbol, open_row, open_price, order_gate, trade_unlock,
            skip_auth_check=True,
            bypass_entry_guards=True,
            order_code=open_code,
        )
        if rollover.state.phase == "open" and position.contracts != 0:
            opened = rollover.state.target_contract
            rollover.complete_if_opened(position.contracts)
            if on_opened is not None and opened:
                on_opened(opened)
        return True

    return False


def _leg_trade_context(
    quote: QuoteClient,
    quote_symbol: str,
    leg_code: str,
    rows: dict[str, Any],
    prices: dict[str, float],
) -> tuple[Any | None, float]:
    row = rows.get(leg_code) or rows.get(quote_symbol)
    price = float(prices.get(leg_code, prices.get(quote_symbol, 0.0)))
    if row is None:
        return None, price
    return _trade_row(quote, leg_code, row), price


def _flat_entry_context(
    portfolio: MHIPortfolio,
    front_contract: str | None,
    quote: QuoteClient,
    quote_symbol: str,
    rows: dict[str, Any],
    prices: dict[str, float],
) -> tuple[UnitPositionBook, MHImainStrategy, str, Any | None, float, Any | None]:
    position, strategy, order_code = portfolio.flat_entry_target(front_contract)
    row = rows.get(order_code) or rows.get(quote_symbol)
    price = float(prices.get(order_code, prices.get(quote_symbol, 0.0)))
    trade_row = _trade_row(quote, order_code, row) if row is not None else None
    return position, strategy, order_code, trade_row, price, row


def _allows_next_month_close(position: UnitPositionBook, action: Action) -> bool:
    """Next-month legs are manual opens — bot may only send close/cover orders."""
    if action == Action.FLAT and position.contracts != 0:
        return True
    if position.contracts > 0 and action == Action.SELL:
        return True
    if position.contracts < 0 and action == Action.BUY:
        return True
    return False


def _pending_close_contracts(portfolio: MHIPortfolio, order_gate: OrderGate) -> int:
    if order_gate.order_code:
        leg = portfolio.leg_for_code(order_gate.order_code)
        if leg is not None:
            return leg.position.contracts
    return portfolio.entry_position.contracts


def _resolve_pending_for_gate(
    cfg: dict,
    trade: TradeClient,
    portfolio: MHIPortfolio,
    risk: RiskManager,
    quote_symbol: str,
    rows: dict[str, Any],
    prices: dict[str, float],
    quote: QuoteClient,
    order_gate: OrderGate,
    *,
    on_failed: str,
    on_failed_hook: Callable[[], None] | None = None,
) -> bool:
    if not order_gate.pending:
        return False
    code = order_gate.order_code or quote_symbol
    leg = portfolio.leg_for_code(code)
    if leg is None:
        position = portfolio.entry_position
        strategy = portfolio.entry_strategy
        entry_code = portfolio.managed_entry_code()
        row, price = _leg_trade_context(quote, quote_symbol, entry_code, rows, prices)
    else:
        position = leg.position
        strategy = leg.strategy
        row, price = _leg_trade_context(quote, quote_symbol, leg.code, rows, prices)
    return _resolve_pending_close(
        cfg, trade, position, strategy, risk, quote_symbol, price, order_gate,
        on_failed=on_failed,
        on_failed_hook=on_failed_hook,
    )


def _handle_kill_switch_portfolio(
    cfg: dict,
    trade: TradeClient,
    portfolio: MHIPortfolio,
    risk: RiskManager,
    quote: QuoteClient,
    quote_symbol: str,
    rows: dict[str, Any],
    prices: dict[str, float],
    order_gate: OrderGate,
    trade_unlock: TradeUnlockSession | None,
) -> bool:
    if not risk.killed:
        return False

    for leg in portfolio.legs.values():
        if leg.rollover is not None and leg.rollover.busy():
            leg.rollover.reset()
    if portfolio.entry_rollover is not None and portfolio.entry_rollover.busy():
        portfolio.entry_rollover.reset()

    if not risk._kill_close_announced:
        risk._kill_close_announced = True
        LOG.error(
            "kill switch — closing open positions before halt",
            extra={"event": "kill_switch", "reason": risk.kill_reason},
        )

    _cancel_pending_entry(
        order_gate,
        portfolio.entry_strategy,
        portfolio.entry_position,
        reason="kill switch — cancelling pending entry order",
    )

    if _resolve_pending_for_gate(
        cfg, trade, portfolio, risk, quote_symbol, rows, prices, quote, order_gate,
        on_failed="  kill-switch close cancelled — retrying",
    ):
        return True

    if portfolio.entry_position.contracts != 0:
        close_code = portfolio.managed_entry_code()
        row, price = _leg_trade_context(quote, quote_symbol, close_code, rows, prices)
        if _submit_forced_flat(
            cfg, trade, portfolio.entry_position, portfolio.entry_strategy, risk,
            quote_symbol, row, price, order_gate, trade_unlock,
            rule="kill",
            reason="kill switch — close before halt",
            order_code=close_code,
        ):
            return True

    for leg in portfolio.ahead_of_front_legs():
        row, price = _leg_trade_context(quote, quote_symbol, leg.code, rows, prices)
        if _submit_forced_flat(
            cfg, trade, leg.position, leg.strategy, risk,
            quote_symbol, row, price, order_gate, trade_unlock,
            rule="kill",
            reason="kill switch — close before halt",
            order_code=leg.code,
        ):
            return True

    return False


def _handle_auth_expiry_portfolio(
    cfg: dict,
    trade: TradeClient,
    portfolio: MHIPortfolio,
    risk: RiskManager,
    quote: QuoteClient,
    quote_symbol: str,
    rows: dict[str, Any],
    prices: dict[str, float],
    order_gate: OrderGate,
    trade_unlock: TradeUnlockSession,
) -> bool:
    if not trade_unlock.is_expired():
        return False

    for leg in portfolio.legs.values():
        if leg.rollover is not None and leg.rollover.busy():
            leg.rollover.reset()
    if portfolio.entry_rollover is not None and portfolio.entry_rollover.busy():
        portfolio.entry_rollover.reset()

    if order_gate.pending and not order_gate.is_close_intent(
        _pending_close_contracts(portfolio, order_gate)
    ):
        _cancel_pending_entry(
            order_gate,
            portfolio.entry_strategy,
            portfolio.entry_position,
            reason="auth expired — cancelling pending entry order before flatten",
        )

    if _resolve_pending_for_gate(
        cfg, trade, portfolio, risk, quote_symbol, rows, prices, quote, order_gate,
        on_failed="  auth-expiry close cancelled — retrying",
        on_failed_hook=trade_unlock.note_close_order_failed,
    ):
        return True

    if portfolio.active_legs() or portfolio.entry_position.contracts != 0:
        trade_unlock.announce_expiry_close()
        if not trade_unlock.ensure_broker_unlocked(trade):
            LOG.warning(
                "broker trade unlock failed — retrying expiry close",
                extra={"event": "auth_expiry", "reason": "broker_unlock_failed"},
            )
        if portfolio.entry_position.contracts != 0:
            close_code = portfolio.managed_entry_code()
            row, price = _leg_trade_context(quote, quote_symbol, close_code, rows, prices)
            if _submit_forced_flat(
                cfg, trade, portfolio.entry_position, portfolio.entry_strategy, risk,
                quote_symbol, row, price, order_gate, trade_unlock,
                rule="auth",
                reason="auth expired — close before trade lock",
                order_code=close_code,
            ):
                if not order_gate.pending and portfolio.entry_position.contracts != 0:
                    trade_unlock.note_close_order_failed()
                return True
        for leg in portfolio.ahead_of_front_legs():
            row, price = _leg_trade_context(quote, quote_symbol, leg.code, rows, prices)
            if _submit_forced_flat(
                cfg, trade, leg.position, leg.strategy, risk,
                quote_symbol, row, price, order_gate, trade_unlock,
                rule="auth",
                reason="auth expired — close before trade lock",
                order_code=leg.code,
            ):
                if not order_gate.pending and leg.position.contracts != 0:
                    trade_unlock.note_close_order_failed()
                return True

    if trade_unlock.ensure_authorized(cfg, trade, position_contracts=0):
        return False

    return True


def _handle_entry_book_rollover(
    cfg: dict,
    trade: TradeClient,
    quote: QuoteClient,
    portfolio: MHIPortfolio,
    risk: RiskManager,
    quote_symbol: str,
    rows: dict[str, Any],
    prices: dict[str, float],
    order_gate: OrderGate,
    trade_unlock: TradeUnlockSession | None,
    *,
    front_contract: str | None,
) -> bool:
    """Roll the bot entry book. Returns True to skip entry strategy only."""
    rollover = portfolio.entry_rollover
    if rollover is None or not rollover.active():
        return False

    held = portfolio.entry_held_code()
    if rollover.busy():
        held = rollover.state.held_contract or held
    elif not held or not is_named_mhi_contract(held):
        return False

    quote_code = held
    if rollover.state.phase == "open" and rollover.state.target_contract:
        quote_code = rollover.state.target_contract
    row, price = _leg_trade_context(quote, quote_symbol, quote_code or quote_symbol, rows, prices)
    last_trade_time = quote.contract_last_trade_time(held) if held else None

    def _note_entry_opened(code: str) -> None:
        portfolio._note_entry_book_code(code)

    def _skip_open_if_ahead_leg(target: str, _direction: int) -> bool:
        return portfolio.ahead_leg_for_code(target) is not None

    def _absorb_ahead_leg(target: str, _direction: int) -> bool:
        if portfolio.ahead_leg_for_code(target) is None:
            return False
        portfolio.absorb_leg_into_entry(target)
        return True

    return _handle_contract_rollover(
        cfg, trade, quote, portfolio.entry_position, portfolio.entry_strategy, risk,
        quote_symbol, row, price, order_gate, trade_unlock,
        rollover,
        front_contract=front_contract,
        held_contract=held,
        last_trade_time=last_trade_time,
        on_opened=_note_entry_opened,
        rows=rows,
        prices=prices,
        rollover_open_skip=_skip_open_if_ahead_leg,
        on_rollover_open_skipped=_absorb_ahead_leg,
    )


def _handle_leg_rollovers(
    cfg: dict,
    trade: TradeClient,
    quote: QuoteClient,
    portfolio: MHIPortfolio,
    risk: RiskManager,
    quote_symbol: str,
    rows: dict[str, Any],
    prices: dict[str, float],
    order_gate: OrderGate,
    trade_unlock: TradeUnlockSession | None,
    *,
    front_contract: str | None,
) -> bool:
    """Roll front-month legs. Returns True to skip all strategy signals."""
    for leg in sorted(portfolio.active_legs(), key=lambda lg: lg.code):
        if portfolio.is_next_month_code(leg.code):
            continue
        if leg.rollover is None or not leg.rollover.active():
            continue
        row, price = _leg_trade_context(quote, quote_symbol, leg.code, rows, prices)
        last_trade_time = quote.contract_last_trade_time(leg.code)
        if _handle_contract_rollover(
            cfg, trade, quote, leg.position, leg.strategy, risk,
            quote_symbol, row, price, order_gate, trade_unlock,
            leg.rollover,
            front_contract=front_contract,
            held_contract=leg.code,
            last_trade_time=last_trade_time,
            rows=rows,
            prices=prices,
        ):
            return True
    return False


def _handle_portfolio_rollovers(
    cfg: dict,
    trade: TradeClient,
    quote: QuoteClient,
    portfolio: MHIPortfolio,
    risk: RiskManager,
    quote_symbol: str,
    rows: dict[str, Any],
    prices: dict[str, float],
    order_gate: OrderGate,
    trade_unlock: TradeUnlockSession | None,
    *,
    front_contract: str | None,
) -> tuple[bool, bool]:
    """Returns (skip_entry_strategy, skip_all_strategy)."""
    entry_skip = _handle_entry_book_rollover(
        cfg, trade, quote, portfolio, risk, quote_symbol,
        rows, prices, order_gate, trade_unlock,
        front_contract=front_contract,
    )
    leg_skip = _handle_leg_rollovers(
        cfg, trade, quote, portfolio, risk, quote_symbol,
        rows, prices, order_gate, trade_unlock,
        front_contract=front_contract,
    )
    return entry_skip, leg_skip


def _portfolio_tags(portfolio: MHIPortfolio, order_gate: OrderGate) -> str:
    tags = ""
    if portfolio.entry_strategy.cooldown.locked:
        tags += " [COOLDOWN]"
    if portfolio.entry_strategy.panic_guard.active:
        tags += " [PANIC_PAUSE]"
    if order_gate.pending:
        tags += " [ORDER_PENDING]"
    if (
        (portfolio.entry_rollover is not None and portfolio.entry_rollover.busy())
        or any(
            leg.rollover is not None and leg.rollover.busy()
            for leg in portfolio.legs.values()
        )
    ):
        tags += " [ROLLOVER]"
    return tags


def _print_portfolio_status(
    quote_symbol: str,
    prices: dict[str, float],
    portfolio: MHIPortfolio,
    status: str,
    update_time: str | None,
    *,
    diff: float | None = None,
    total_change: float | None = None,
) -> None:
    main_px = prices.get(quote_symbol, 0.0)
    leg_bits = []
    if portfolio.entry_position.contracts != 0:
        entry_code = portfolio.managed_entry_code()
        entry_px = prices.get(entry_code, main_px)
        pnl = live_pnl_points(portfolio.entry_strategy, portfolio.entry_position, entry_px)
        pnl_s = f" P/L{pnl:+.0f}" if pnl is not None else ""
        front_label = contract_log_label(entry_code)
        leg_bits.append(f"{front_label} {portfolio.entry_position.contracts:+d}{pnl_s}")
    for leg in portfolio.ahead_of_front_legs():
        px = prices.get(leg.code, main_px)
        pnl = live_pnl_points(leg.strategy, leg.position, px)
        pnl_s = f" P/L{pnl:+.0f}" if pnl is not None else ""
        leg_bits.append(f"{contract_log_label(leg.code)} {leg.position.contracts:+d}{pnl_s}")
    legs_tag = (" | " + ", ".join(leg_bits)) if leg_bits else " flat"
    ts = update_time or datetime.now().strftime("%H:%M:%S")
    move_tag = ""
    if diff is not None and total_change is not None:
        move_tag = f" {diff:+.0f}|{total_change:+.0f}"
    LOG.info(
        f"[{ts}] {main_px:.0f}{legs_tag}{move_tag} — {_compact_signal(status)}",
        extra={
            "event": "poll",
            "symbol": quote_symbol,
            "price": main_px,
            "legs": {
                **(
                    {portfolio.managed_entry_code(): portfolio.entry_position.contracts}
                    if portfolio.entry_position.contracts != 0
                    else {}
                ),
                **{leg.code: leg.position.contracts for leg in portfolio.ahead_of_front_legs()},
            },
            "data_time": update_time,
            "signal": status,
        },
    )


def execute_unit_order(
    cfg: dict,
    trade: TradeClient,
    position: UnitPositionBook,
    action: Action,
    symbol: str,
    quote_row=None,
    *,
    trade_unlock: TradeUnlockSession | None = None,
    order_code: str | None = None,
    risk: RiskManager | None = None,
    portfolio_total_signed: int | None = None,
) -> dict:
    code = order_code or symbol
    ok, reason = position.validate_transition(action)
    if not ok:
        return {"status": "rejected", "reason": reason}

    side = position.resolve_order_side(action)
    if side is None:
        return {"status": "skipped", "reason": "HOLD"}

    order_qty = position.order_qty(action)

    if risk is not None:
        if portfolio_total_signed is not None:
            risk.position_shares = portfolio_total_signed
        ok, cap_reason = risk.approve_order(side, order_qty)
        if not ok:
            return {"status": "rejected", "reason": cap_reason}

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
        code=code,
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
) -> None:
    _print_position_snapshot(
        symbol,
        strategy,
        position,
        live_price,
        title="Startup",
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
    parser.add_argument(
        "--log-file",
        default=None,
        help="Log file path (default: logging.file in config, logs/mhimain.jsonl)",
    )
    parser.add_argument(
        "--no-log-file",
        action="store_true",
        help="Disable file logging (stdout/journal only)",
    )
    args = parser.parse_args()

    cfg = load_config(args.config)
    log_path = setup_logging(
        cfg,
        level=args.log_level,
        fmt=args.log_format,
        log_file=args.log_file,
        no_log_file=args.no_log_file,
    )
    if log_path is not None:
        LOG.info(
            "file logging enabled",
            extra={"event": "logging", "log_file": str(log_path)},
        )
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

    portfolio = MHIPortfolio.create(cfg, trend=trend, quote_symbol=symbol)
    risk = RiskManager({**cfg, "risk": {**cfg.get("risk", {}), "max_position_shares": 8}})

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

        front_contract: str | None = None
        if is_continuous_mhi(symbol):
            ok_front, front_contract = quote.resolve_front_contract(symbol)
            if ok_front and front_contract:
                LOG.info(
                    "front-month contract resolved",
                    extra={
                        "event": "contract_rollover",
                        "quote_symbol": symbol,
                        "front_contract": front_contract,
                    },
                )
        _sync_front_context(portfolio, quote, symbol, front_contract)

        sub_symbols = portfolio.quote_symbols()
        sub_ok, sub_msg = quote.subscribe_quote(sub_symbols)
        if not sub_ok:
            LOG.warning(
                "quote subscribe failed",
                extra={"event": "quote_subscribe", "symbols": sub_symbols, "detail": str(sub_msg)},
            )

        ret_ok, quote_data = quote.quote(sub_symbols)
        prices, rows, _ = parse_quote_batch(quote_data if ret_ok else None, sub_symbols)
        quote_price = prices.get(symbol)
        if quote_price is None:
            LOG.warning("no initial quote — waiting for live feed", extra={"event": "quote", "symbol": symbol})

        broker_legs = fetch_broker_mhi_legs(trade, symbol, cfg, quote_prices=prices)
        ok_ma, ma5 = quote.daily_ma(symbol, period=ma_period)
        portfolio.bootstrap_from_broker(broker_legs, risk, ma5=ma5, front_contract=front_contract)
        if ok_ma and ma5 is not None:
            LOG.info(f"MA{ma_period} (daily): {ma5:.1f}", extra={"event": "ma", "ma_period": ma_period, "ma": ma5})
        elif trend == TrendMode.UNCERTAIN:
            LOG.warning(
                f"could not fetch MA{ma_period}; uncertain entries need MA",
                extra={"event": "ma", "ma_period": ma_period, "ok": False},
            )

        if portfolio.entry_position.contracts != 0:
            _print_position_snapshot(
                symbol,
                portfolio.entry_strategy,
                portfolio.entry_position,
                quote_price,
                title=f"Startup {contract_log_label(front_contract)}",
            )
        for leg in portfolio.ahead_of_front_legs():
            _print_position_snapshot(
                symbol,
                leg.strategy,
                leg.position,
                prices.get(leg.code, quote_price),
                title=f"Startup {contract_log_label(leg.code)} (next month)",
            )
        if portfolio.is_flat():
            _print_bootstrap(symbol, portfolio.entry_strategy, portfolio.entry_position, quote_price)

        order_gate = OrderGate()
        poll_display = PollDisplayGate(threshold_pts=poll_display_threshold)
        if quote_price is not None:
            poll_display.ref_price = quote_price

        if (
            portfolio.is_flat()
            and not risk.killed
            and quote_price is not None
        ):
            (
                launch_pos,
                launch_strat,
                launch_order,
                launch_trade_row,
                launch_price,
                launch_row,
            ) = _flat_entry_context(
                portfolio, front_contract, quote, symbol, rows, prices
            )
            if not launch_strat.cooldown.locked:
                launch_signal = launch_strat.update(launch_price, launch_pos)
                if launch_signal.action != Action.HOLD:
                    launch_data_time = str(launch_row.get("data_time", "")) if launch_row is not None else ""
                    launch_fresh = assess_quote_freshness(
                        cfg,
                        poll_interval,
                        last_successful_poll_at=None,
                        data_time=launch_data_time,
                    )
                    if launch_fresh.block_entries and is_new_entry(launch_pos.contracts, launch_signal.action):
                        _log_quote_stale(launch_fresh, event="launch_blocked_stale")
                    else:
                        LOG.info(
                            f"Launch: {launch_signal.action.value} {contract_log_label(launch_order)} — {launch_signal.reason}",
                            extra={
                                "event": "launch",
                                "action": launch_signal.action.value,
                                "contract": contract_log_label(launch_order),
                                "order_code": launch_order,
                                "reason": launch_signal.reason,
                            },
                        )
                        _process_signal(
                            cfg, trade, launch_pos, launch_strat, risk,
                            launch_signal, symbol, launch_trade_row, launch_price, order_gate, trade_unlock,
                            quote_freshness=launch_fresh,
                            order_code=launch_order,
                            front_contract=portfolio.front_contract,
                            front_last_trade_time=portfolio.front_last_trade_time,
                        )

        count = 0
        last_poll_at = None
        last_live_price: float | None = quote_price
        last_prices = dict(prices)
        last_rows = dict(rows)
        last_position_refresh = time.monotonic()
        try:
            while args.iterations is None or count < args.iterations:
                poll_symbols = portfolio.quote_symbols()
                new_syms = portfolio.detect_new_subscriptions(poll_symbols)
                if new_syms:
                    quote.subscribe_quote(new_syms)

                ret_ok, data = quote.quote(poll_symbols)
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
                    now_mono = time.monotonic()
                    if now_mono - last_position_refresh >= position_refresh_sec:
                        _refresh_account_equity(trade, cfg, risk)
                        last_position_refresh = now_mono
                    if last_live_price is not None and (
                        risk.killed
                        or (trade_unlock is not None and trade_unlock.is_expired())
                    ):
                        if _handle_kill_switch_portfolio(
                            cfg, trade, portfolio, risk, quote, symbol,
                            last_rows, last_prices, order_gate, trade_unlock,
                        ):
                            time.sleep(poll_interval)
                            count += 1
                            continue
                        if trade_unlock is not None and _handle_auth_expiry_portfolio(
                            cfg, trade, portfolio, risk, quote, symbol,
                            last_rows, last_prices, order_gate, trade_unlock,
                        ):
                            time.sleep(poll_interval)
                            count += 1
                            continue
                        if risk.killed and portfolio.is_flat() and not order_gate.pending:
                            LOG.error(
                                "kill switch halt",
                                extra={"event": "kill_switch_halt", "reason": risk.kill_reason},
                            )
                            break
                    time.sleep(poll_interval)
                    count += 1
                    continue

                poll_now = datetime.now(timezone.utc)
                prev_poll_at = last_poll_at

                if trade_unlock is not None:
                    trade_unlock.on_opend_recovered(trade)

                prices, rows, data_times = parse_quote_batch(data, poll_symbols)
                price = prices.get(symbol)
                if price is None and prices:
                    price = next(iter(prices.values()))
                last_live_price = price
                last_prices = dict(prices)
                last_rows = dict(rows)
                main_row = rows.get(symbol)
                update_time = data_times.get(symbol, "")
                quote_fresh = assess_quote_freshness(
                    cfg,
                    poll_interval,
                    last_successful_poll_at=prev_poll_at,
                    data_time=update_time,
                    now=poll_now,
                )
                last_poll_at = poll_now
                now_mono = time.monotonic()
                if quote_fresh.block_entries:
                    _log_quote_stale(quote_fresh, event="quote_stale")

                if is_continuous_mhi(symbol):
                    ok_front, resolved_front = quote.resolve_front_contract(symbol)
                    if ok_front and resolved_front:
                        front_contract = resolved_front
                _sync_front_context(portfolio, quote, symbol, front_contract)

                if (
                    portfolio.entry_position.contracts == 0
                    and not order_gate.pending
                    and (trade_unlock is None or not trade_unlock.is_expired())
                ):
                    broker_legs = fetch_broker_mhi_legs(trade, symbol, cfg, quote_prices=prices)
                    portfolio.refresh_from_broker(broker_legs, risk)

                entry_roll_skip, leg_roll_skip = _handle_portfolio_rollovers(
                    cfg, trade, quote, portfolio, risk, symbol,
                    rows, prices, order_gate, trade_unlock,
                    front_contract=front_contract,
                )
                if leg_roll_skip:
                    time.sleep(poll_interval)
                    count += 1
                    continue

                if now_mono - last_position_refresh >= position_refresh_sec:
                    if (
                        not order_gate.pending
                        and (trade_unlock is None or not trade_unlock.is_expired())
                    ):
                        broker_legs = fetch_broker_mhi_legs(trade, symbol, cfg, quote_prices=prices)
                        changed_codes = portfolio.refresh_from_broker(broker_legs, risk)
                        for code in changed_codes:
                            if code == portfolio.managed_entry_code():
                                _print_position_snapshot(
                                    symbol,
                                    portfolio.entry_strategy,
                                    portfolio.entry_position,
                                    prices.get(code, prices.get(symbol, price)),
                                    title=f"Refresh {contract_log_label(code)}",
                                )
                                continue
                            leg = portfolio.leg_for_code(code)
                            if leg is not None:
                                _print_position_snapshot(
                                    symbol,
                                    leg.strategy,
                                    leg.position,
                                    prices.get(code, price),
                                    title=f"Refresh {contract_log_label(code)}",
                                )
                    _refresh_account_equity(trade, cfg, risk)
                    last_position_refresh = now_mono

                if _handle_kill_switch_portfolio(
                    cfg, trade, portfolio, risk, quote, symbol,
                    rows, prices, order_gate, trade_unlock,
                ):
                    time.sleep(poll_interval)
                    count += 1
                    continue

                if trade_unlock is not None and _handle_auth_expiry_portfolio(
                    cfg, trade, portfolio, risk, quote, symbol,
                    rows, prices, order_gate, trade_unlock,
                ):
                    time.sleep(poll_interval)
                    count += 1
                    continue

                status = "flat"
                if portfolio.is_flat():
                    if entry_roll_skip:
                        time.sleep(poll_interval)
                        count += 1
                        continue
                    (
                        entry_pos,
                        entry_strat,
                        entry_order,
                        entry_trade_row,
                        entry_price,
                        entry_row,
                    ) = _flat_entry_context(
                        portfolio, front_contract, quote, symbol, rows, prices
                    )
                    signal = entry_strat.update(entry_price, entry_pos)
                    if entry_strat.consume_panic_trigger():
                        _alert_panic_pause(entry_strat, entry_price)
                    cooldown_tag = _portfolio_tags(portfolio, order_gate) + quote_fresh.status_tag
                    status = signal.reason
                    if signal.action != Action.HOLD:
                        status = f"{contract_log_label(entry_order)} {signal.action.value}: {signal.reason}"
                    show_poll, diff = poll_display.note_price(price)
                    if show_poll:
                        _print_portfolio_status(
                            symbol, prices, portfolio, status + cooldown_tag, update_time,
                            diff=diff, total_change=poll_display.total_change,
                        )
                    if risk.killed and portfolio.is_flat() and not order_gate.pending:
                        LOG.error(
                            "kill switch halt",
                            extra={"event": "kill_switch_halt", "reason": risk.kill_reason},
                        )
                        break
                    _process_signal(
                        cfg, trade, entry_pos, entry_strat, risk,
                        signal, symbol, entry_trade_row, entry_price, order_gate, trade_unlock,
                        quote_freshness=quote_fresh,
                        order_code=entry_order,
                        front_contract=portfolio.front_contract,
                        front_last_trade_time=portfolio.front_last_trade_time,
                        portfolio=portfolio,
                    )
                    portfolio._note_entry_book_code(
                        entry_order if portfolio.entry_position.contracts != 0 else None
                    )
                else:
                    if portfolio.entry_position.contracts != 0 and not entry_roll_skip:
                        entry_order = portfolio.managed_entry_code()
                        entry_trade_row, entry_price = _leg_trade_context(
                            quote, symbol, entry_order, rows, prices
                        )
                        signal = portfolio.entry_strategy.update(entry_price, portfolio.entry_position)
                        if portfolio.entry_strategy.consume_panic_trigger():
                            _alert_panic_pause(portfolio.entry_strategy, entry_price)
                        leg_status = signal.reason
                        if signal.action != Action.HOLD:
                            leg_status = (
                                f"{contract_log_label(entry_order)} "
                                f"{signal.action.value}: {signal.reason}"
                            )
                        status = leg_status
                        cooldown_tag = _portfolio_tags(portfolio, order_gate)
                        show_poll, diff = poll_display.note_price(price)
                        if show_poll:
                            _print_portfolio_status(
                                symbol, prices, portfolio, leg_status + cooldown_tag, update_time,
                                diff=diff, total_change=poll_display.total_change,
                            )
                        if risk.killed and portfolio.is_flat() and not order_gate.pending:
                            LOG.error(
                                "kill switch halt",
                                extra={"event": "kill_switch_halt", "reason": risk.kill_reason},
                            )
                            break
                        _process_signal(
                            cfg, trade, portfolio.entry_position, portfolio.entry_strategy, risk,
                            signal, symbol, entry_trade_row, entry_price, order_gate, trade_unlock,
                            quote_freshness=quote_fresh,
                            order_code=entry_order,
                            front_contract=portfolio.front_contract,
                            front_last_trade_time=portfolio.front_last_trade_time,
                            portfolio=portfolio,
                        )
                        portfolio._note_entry_book_code(
                            entry_order if portfolio.entry_position.contracts != 0 else None
                        )
                        if order_gate.pending:
                            time.sleep(poll_interval)
                            count += 1
                            continue

                    for leg in portfolio.ahead_of_front_legs():
                        leg_trade_row, leg_price = _leg_trade_context(
                            quote, symbol, leg.code, rows, prices
                        )
                        signal = leg.strategy.update(leg_price, leg.position)
                        if leg.strategy.consume_panic_trigger():
                            _alert_panic_pause(leg.strategy, leg_price)
                        if signal.action != Action.HOLD and not _allows_next_month_close(
                            leg.position, signal.action
                        ):
                            LOG.info(
                                "next-month entry blocked — manual opens only",
                                extra={
                                    "event": "next_month_entry_blocked",
                                    "contract": leg.code,
                                    "action": signal.action.value,
                                },
                            )
                            continue
                        leg_status = signal.reason
                        if signal.action != Action.HOLD:
                            leg_status = (
                                f"{contract_log_label(leg.code)} "
                                f"{signal.action.value}: {signal.reason}"
                            )
                        status = leg_status
                        cooldown_tag = _portfolio_tags(portfolio, order_gate)
                        show_poll, diff = poll_display.note_price(price)
                        if show_poll:
                            _print_portfolio_status(
                                symbol, prices, portfolio, leg_status + cooldown_tag, update_time,
                                diff=diff, total_change=poll_display.total_change,
                            )
                        if risk.killed and portfolio.is_flat() and not order_gate.pending:
                            LOG.error(
                                "kill switch halt",
                                extra={"event": "kill_switch_halt", "reason": risk.kill_reason},
                            )
                            break
                        if signal.action == Action.HOLD:
                            continue
                        _process_signal(
                            cfg, trade, leg.position, leg.strategy, risk,
                            signal, symbol, leg_trade_row, leg_price, order_gate, trade_unlock,
                            order_code=leg.code,
                            portfolio=portfolio,
                        )
                        if order_gate.pending:
                            break

                time.sleep(poll_interval)
                count += 1
        except KeyboardInterrupt:
            LOG.info(
                "stopped",
                extra={"event": "shutdown", "final_contracts": portfolio.total_signed_contracts()},
            )


if __name__ == "__main__":
    main()
