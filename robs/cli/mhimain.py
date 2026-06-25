#!/usr/bin/env python3
"""Standalone HK.MHImain trader — front-month bot entries and optional next-month legs."""

from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, date, timezone
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
    HKEXFrontCalendar,
    HKEXMHISpot,
    contract_log_label,
    hkex_spot_startup_message,
    is_continuous_mhi,
    is_hk_mhi_product_code,
    is_ltd_expiring_month_open_banned,
    is_named_mhi_contract,
    local_contract_last_trade_time,
    resolve_rollover_target,
    should_rollover,
)
from robs.execution.market_guard import allow_market_order
from robs.execution.mhi_portfolio import (
    BrokerPositionChange,
    MHIPortfolio,
    parse_quote_batch,
)
from robs.execution.order_gate import OrderGate
from robs.execution.position import UnitPositionBook
from robs.execution.position_sync import (
    BrokerPosition,
    fetch_broker_mhi_legs,
    fetch_broker_position,
    fetch_broker_position_for_code,
    live_pnl_points,
)
from robs.execution.quote_staleness import (
    QuoteFreshness,
    assess_quote_freshness,
    is_new_entry,
    parse_quote_data_time,
    stale_threshold_sec,
)
from robs.execution.risk import RiskManager
from robs.execution.trade_unlock import (
    TradeUnlockSession,
    maybe_create_unlock_session,
    warn_short_trade_unlock_window,
)
from robs.log_config import format_hk_log_ts, setup_logging
from robs.strategy.mhimain import MHImainStrategy
from robs.strategy.rules import Action, Signal
from robs.strategy.trend import TrendMode, prompt_trend_mode

LOG = logging.getLogger("robs.mhimain")


def _sync_front_context(
    portfolio: MHIPortfolio,
    symbol: str,
    spot: HKEXMHISpot | None,
) -> None:
    if is_continuous_mhi(symbol) and spot:
        portfolio.update_front_context(spot.front, spot.front_ltd)
    else:
        portfolio.update_front_context(None, None)


def _seed_hkex_calendar_ltd(quote: QuoteClient, calendar: HKEXFrontCalendar) -> None:
    """One OpenD fetch at startup for front+next LTD; calendar handles the rest."""
    spot = calendar.spot()
    if not spot:
        return
    raw = quote.seed_mhi_ltd_overrides([spot.front, spot.next])
    overrides = {k: v for k, v in raw.items() if isinstance(v, date)}
    if overrides:
        calendar.merge_ltd_overrides(overrides)
        LOG.info(
            "HKEX calendar LTD seeded from OpenD",
            extra={
                "event": "contract_rollover",
                "codes": list(overrides.keys()),
            },
        )


def _log_hkex_calendar(spot: HKEXMHISpot) -> None:
    msg = hkex_spot_startup_message(spot)
    LOG.info(
        msg,
        extra={
            "event": "contract_rollover",
            "front_contract": spot.front,
            "next_contract": spot.next,
            "front_ltd": spot.front_ltd,
            "next_ltd": spot.next_ltd,
        },
    )


def _refresh_hkex_spot_if_changed(
    quote: QuoteClient,
    calendar: HKEXFrontCalendar,
    last_key: tuple[str, str] | None,
    *,
    seed_ltd: bool = False,
) -> tuple[HKEXMHISpot | None, tuple[str, str] | None]:
    """Resolve spot; log calendar line when front/next changes (e.g. after LTD flip)."""
    if seed_ltd:
        _seed_hkex_calendar_ltd(quote, calendar)
    spot = calendar.spot()
    if spot is None:
        return None, last_key
    key = (spot.front, spot.next)
    if key == last_key:
        return spot, last_key
    if last_key is not None:
        _seed_hkex_calendar_ltd(quote, calendar)
        spot = calendar.spot()
        if spot is None:
            return None, key
        key = (spot.front, spot.next)
    _log_hkex_calendar(spot)
    return spot, key


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
    contract: str | None = None,
    pos_after: int | None = None,
    cum_pnl: float | None = None,
    compact_fill: bool = False,
) -> str:
    q = int(qty)
    contract_tag = f" {contract_log_label(contract)}" if contract else ""
    if result.get("status") in ("rejected", "skipped") or not result.get("ok"):
        reason = str(result.get("reason") or result.get("error") or "failed")
        if "market blocked" in reason:
            text = f"{side} x{q}{contract_tag} blocked (slippage)"
        else:
            short = reason if len(reason) <= 60 else reason[:57] + "..."
            text = f"{side} x{q}{contract_tag} rejected: {short}"
    elif result.get("filled") or result.get("status", "").upper().startswith("FILLED"):
        if compact_fill:
            oid = result.get("order_id")
            oid_tag = f" #{oid}" if oid else ""
            text = f"filled{oid_tag} @ {price:.0f}"
        else:
            text = f"{side} x{q}{contract_tag} filled @ {price:.0f}"
    else:
        oid = result.get("order_id") or "?"
        text = f"{side} x{q}{contract_tag} pending #{oid}"
    if pos_after is not None:
        text += f" → pos={pos_after:+d}"
    if cum_pnl is not None and not compact_fill:
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
    compact_fill: bool = False,
) -> None:
    LOG.info(
        _order_line(
            side, qty, price, result,
            contract=contract,
            pos_after=pos_after,
            cum_pnl=cum_pnl,
            compact_fill=compact_fill,
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
class _PollLegState:
    ref_price: float | None = None
    total_change: float = 0.0


@dataclass
class PollDisplayGate:
    """Per-contract poll gates: emit when any tracked code moves threshold pts."""

    threshold_pts: float
    _legs: dict[str, _PollLegState] = field(default_factory=dict)

    def seed(self, code: str, price: float) -> None:
        leg = self._legs.setdefault(code, _PollLegState())
        if leg.ref_price is None:
            leg.ref_price = price

    def note_prices(
        self,
        prices: dict[str, float],
        codes: list[str],
    ) -> tuple[bool, dict[str, float], dict[str, float]]:
        """Update all tracked codes; return True if any crossed the threshold."""
        triggered = False
        diffs: dict[str, float] = {}
        totals: dict[str, float] = {}
        for code in codes:
            px = prices.get(code)
            if px is None:
                continue
            show, diff = self._note_one(code, float(px))
            if show:
                triggered = True
                diffs[code] = diff
                totals[code] = self._legs[code].total_change
        return triggered, diffs, totals

    def _note_one(self, code: str, price: float) -> tuple[bool, float]:
        leg = self._legs.setdefault(code, _PollLegState())
        if leg.ref_price is None:
            leg.ref_price = price
            return False, 0.0
        diff = price - leg.ref_price
        if abs(diff) < self.threshold_pts:
            return False, diff
        leg.total_change += diff
        leg.ref_price = price
        return True, diff


def _poll_display_codes(portfolio: MHIPortfolio, quote_symbol: str) -> list[str]:
    """MHImain plus broker front and each named month we quote."""
    codes = [quote_symbol]
    front = portfolio.front_contract
    if front and is_named_mhi_contract(front) and front not in codes:
        codes.append(front)
    for code in portfolio._quoted_month_codes():
        if code not in codes:
            codes.append(code)
    return codes


def _resolve_contract_poll_price(
    prices: dict[str, float],
    code: str,
    quote_symbol: str,
    front_contract: str | None,
) -> float | None:
    """Price for poll display; front month may use MHImain when only continuous is quoted."""
    if code in prices:
        return float(prices[code])
    if (
        is_named_mhi_contract(code)
        and front_contract
        and code.upper() == front_contract.upper()
        and quote_symbol in prices
    ):
        return float(prices[quote_symbol])
    if is_named_mhi_contract(code):
        return None
    return prices.get(quote_symbol)


def _poll_prices_for_display(
    prices: dict[str, float],
    quote_symbol: str,
    portfolio: MHIPortfolio,
    codes: list[str],
) -> dict[str, float]:
    front = portfolio.front_contract
    out = dict(prices)
    for code in codes:
        if code in out:
            continue
        px = _resolve_contract_poll_price(prices, code, quote_symbol, front)
        if px is not None:
            out[code] = px
    return out


def _poll_update_time(
    data_times: dict[str, str],
    quote_symbol: str,
    portfolio: MHIPortfolio,
) -> str:
    """Prefer quote data_time from front month, then MHImain."""
    front = portfolio.front_contract
    if front and data_times.get(front):
        return data_times[front]
    if data_times.get(quote_symbol):
        return data_times[quote_symbol]
    for code in portfolio._quoted_month_codes():
        if data_times.get(code):
            return data_times[code]
    return ""


def _poll_contract_short(code: str | None) -> str:
    """MHImain → main, HK.MHI2606 / MHI2606 → 2606."""
    if not code:
        return "?"
    label = contract_log_label(code)
    if label == "MHImain":
        return "main"
    if label.startswith("MHI") and label != "MHImain":
        return label[3:]
    return label


def _extract_bracket_tags(status: str) -> tuple[str, str]:
    core = status.strip()
    tags = "".join(re.findall(r" \[[^\]]+\]", core))
    core = re.sub(r" \[[^\]]+\]", "", core).strip()
    return core, tags


def _compact_poll_signal(status: str) -> str:
    """Short poll action line; bracket tags (COOLDOWN, etc.) preserved at end."""
    core, tags = _extract_bracket_tags(status)
    if not core:
        return tags.strip()

    if " FLAT: take profit" in core:
        leg, rest = core.split(" FLAT: take profit", 1)
        leg_s = _poll_contract_short(leg.strip())
        now_m = re.search(r"\(now ([+-]?\d+)", rest)
        pts = now_m.group(1) if now_m else ""
        return f"{leg_s} TP {pts}{tags}" if pts else f"{leg_s} TP{tags}"

    if " FLAT: cut loss" in core:
        leg, rest = core.split(" FLAT: cut loss", 1)
        leg_s = _poll_contract_short(leg.strip())
        now_m = re.search(r"\(now ([+-]?\d+)", rest)
        pts = now_m.group(1) if now_m else ""
        return f"{leg_s} cut {pts}{tags}" if pts else f"{leg_s} cut{tags}"

    for action in ("BUY", "SELL", "FLAT"):
        token = f" {action}:"
        if token in core:
            leg, reason = core.split(token, 1)
            leg_s = _poll_contract_short(leg.strip())
            short_reason = reason.strip().split(" (", 1)[0]
            if len(short_reason) > 48:
                short_reason = short_reason[:45] + "..."
            return f"{leg_s} {action} {short_reason}{tags}"

    if core.startswith("holding "):
        return f"hold{tags}"

    if core.startswith("flat, waiting entry"):
        return f"wait entry{tags}"

    if core == "flat":
        return f"flat{tags}"

    if core == "order pending":
        return f"wait fill{tags}"

    compact = _compact_signal(core)
    return f"{compact}{tags}" if compact else tags.strip()


def _format_poll_move(diffs: dict[str, float] | None, code: str) -> str:
    if not diffs or code not in diffs:
        return ""
    return f"Δ{diffs[code]:+.0f}"


def _status_contract_code(status: str) -> str | None:
    core, _ = _extract_bracket_tags(status)
    m = re.match(r"^(MHI\w+)", core.strip())
    if not m:
        return None
    label = m.group(1)
    if label == "MHImain":
        return "HK.MHImain"
    return f"HK.{label}"


def _poll_signal_for_contract(status: str, contract_code: str, *, main_line: bool = False) -> str:
    target = _status_contract_code(status)
    if target is not None:
        if target.upper() != contract_code.upper():
            return ""
    elif not main_line:
        return ""
    text = _compact_poll_signal(status)
    return text


def _has_next_month_contracts(portfolio: MHIPortfolio) -> bool:
    if portfolio.entry_position.contracts != 0:
        if portfolio.is_next_month_code(portfolio.managed_entry_code()):
            return True
    return any(
        leg.position.contracts != 0 for leg in portfolio.ahead_of_front_legs()
    )


def _next_month_poll_code(portfolio: MHIPortfolio) -> str | None:
    if not _has_next_month_contracts(portfolio):
        return None
    for leg in portfolio.ahead_of_front_legs():
        if leg.position.contracts != 0:
            return leg.code
    entry_code = portfolio.managed_entry_code()
    if portfolio.entry_position.contracts != 0 and portfolio.is_next_month_code(entry_code):
        return entry_code
    return None


def _poll_contract_context(
    portfolio: MHIPortfolio,
    code: str,
) -> tuple[MHImainStrategy, UnitPositionBook, bool]:
    """Strategy, position, and watch flag for one poll row."""
    entry_code = (
        portfolio.managed_entry_code()
        if portfolio.entry_position.contracts != 0
        else None
    )
    if entry_code and code.upper() == entry_code.upper():
        return (
            portfolio.entry_strategy,
            portfolio.entry_position,
            portfolio.entry_position.contracts == 0,
        )
    leg = portfolio.leg_for_code(code)
    if leg is not None and leg.position.contracts != 0:
        return leg.strategy, leg.position, False
    return portfolio.entry_strategy, portfolio.entry_position, True


def _poll_month_label(code: str) -> str:
    """HK.MHI2606 → MHI2606, HK.MHImain → MHImain."""
    return contract_log_label(code) or "?"


def _front_month_poll_code(portfolio: MHIPortfolio, quote_symbol: str) -> str:
    if portfolio.entry_position.contracts != 0:
        entry_code = portfolio.managed_entry_code()
        if not portfolio.is_next_month_code(entry_code):
            return entry_code
    front = portfolio.front_contract
    if front and is_named_mhi_contract(front):
        return front
    return quote_symbol


def _poll_exit_prices(strategy: MHImainStrategy, position_sign: int) -> tuple[str, str]:
    """Cut-loss and take-profit price levels from entry and config."""
    entry = strategy.entry_price
    if entry is None or position_sign == 0:
        return "-", "-"
    bl = strategy.pnl_baseline
    cut_pnl = bl.cut_loss_trigger()
    tp_pnl = bl.take_profit_trigger()
    if position_sign > 0:
        cut_px = entry + cut_pnl
        tp_px = entry + tp_pnl
    else:
        cut_px = entry - cut_pnl
        tp_px = entry - tp_pnl
    return f"{cut_px:.0f}", f"{tp_px:.0f}"


def _format_poll_contract_pl_line(
    code: str,
    strategy: MHImainStrategy,
    position: UnitPositionBook,
    prices: dict[str, float],
    quote_symbol: str,
    main_px: float | None,
    *,
    front_contract: str | None = None,
    watch: bool = False,
) -> str:
    """MHIyymm entry, current, P/L, cut-loss price, take-profit price."""
    label = _poll_month_label(code)
    px = _resolve_contract_poll_price(prices, code, quote_symbol, front_contract)
    if px is None and not is_named_mhi_contract(code):
        px = main_px
    current_s = f"{px:.0f}" if px is not None else "-"

    if watch or position.contracts == 0:
        entry_s = "-"
        pos_s = _position_label(0)
        pnl_s = "0" if watch else "-"
        cut_s, tp_s = "-", "-"
    else:
        entry = strategy.entry_price
        entry_s = f"{entry:.0f}" if entry is not None else "-"
        pos_s = _position_label(position.contracts)
        pnl = live_pnl_points(strategy, position, px) if px is not None else None
        pnl_s = f"{pnl:+.0f}" if pnl is not None else "-"
        cut_s, tp_s = _poll_exit_prices(strategy, position.position)

    return (
        f"{label} ent {entry_s} {pos_s}, last {current_s}, pnl {pnl_s}, cut {cut_s}, tp {tp_s}"
    )


def _poll_log_ts(update_time: str | None) -> str:
    """HK timestamp for poll logs (from quote data_time when available)."""
    parsed = parse_quote_data_time(update_time or "")
    if parsed is not None:
        return format_hk_log_ts(parsed)
    return format_hk_log_ts()


def _build_poll_status_lines(
    quote_symbol: str,
    prices: dict[str, float],
    portfolio: MHIPortfolio,
    update_time: str | None,
) -> list[str]:
    del update_time
    front_contract = portfolio.front_contract
    main_px = prices.get(quote_symbol)
    lines: list[str] = []

    front_code = _front_month_poll_code(portfolio, quote_symbol)
    strat, pos, watch = _poll_contract_context(portfolio, front_code)
    lines.append(
        _format_poll_contract_pl_line(
            front_code,
            strat,
            pos,
            prices,
            quote_symbol,
            main_px,
            front_contract=front_contract,
            watch=watch,
        )
    )

    next_code = _next_month_poll_code(portfolio)
    if next_code is None or next_code.upper() == front_code.upper():
        return lines

    strat, pos, watch = _poll_contract_context(portfolio, next_code)
    lines.append(
        _format_poll_contract_pl_line(
            next_code,
            strat,
            pos,
            prices,
            quote_symbol,
            main_px,
            front_contract=front_contract,
            watch=watch,
        )
    )
    return lines


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
            compact = order_gate.logged_submit
            oid = order_gate.order_id
            _finalize_filled_order(strategy, risk, position, price, order_gate)
            _sync_portfolio_risk()
            _log_order(
                side, qty, price,
                {"ok": True, "filled": True, "status": "FILLED", "order_id": oid},
                contract=contract,
                pos_after=position.contracts,
                cum_pnl=None if compact else _session_pnl_pts(strategy, position, price),
                compact_fill=compact,
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

    if is_hk_mhi_product_code(trade_code):
        broker_before = fetch_broker_position_for_code(
            trade, trade_code, cfg, quote_price=price,
        )
    else:
        broker_before = fetch_broker_position(trade, symbol, cfg, quote_price=price)
    order_gate.mark_submitted(
        order_id=result.get("order_id"),
        side=side,
        signal_action=signal.action,
        qty=float(result.get("qty", order_qty)),
        order_code=trade_code,
        local_contracts_at_submit=position.contracts,
        broker_contracts_at_submit=broker_before.contracts,
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
    order_gate.logged_submit = True


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
        compact = order_gate.logged_submit
        oid = order_gate.order_id
        _finalize_filled_order(strategy, risk, position, price, order_gate)
        _log_order(
            side, qty, price,
            {"ok": True, "filled": True, "status": "FILLED", "order_id": oid},
            contract=contract,
            pos_after=position.contracts,
            cum_pnl=None if compact else _session_pnl_pts(strategy, position, price),
            compact_fill=compact,
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
        compact = order_gate.logged_submit
        oid = order_gate.order_id
        _finalize_filled_order(strategy, risk, position, price, order_gate)
        _log_order(
            side, qty, price,
            {"ok": True, "filled": True, "status": "FILLED", "order_id": oid},
            contract=contract,
            pos_after=position.contracts,
            cum_pnl=None if compact else _session_pnl_pts(strategy, position, price),
            compact_fill=compact,
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
    ltd_overrides: dict[str, date] | None = None,
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
            ltd_overrides=ltd_overrides,
        )
        if not do_roll:
            return False
        target = resolve_rollover_target(
            held,
            front_contract,
            last_trade_time=last_trade_time,
            now=datetime.now(HK),
            ltd_overrides=ltd_overrides,
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


def _lookup_quote_row(rows: dict[str, Any], *keys: str) -> Any | None:
    """First matching quote row; avoids `series or …` truthiness on pandas Series."""
    for key in keys:
        if key in rows:
            return rows[key]
    return None


def _resolve_trade_price(
    prices: dict[str, float],
    code: str,
    quote_symbol: str,
    front_contract: str | None,
) -> float | None:
    """Trade price for a contract; None when no valid quote (never use 0)."""
    px = _resolve_contract_poll_price(prices, code, quote_symbol, front_contract)
    if px is None or px <= 0:
        return None
    return float(px)


def _lookup_quote_price(prices: dict[str, float], *keys: str, default: float = 0.0) -> float:
    for key in keys:
        if key in prices:
            return float(prices[key])
    return default


def _leg_trade_context(
    quote: QuoteClient,
    quote_symbol: str,
    leg_code: str,
    rows: dict[str, Any],
    prices: dict[str, float],
    *,
    front_contract: str | None = None,
) -> tuple[Any | None, float | None]:
    """Quote row/price for a named month; front month may use MHImain when only continuous is quoted."""
    row = _lookup_quote_row(rows, leg_code)
    price = _resolve_trade_price(prices, leg_code, quote_symbol, front_contract)
    if is_named_mhi_contract(leg_code):
        if row is None and price is not None and leg_code not in prices:
            fallback_row = _lookup_quote_row(rows, quote_symbol)
            if fallback_row is not None:
                return _trade_row(quote, quote_symbol, fallback_row), price
        if row is None:
            return None, price
        return _trade_row(quote, leg_code, row), price
    if row is None:
        row = _lookup_quote_row(rows, quote_symbol)
    if price is None:
        price = _resolve_trade_price(prices, quote_symbol, quote_symbol, front_contract)
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
    row, price = _leg_trade_context(quote, quote_symbol, order_code, rows, prices)
    trade_row = row
    raw_row = _lookup_quote_row(rows, order_code)
    return position, strategy, order_code, trade_row, price, raw_row


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
    ltd_overrides: dict[str, date] | None = None,
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
    last_trade_time = (
        local_contract_last_trade_time(held, ltd_overrides=ltd_overrides) if held else None
    )

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
        ltd_overrides=ltd_overrides,
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
    ltd_overrides: dict[str, date] | None = None,
) -> bool:
    """Roll front-month legs. Returns True to skip all strategy signals."""
    for leg in sorted(portfolio.active_legs(), key=lambda lg: lg.code):
        if portfolio.is_next_month_code(leg.code):
            continue
        if leg.rollover is None or not leg.rollover.active():
            continue
        row, price = _leg_trade_context(quote, quote_symbol, leg.code, rows, prices)
        last_trade_time = local_contract_last_trade_time(leg.code, ltd_overrides=ltd_overrides)
        if _handle_contract_rollover(
            cfg, trade, quote, leg.position, leg.strategy, risk,
            quote_symbol, row, price, order_gate, trade_unlock,
            leg.rollover,
            front_contract=front_contract,
            held_contract=leg.code,
            last_trade_time=last_trade_time,
            rows=rows,
            prices=prices,
            ltd_overrides=ltd_overrides,
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
    ltd_overrides: dict[str, date] | None = None,
) -> tuple[bool, bool]:
    """Returns (skip_entry_strategy, skip_all_strategy)."""
    entry_skip = _handle_entry_book_rollover(
        cfg, trade, quote, portfolio, risk, quote_symbol,
        rows, prices, order_gate, trade_unlock,
        front_contract=front_contract,
        ltd_overrides=ltd_overrides,
    )
    leg_skip = _handle_leg_rollovers(
        cfg, trade, quote, portfolio, risk, quote_symbol,
        rows, prices, order_gate, trade_unlock,
        front_contract=front_contract,
        ltd_overrides=ltd_overrides,
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


def _contract_live_price(prices: dict[str, float], code: str, main_px: float) -> float | None:
    """Last price for a named month; do not substitute MHImain for month contracts."""
    if code in prices:
        return float(prices[code])
    if is_named_mhi_contract(code):
        return None
    return main_px


def _refresh_broker_positions_if_due(
    trade: TradeClient,
    symbol: str,
    cfg: dict,
    portfolio: MHIPortfolio,
    risk: RiskManager,
    prices: dict[str, float],
    fallback_price: float | None,
    *,
    last_refresh_mono: float,
    refresh_sec: float,
    now_mono: float,
    order_gate: OrderGate | None = None,
) -> float:
    """Sync entry + legs from broker every refresh_sec (flat or holding)."""
    if now_mono - last_refresh_mono < refresh_sec:
        return last_refresh_mono
    broker_legs = fetch_broker_mhi_legs(trade, symbol, cfg, quote_prices=prices)
    changed_codes = portfolio.refresh_from_broker(broker_legs, risk)
    if changed_codes:
        if order_gate is not None and order_gate.pending and order_gate.order_code:
            pending_code = order_gate.order_code.upper()
            changed_codes = [
                c
                for c in changed_codes
                if c.code.upper() != pending_code or c.contracts == 0
            ]
        if changed_codes:
            _report_broker_position_changes(
                symbol,
                portfolio,
                changed_codes,
                prices,
                fallback_price,
                broker_legs=broker_legs,
            )
    return now_mono


def _price_for_position_refresh(
    code: str,
    prices: dict[str, float],
    quote_symbol: str,
    front_contract: str | None,
    fallback_price: float | None,
    broker_by_code: dict[str, BrokerPosition] | None = None,
) -> float | None:
    """Last price for position_refresh; never substitute MHImain for non-front months."""
    px = _resolve_contract_poll_price(prices, code, quote_symbol, front_contract)
    if px is not None:
        return px
    if broker_by_code:
        broker = broker_by_code.get(code)
        if broker is not None and broker.current_price is not None:
            return broker.current_price
    if is_named_mhi_contract(code):
        return None
    return fallback_price


def _report_broker_position_changes(
    quote_symbol: str,
    portfolio: MHIPortfolio,
    changes: list[BrokerPositionChange],
    prices: dict[str, float],
    fallback_price: float | None,
    *,
    broker_legs: list[BrokerPosition] | None = None,
    title_prefix: str = "Refresh",
) -> None:
    broker_by_code = {b.code: b for b in (broker_legs or [])}
    for change in changes:
        code = change.code
        px = _price_for_position_refresh(
            code,
            prices,
            quote_symbol,
            portfolio.front_contract,
            fallback_price,
            broker_by_code,
        )
        if change.book == "entry":
            strategy = portfolio.entry_strategy
            position = portfolio.entry_position
        else:
            leg = portfolio.leg_for_code(code)
            if leg is None:
                if change.contracts != 0:
                    continue
                title = f"{title_prefix} {contract_log_label(code)}"
                msg = f"{title} flat (0)"
                LOG.info(
                    msg,
                    extra={
                        "event": "position_refresh",
                        "contract": code,
                        "contracts": 0,
                        "book": change.book,
                    },
                )
                continue
            strategy = leg.strategy
            position = leg.position
        title = f"{title_prefix} {contract_log_label(code)}"
        msg = _position_snapshot_message(strategy, position, px, title=title)
        LOG.info(
            msg,
            extra={
                "event": "position_refresh",
                "contract": code,
                "contracts": change.contracts,
                "book": change.book,
            },
        )
        if change.book == "leg" and change.contracts == 0:
            portfolio.remove_if_flat(code)


def _print_portfolio_status(
    quote_symbol: str,
    prices: dict[str, float],
    portfolio: MHIPortfolio,
    status: str,
    update_time: str | None,
    *,
    move_diffs: dict[str, float] | None = None,
    move_totals: dict[str, float] | None = None,
) -> None:
    del status, move_diffs, move_totals
    lines = _build_poll_status_lines(
        quote_symbol,
        prices,
        portfolio,
        update_time,
    )
    ts = _poll_log_ts(update_time)
    for line in lines:
        LOG.info(f"{ts} {line}", extra={"event": "poll"})


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
        ok, cap_reason = risk.approve_order(
            side, order_qty, unit_contracts=position.contracts,
        )
        if not ok:
            return {
                "status": "rejected",
                "reason": (
                    f"{cap_reason} (unit={position.contracts:+d}"
                    f" net={risk.position_shares:+d})"
                ),
            }

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


def _position_snapshot_message(
    strategy: MHImainStrategy,
    position: UnitPositionBook,
    live_price: float | None,
    *,
    title: str,
) -> str:
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
    return " ".join(parts)


def _print_position_snapshot(
    symbol: str,
    strategy: MHImainStrategy,
    position: UnitPositionBook,
    live_price: float | None,
    *,
    title: str = "Position",
) -> None:
    del symbol
    msg = _position_snapshot_message(strategy, position, live_price, title=title)
    LOG.info(msg, extra={"event": "position_snapshot", "title": title, "contracts": position.contracts})


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
    risk = RiskManager(cfg)

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

        hkex_calendar = HKEXFrontCalendar()
        hkex_spot_key: tuple[str, str] | None = None
        front_contract: str | None = None
        if is_continuous_mhi(symbol):
            spot, hkex_spot_key = _refresh_hkex_spot_if_changed(
                quote, hkex_calendar, hkex_spot_key, seed_ltd=True,
            )
            if spot:
                front_contract = spot.front
        else:
            spot = None
        _sync_front_context(portfolio, symbol, spot)

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
                prices.get(leg.code) if leg.code in prices else None,
                title=f"Startup {contract_log_label(leg.code)} (next month)",
            )
        if portfolio.is_flat():
            _, _, bootstrap_order = portfolio.flat_entry_target(front_contract)
            bootstrap_px = prices.get(bootstrap_order, quote_price)
            _print_bootstrap(symbol, portfolio.entry_strategy, portfolio.entry_position, bootstrap_px)

        order_gate = OrderGate()
        poll_display = PollDisplayGate(threshold_pts=poll_display_threshold)
        for code in _poll_display_codes(portfolio, symbol):
            seed_px = prices.get(code)
            if seed_px is not None:
                poll_display.seed(code, float(seed_px))

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
                if is_continuous_mhi(symbol):
                    spot, hkex_spot_key = _refresh_hkex_spot_if_changed(
                        quote, hkex_calendar, hkex_spot_key,
                    )
                    if spot:
                        front_contract = spot.front
                else:
                    spot = None
                _sync_front_context(portfolio, symbol, spot)

                now_mono = time.monotonic()
                last_position_refresh = _refresh_broker_positions_if_due(
                    trade, symbol, cfg, portfolio, risk, last_prices,
                    last_live_price,
                    last_refresh_mono=last_position_refresh,
                    refresh_sec=position_refresh_sec,
                    now_mono=now_mono,
                    order_gate=order_gate,
                )

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
                    refresh_before = last_position_refresh
                    last_position_refresh = _refresh_broker_positions_if_due(
                        trade, symbol, cfg, portfolio, risk, last_prices,
                        last_live_price,
                        last_refresh_mono=last_position_refresh,
                        refresh_sec=position_refresh_sec,
                        now_mono=now_mono,
                        order_gate=order_gate,
                    )
                    if now_mono - refresh_before >= position_refresh_sec:
                        _refresh_account_equity(trade, cfg, risk)
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

                entry_roll_skip, leg_roll_skip = _handle_portfolio_rollovers(
                    cfg, trade, quote, portfolio, risk, symbol,
                    rows, prices, order_gate, trade_unlock,
                    front_contract=front_contract,
                    ltd_overrides=hkex_calendar.ltd_overrides if is_continuous_mhi(symbol) else None,
                )
                if leg_roll_skip:
                    time.sleep(poll_interval)
                    count += 1
                    continue

                if now_mono - last_position_refresh >= position_refresh_sec:
                    _refresh_account_equity(trade, cfg, risk)

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
                display_status = "flat"
                display_tags = quote_fresh.status_tag
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
                    display_status = status
                    display_tags = cooldown_tag
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
                    skip_ahead_legs = False
                    if portfolio.entry_position.contracts != 0 and not entry_roll_skip:
                        entry_order = portfolio.managed_entry_code()
                        entry_trade_row, entry_price = _leg_trade_context(
                            quote, symbol, entry_order, rows, prices,
                            front_contract=portfolio.front_contract,
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
                        display_status = leg_status
                        display_tags = cooldown_tag + quote_fresh.status_tag
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
                            skip_ahead_legs = True

                    if not skip_ahead_legs:
                        for leg in portfolio.ahead_of_front_legs():
                            leg_trade_row, leg_price = _leg_trade_context(
                                quote, symbol, leg.code, rows, prices,
                                front_contract=portfolio.front_contract,
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
                            display_status = leg_status
                            display_tags = cooldown_tag + quote_fresh.status_tag
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

                poll_codes = _poll_display_codes(portfolio, symbol)
                display_prices = _poll_prices_for_display(
                    prices, symbol, portfolio, poll_codes,
                )
                update_time = _poll_update_time(data_times, symbol, portfolio)
                for code in poll_codes:
                    seed_px = display_prices.get(code)
                    if seed_px is not None:
                        poll_display.seed(code, float(seed_px))
                show_poll, move_diffs, move_totals = poll_display.note_prices(
                    display_prices, poll_codes,
                )
                if show_poll:
                    _print_portfolio_status(
                        symbol,
                        display_prices,
                        portfolio,
                        display_status + display_tags,
                        update_time,
                        move_diffs=move_diffs,
                        move_totals=move_totals,
                    )

                time.sleep(poll_interval)
                count += 1
        except KeyboardInterrupt:
            LOG.info(
                "stopped",
                extra={"event": "shutdown", "final_contracts": portfolio.total_signed_contracts()},
            )


if __name__ == "__main__":
    main()
