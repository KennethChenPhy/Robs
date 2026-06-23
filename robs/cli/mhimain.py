#!/usr/bin/env python3
"""Standalone HK.MHImain trader — max one contract long or short.

Position rules:
  flat (0):  allowed HOLD, BUY (open long), SELL (open short)
  long (+1): allowed HOLD, SELL (close) — no BUY
  short (-1): allowed HOLD, BUY (cover) — no SELL
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
from robs.execution.risk import RiskManager
from robs.strategy.mhimain import MHImainStrategy
from robs.strategy.rules import Action
from robs.strategy.trend import TrendMode, prompt_trend_mode


def _alert_panic_pause(strategy: MHImainStrategy, price: float) -> None:
    guard = strategy.panic_guard
    move = guard.move_pts
    window = guard.window_sec
    wait = guard.wait_min
    print(
        f"\n🚨 PANIC PAUSE at price {price:.1f} — "
        f"{move:.0f}pt move in {window:.0f}s; cut loss disabled for {wait:.0f}min\n"
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
    if result.get("dry_run"):
        text = f"dry-run {side} x{q}"
    elif result.get("status") in ("rejected", "skipped") or not result.get("ok", result.get("dry_run")):
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
    print(f"  {_order_line(side, qty, price, result, pos_after=pos_after, cum_pnl=cum_pnl)}")


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
    print(
        f"[{ts}] {price:.0f}{entry_tag} {position.contracts:+d}{pnl_tag}{move_tag}"
        f" — {_compact_signal(signal_reason)}"
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
    dry_run: bool,
    order_gate: OrderGate,
) -> None:
    if order_gate.pending:
        outcome = order_gate.try_resolve(trade, cfg, symbol, position, strategy, risk, price)
        if outcome == "filled":
            side = order_gate.side or "?"
            qty = int(order_gate.qty)
            _finalize_filled_order(strategy, risk, position, price, order_gate, dry_run)
            cum = _session_pnl_pts(strategy, position, price)
            _log_order(
                side, qty, price,
                {"ok": True, "filled": True, "status": "FILLED"},
                pos_after=position.contracts,
                cum_pnl=cum,
            )
        elif outcome == "failed":
            print("  order cancelled — retry later")
            strategy.rearm_entry_if_flat(position)
        if order_gate.pending:
            return

    action_label = signal.action.value
    if signal.action == Action.HOLD:
        return

    if dry_run:
        side = position.resolve_order_side(signal.action) or action_label
        qty = position.order_qty(signal.action)
        cum = _session_pnl_pts(strategy, position, price)
        _log_order(side, qty, price, {"dry_run": True}, cum_pnl=cum)
        return

    if risk.killed:
        print("KILL SWITCH:", risk.kill_reason)
        return

    result = execute_unit_order(cfg, trade, position, signal.action, symbol, quote_row=quote_row, dry_run=dry_run)
    side = position.resolve_order_side(signal.action)
    if side is None:
        return
    order_qty = position.order_qty(signal.action)

    if result.get("status") in ("rejected", "skipped") or not result.get("ok", result.get("dry_run")):
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

    if result.get("dry_run") or result.get("filled"):
        if dry_run:
            position.on_fill(side, fill_qty=order_qty)
        else:
            order_gate.try_resolve(trade, cfg, symbol, position, strategy, risk, price)
        _finalize_filled_order(strategy, risk, position, price, order_gate, dry_run, fill_price=price)
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
    dry_run: bool,
    *,
    fill_price: float | None = None,
) -> None:
    action = order_gate.signal_action
    exit_price = fill_price if fill_price is not None else price
    entry_before = strategy.entry_price
    close_side = order_gate.side

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
        position.reset_after_flat()
        strategy.rearm_entry_if_flat(position)
    elif action in (Action.BUY, Action.SELL) and position.contracts != 0:
        strategy.on_new_entry(exit_price)
        risk.position_shares = position.contracts
    elif dry_run and action in (Action.BUY, Action.SELL) and position.contracts != 0:
        strategy.on_new_entry(exit_price)
        risk.position_shares = position.contracts


def execute_unit_order(
    cfg: dict,
    trade: TradeClient,
    position: UnitPositionBook,
    action: Action,
    symbol: str,
    quote_row=None,
    *,
    dry_run: bool = False,
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
        dry_run=dry_run,
    )
    result["qty"] = order_qty
    if not result.get("ok", result.get("dry_run")):
        result["status"] = "rejected"
    elif result.get("dry_run"):
        result["status"] = "simulated"
        result["filled"] = True
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
    print("  " + " | ".join(parts))


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


def main() -> None:
    parser = argparse.ArgumentParser(description="HK.MHImain unit-position trader")
    parser.add_argument("--config", default="mhimain.yaml")
    parser.add_argument("--iterations", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true", help="Signals only, no orders")
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
    args = parser.parse_args()

    cfg = load_config(args.config)
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
    endpoints = endpoints_from_config(cfg)

    if args.prompt_trend:
        trend = prompt_trend_mode()
    else:
        trend_name = args.trend or mhi_cfg.get("default_trend", "uncertain")
        trend = TrendMode(str(trend_name).lower())

    strategy = MHImainStrategy.from_config(cfg, trend=trend)
    position = UnitPositionBook()
    risk = RiskManager({**cfg, "risk": {**cfg.get("risk", {}), "max_position_shares": 1}})
    risk.set_equity(1.0)

    print(f"MHImain trader symbol={symbol} trd_env={trd_env_name(cfg)}")
    print(f"Session trend: {trend.value}")
    print("Position: max 1 contract. long(+1)->HOLD|SELL  short(-1)->HOLD|BUY  flat(0)->HOLD|BUY|SELL")
    print(
        f"Entry: bull→long | bear→short | uncertain→vs MA{mhi_cfg.get('ma_period', 5)} | "
        f"cut −{mhi_cfg.get('cut_loss_pts', 200)} / profit +{mhi_cfg.get('take_profit_pts', 400)} from launch | "
        f"cooldown {mhi_cfg.get('reentry_move_pts', 300)}pt or {mhi_cfg.get('reentry_trading_hours', 6)}h | "
        f"panic: {mhi_cfg.get('panic_move_pts', 200)}pt/{mhi_cfg.get('panic_window_sec', 200)}s → wait {mhi_cfg.get('panic_wait_min', 30)}min before cut loss"
    )
    print(f"Poll display: ±{poll_display_threshold:.0f}pts (strategy still runs every {poll_interval:.0f}s)")
    print("Ctrl+C to stop.\n")

    with QuoteClient(endpoints) as quote, TradeClient(endpoints, futures=True) as trade:
        ok, state = quote.global_state()
        if not ok:
            print("Failed to connect to OpenD")
            sys.exit(1)
        print("OpenD:", state)

        sub_ok, sub_msg = quote.subscribe_quote([symbol])
        if not sub_ok:
            print(f"WARN: quote subscribe failed: {sub_msg}")

        ret_ok, quote_data = quote.quote([symbol])
        quote_price = float(quote_data.iloc[0]["last_price"]) if ret_ok and len(quote_data) else None
        if quote_price is None:
            print("WARN: no initial quote — waiting for live feed")

        broker = fetch_broker_position(trade, symbol, cfg, quote_price=quote_price)
        apply_broker_position(broker, position, strategy)
        strategy.sync_existing_position(broker.contracts != 0)
        risk.position_shares = broker.contracts

        ma_period = int(mhi_cfg.get("ma_period", 5))
        ok_ma, ma5 = quote.daily_ma(symbol, period=ma_period)
        strategy.set_ma5(ma5)
        if ok_ma and ma5 is not None:
            print(f"MA{ma_period} (daily): {ma5:.1f}")
        elif trend == TrendMode.UNCERTAIN:
            print(f"WARN: could not fetch MA{ma_period}; uncertain entries need MA5")

        _print_bootstrap(symbol, strategy, position, quote_price, broker_pnl_val=broker.pnl_val)
        print()

        # Try opening immediately when flat at launch
        if position.contracts == 0 and not strategy.cooldown.locked and quote_price is not None:
            launch_signal = strategy.update(quote_price, position)
            if launch_signal.action != Action.HOLD:
                print(f"Launch: {launch_signal.action.value} — {launch_signal.reason}")
                if not args.dry_run:
                    _, trade_row = quote.quote_for_trade(symbol, row=quote_data.iloc[0])
                    _process_signal(
                        cfg, trade, position, strategy, risk,
                        launch_signal, symbol, trade_row, quote_price, args.dry_run, order_gate,
                    )
                else:
                    side = launch_signal.action.value
                    qty = position.order_qty(launch_signal.action)
                    _log_order(
                        side, qty, quote_price, {"dry_run": True},
                        cum_pnl=_session_pnl_pts(strategy, position, quote_price),
                    )

        count = 0
        last_poll_at = None
        last_position_refresh = time.monotonic()
        poll_display = PollDisplayGate(threshold_pts=poll_display_threshold)
        order_gate = OrderGate()
        if quote_price is not None:
            poll_display.ref_price = quote_price
        try:
            while args.iterations is None or count < args.iterations:
                ret_ok, data = quote.quote([symbol])
                last_poll_at = datetime.now(timezone.utc)
                if not ret_ok or data is None or len(data) == 0:
                    print("WARN: quote poll failed — check OpenD / quote subscription")
                    time.sleep(poll_interval)
                    count += 1
                    continue

                row = data.iloc[0]
                price = float(row["last_price"])
                update_time = str(row.get("data_time", ""))

                now_mono = time.monotonic()
                if now_mono - last_position_refresh >= position_refresh_sec:
                    broker = fetch_broker_position(trade, symbol, cfg, quote_price=price)
                    changed = refresh_broker_position(broker, position, strategy, risk, cfg=cfg)
                    if changed:
                        print(f"WARN: broker position changed → {_position_label(broker.contracts)}")
                    _print_position_snapshot(
                        symbol,
                        strategy,
                        position,
                        price,
                        title="Refresh",
                        broker_pnl_val=broker.pnl_val,
                    )
                    last_position_refresh = now_mono

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

                if risk.killed:
                    print("KILL SWITCH:", risk.kill_reason)
                    break
                if risk.check_stale(last_poll_at, poll_interval):
                    print("WARN: stale quote")

                _process_signal(
                    cfg, trade, position, strategy, risk,
                    signal, symbol, _trade_row(quote, symbol, row), price, args.dry_run, order_gate,
                )

                time.sleep(poll_interval)
                count += 1
        except KeyboardInterrupt:
            print(f"\nStopped. final position={position.contracts:+d}")


if __name__ == "__main__":
    main()
