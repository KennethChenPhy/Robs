"""P/L baselines for cut loss and take profit from launch or new entry."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


@dataclass
class PositionPnLBaseline:
    """Launch P/L is the reference: cut = baseline - cut_loss_pts, profit = baseline + take_profit_pts."""

    cut_loss_pts: float = 200.0
    take_profit_pts: float = 400.0
    reentry_move_pts: float = 300.0
    reentry_trading_hours: float = 6.0
    reentry_minimum_hours: float = 4.0
    cut_loss_min_hold_hours: float = 24.0
    locked: bool = False
    cooldown_ref_price: float | None = None
    cooldown_started_at: datetime | None = None
    cooldown_until: datetime | None = None
    baseline_pnl_pts: float = 0.0
    session_accum_pnl_pts: float = 0.0

    def roll_session(self) -> None:
        """No-op kept for call-site compatibility."""

    @staticmethod
    def pnl_points(entry_price: float, price: float, position: int) -> float:
        if position == 1:
            return price - entry_price
        if position == -1:
            return entry_price - price
        return 0.0

    def bootstrap(self, entry_price: float, current_price: float, position: int) -> None:
        """Set baseline to current broker P/L at script start."""
        self.baseline_pnl_pts = self.pnl_points(entry_price, current_price, position)
        self.session_accum_pnl_pts = self.baseline_pnl_pts

    def reset_on_new_entry(self) -> None:
        """New position opened this session — baseline is flat P/L (session accum unchanged)."""
        self.baseline_pnl_pts = 0.0

    def realize_on_close(self, entry_price: float, exit_price: float, position: int) -> float:
        """Book per-contract P/L from a closed trade into the session total."""
        pts = self.pnl_points(entry_price, exit_price, position)
        self.session_accum_pnl_pts += pts
        return pts

    def session_total_pnl(
        self,
        entry_price: float | None,
        price: float,
        position: int,
    ) -> float:
        """Realized session P/L plus open-position unrealized (per contract)."""
        if position != 0 and entry_price is not None:
            return self.session_accum_pnl_pts + self.pnl_points(entry_price, price, position)
        return self.session_accum_pnl_pts

    def cut_loss_trigger(self) -> float:
        return self.baseline_pnl_pts - self.cut_loss_pts

    def take_profit_trigger(self) -> float:
        return self.baseline_pnl_pts + self.take_profit_pts

    def should_cut_loss(self, entry_price: float, price: float, position: int) -> bool:
        if position == 0:
            return False
        pnl = self.pnl_points(entry_price, price, position)
        return pnl <= self.cut_loss_trigger()

    def blocks_cut_loss_for_hold(
        self,
        opened_at: datetime | None,
        now: datetime | None = None,
    ) -> tuple[bool, str]:
        """Block cut loss until position has been held for cut_loss_min_hold_hours."""
        if opened_at is None:
            return False, ""
        now = now or datetime.now(timezone.utc)
        if opened_at.tzinfo is None:
            opened_at = opened_at.replace(tzinfo=timezone.utc)
        elapsed_h = (now - opened_at).total_seconds() / 3600.0
        if elapsed_h >= self.cut_loss_min_hold_hours:
            return False, ""
        remaining_h = self.cut_loss_min_hold_hours - elapsed_h
        return (
            True,
            f"min hold: no cut loss for {remaining_h:.1f}h more "
            f"(need {self.cut_loss_min_hold_hours:.0f}h)",
        )

    def should_take_profit(self, entry_price: float, price: float, position: int) -> bool:
        if position == 0:
            return False
        pnl = self.pnl_points(entry_price, price, position)
        return pnl >= self.take_profit_trigger()

    def status_line(self, entry_price: float, price: float, position: int) -> str:
        pnl = self.pnl_points(entry_price, price, position)
        return (
            f"P/L {pnl:+.0f}pts | cut {self.cut_loss_trigger():+.0f} | "
            f"profit {self.take_profit_trigger():+.0f}"
        )

    def _clear_cooldown(self) -> None:
        self.locked = False
        self.cooldown_ref_price = None
        self.cooldown_started_at = None
        self.cooldown_until = None

    def record_exit_cooldown(self, exit_price: float) -> None:
        """After exit: lock entries until min hours, then move or max trading hours."""
        now = datetime.now(timezone.utc)
        self.locked = True
        self.cooldown_ref_price = exit_price
        self.cooldown_started_at = now
        self.cooldown_until = now + timedelta(hours=self.reentry_trading_hours)

    def _cooldown_hours_elapsed(self, now: datetime) -> float:
        if self.cooldown_started_at is None:
            return 0.0
        started = self.cooldown_started_at
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        return (now - started).total_seconds() / 3600.0

    def record_cut_loss(self, exit_price: float) -> None:
        self.record_exit_cooldown(exit_price)

    def blocks_entry(self, price: float) -> tuple[bool, str]:
        if not self.locked or self.cooldown_ref_price is None:
            return False, ""

        now = datetime.now(timezone.utc)
        elapsed_h = self._cooldown_hours_elapsed(now)

        if self.cooldown_until is not None and now >= self.cooldown_until:
            self._clear_cooldown()
            return False, f"cooldown cleared after {self.reentry_trading_hours:.0f} trading hours"

        move = abs(price - self.cooldown_ref_price)
        min_hours_left = max(0.0, self.reentry_minimum_hours - elapsed_h)

        if min_hours_left > 0:
            return (
                True,
                f"cooldown: {min_hours_left:.1f}h min wait before re-entry "
                f"(move {move:.0f}/{self.reentry_move_pts:.0f}pt from {self.cooldown_ref_price:.1f})",
            )

        if move >= self.reentry_move_pts:
            self._clear_cooldown()
            return False, f"cooldown cleared after {move:.0f}pt move"

        need_pts = self.reentry_move_pts - move
        hours_left = 0.0
        if self.cooldown_until is not None:
            hours_left = max(0.0, (self.cooldown_until - now).total_seconds() / 3600.0)
        return (
            True,
            f"cooldown: need {need_pts:.0f}pt more or {hours_left:.1f}h left "
            f"(from {self.cooldown_ref_price:.1f})",
        )


CutLossCooldown = PositionPnLBaseline
