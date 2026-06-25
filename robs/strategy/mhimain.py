"""HK.MHImain strategy with trend entry, cut loss, and take profit."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from robs.execution.cut_loss import PositionPnLBaseline
from robs.execution.panic_pause import PanicGuard
from robs.execution.position import UnitPositionBook
from robs.strategy.entry import TrendEntry
from robs.strategy.rules import Action, Signal
from robs.strategy.trend import TrendMode, apply_trend_filter


@dataclass
class MHImainStrategy:
    symbol: str = "HK.MHImain"
    cut_loss_pts: float = 200.0
    take_profit_pts: float = 400.0
    reentry_move_pts: float = 300.0
    ma_period: int = 5
    entry_price: float | None = None
    position_opened_at: datetime | None = None
    ma5: float | None = None
    pnl_baseline: PositionPnLBaseline = field(default_factory=PositionPnLBaseline)
    panic_guard: PanicGuard = field(default_factory=PanicGuard)
    _pending_cut_loss: bool = False
    _pending_take_profit: bool = False
    _entry_armed: bool = True
    _panic_just_triggered: bool = False
    _order_pending: bool = False
    trend: TrendMode = TrendMode.UNCERTAIN

    @property
    def cooldown(self) -> PositionPnLBaseline:
        return self.pnl_baseline

    @classmethod
    def from_config(cls, cfg: dict[str, Any], trend: TrendMode = TrendMode.UNCERTAIN) -> MHImainStrategy:
        mhi = cfg.get("mhimain", {})
        symbol = str(mhi.get("symbol", "HK.MHImain"))
        cut_loss_pts = float(mhi.get("cut_loss_pts", 200))
        reentry_move_pts = float(mhi.get("reentry_move_pts", 300))
        reentry_trading_hours = float(mhi.get("reentry_trading_hours", 6))
        reentry_minimum_hours = float(mhi.get("reentry_minimum_hours", 4))
        take_profit_pts = float(mhi.get("take_profit_pts", 400))
        cut_loss_min_hold_hours = float(mhi.get("cut_loss_min_hold_hours", 24))
        return cls(
            symbol=symbol,
            cut_loss_pts=cut_loss_pts,
            take_profit_pts=take_profit_pts,
            reentry_move_pts=reentry_move_pts,
            ma_period=int(mhi.get("ma_period", 5)),
            pnl_baseline=PositionPnLBaseline(
                cut_loss_pts=cut_loss_pts,
                take_profit_pts=take_profit_pts,
                reentry_move_pts=reentry_move_pts,
                reentry_trading_hours=reentry_trading_hours,
                reentry_minimum_hours=reentry_minimum_hours,
                cut_loss_min_hold_hours=cut_loss_min_hold_hours,
            ),
            panic_guard=PanicGuard(
                move_pts=float(mhi.get("panic_move_pts", 200)),
                window_sec=float(mhi.get("panic_window_sec", 200)),
                wait_min=float(mhi.get("panic_wait_min", 30)),
            ),
            trend=trend,
        )

    def set_ma5(self, ma5: float | None) -> None:
        self.ma5 = ma5

    def _finalize(self, signal: Signal, position: UnitPositionBook) -> Signal:
        signal = position.filter_signal(signal)
        return apply_trend_filter(signal, position, self.trend)

    def on_new_entry(self, entry_price: float) -> None:
        self.entry_price = entry_price
        self.position_opened_at = datetime.now(timezone.utc)
        self.pnl_baseline.reset_on_new_entry()
        self._entry_armed = False

    def on_broker_position_opened(self) -> None:
        """Broker sync detected a new position (e.g. manual open). Start min-hold timer."""
        self.position_opened_at = datetime.now(timezone.utc)
        self._entry_armed = False

    def on_broker_position_closed(self, exit_price: float) -> None:
        """Broker sync detected flat (e.g. manual close). Apply post-exit cooldown."""
        self.on_exit_cooldown(exit_price)

    def rearm_entry_if_flat(self, position: UnitPositionBook) -> None:
        if position.contracts == 0 and not self.pnl_baseline.locked:
            self._entry_armed = True

    def _try_flat_entry(self, price: float, position: UnitPositionBook) -> Signal | None:
        if not self._entry_armed:
            return None

        entry = TrendEntry(trend=self.trend, ma5=self.ma5)
        raw = entry.evaluate(price, self.symbol)
        if raw is None:
            return Signal(
                "mhimain",
                Action.HOLD,
                self.symbol,
                "uncertain entry blocked: MA5 unavailable",
                {"price": price},
            )
        if raw.action == Action.HOLD:
            return raw

        final = self._finalize(raw, position)
        if final.action in (Action.BUY, Action.SELL):
            return final
        return final

    def consume_panic_trigger(self) -> bool:
        triggered = self._panic_just_triggered
        self._panic_just_triggered = False
        return triggered

    def set_order_pending(self, pending: bool) -> None:
        self._order_pending = pending
        if pending:
            self._entry_armed = False

    def update(self, price: float, position: UnitPositionBook) -> Signal:
        self.pnl_baseline.roll_session()
        self._panic_just_triggered = self.panic_guard.update(price)
        trade_ticker = self.symbol

        if self._order_pending:
            return Signal(
                "mhimain",
                Action.HOLD,
                trade_ticker,
                "order pending",
                {"price": price},
            )

        if position.contracts == 0:
            blocked, reason = self.pnl_baseline.blocks_entry(price)
            if blocked:
                return Signal("mhimain", Action.HOLD, trade_ticker, reason, {"price": price})
            if "cooldown cleared" in reason:
                self.rearm_entry_if_flat(position)

            entry_signal = self._try_flat_entry(price, position)
            if entry_signal is not None:
                return entry_signal

            ma_info = f" MA5={self.ma5:.1f}" if self.ma5 is not None else ""
            return Signal(
                "mhimain",
                Action.HOLD,
                trade_ticker,
                f"flat, waiting entry{ma_info}",
                {"price": price, "ma5": self.ma5},
            )

        live_px = float(price) if price is not None else 0.0
        if live_px <= 0:
            label = (
                f"holding long x{position.size}"
                if position.contracts > 0
                else f"holding short x{position.size}"
            )
            return Signal(
                "mhimain",
                Action.HOLD,
                trade_ticker,
                f"{label} (no quote for exit checks)",
                {"price": price},
            )

        if self.entry_price is None:
            self.entry_price = live_px

        panic_blocked, panic_reason = self.panic_guard.blocks_cut_loss()
        hold_blocked, hold_reason = self.pnl_baseline.blocks_cut_loss_for_hold(self.position_opened_at)
        if (
            not panic_blocked
            and not hold_blocked
            and self.pnl_baseline.should_cut_loss(self.entry_price, live_px, position.position)
        ):
            self._pending_cut_loss = True
            trigger = self.pnl_baseline.cut_loss_trigger()
            return self._finalize(
                Signal(
                    "mhimain",
                    Action.FLAT,
                    trade_ticker,
                    f"cut loss at {trigger:+.0f}pts (now {self.pnl_baseline.pnl_points(self.entry_price, live_px, position.position):+.0f})",
                    {"price": live_px},
                ),
                position,
            )

        if self.pnl_baseline.should_take_profit(self.entry_price, live_px, position.position):
            self._pending_take_profit = True
            trigger = self.pnl_baseline.take_profit_trigger()
            pnl = self.pnl_baseline.pnl_points(self.entry_price, live_px, position.position)
            return self._finalize(
                Signal(
                    "mhimain",
                    Action.FLAT,
                    trade_ticker,
                    f"take profit at {trigger:+.0f}pts (now {pnl:+.0f})",
                    {"price": live_px},
                ),
                position,
            )

        pnl_status = self.pnl_baseline.status_line(self.entry_price, live_px, position.position)
        label = f"holding long x{position.size}" if position.contracts > 0 else f"holding short x{position.size}"
        block_reason = panic_reason if panic_blocked else (hold_reason if hold_blocked else "")
        if block_reason:
            return Signal(
                "mhimain",
                Action.HOLD,
                trade_ticker,
                f"{label} ({pnl_status}) [{block_reason}]",
                {"price": live_px},
            )
        return Signal("mhimain", Action.HOLD, trade_ticker, f"{label} ({pnl_status})", {"price": live_px})

    def on_exit_cooldown(self, exit_price: float) -> None:
        self.pnl_baseline.record_exit_cooldown(exit_price)
        self._pending_cut_loss = False
        self._pending_take_profit = False
        self.entry_price = None
        self.position_opened_at = None

    def on_cut_loss_filled(self, exit_price: float) -> None:
        self.on_exit_cooldown(exit_price)

    def on_take_profit_filled(self, exit_price: float) -> None:
        self.on_exit_cooldown(exit_price)

    def on_position_closed(
        self,
        exit_price: float,
        was_cut_loss: bool = False,
        was_take_profit: bool = False,
    ) -> None:
        if was_cut_loss or self._pending_cut_loss:
            self.on_cut_loss_filled(exit_price)
        elif was_take_profit or self._pending_take_profit:
            self.on_take_profit_filled(exit_price)
        else:
            self.entry_price = None
            self.position_opened_at = None
            self._pending_cut_loss = False
            self._pending_take_profit = False

    def sync_existing_position(self, has_position: bool) -> None:
        """At launch: only auto-enter when flat."""
        self._entry_armed = not has_position
