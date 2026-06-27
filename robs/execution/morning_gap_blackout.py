"""Block flat morning entries when day open gaps from prior night close."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from robs.execution.hkex_trading_hours import (
    HK,
    DAY_MORNING_CLOSE,
    DAY_MORNING_OPEN,
    in_morning_gap_entry_blackout_window,
    is_night_trading_session,
)


@dataclass
class MorningGapBlackout:
    """
    If flat at 09:15 and |day open − prior night close (≈03:00)| >= gap_pts,
    block new entries until 09:45 HKT.
    """

    gap_pts: float = 200.0
    enabled: bool = True
    night_close_price: float | None = None
    morning_open_price: float | None = None
    morning_open_day: date | None = None
    was_flat_at_morning_open: bool = False
    active: bool = False

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> MorningGapBlackout:
        mhi = cfg.get("mhimain", {})
        raw = mhi.get("morning_gap_blackout_pts")
        if raw is None:
            return cls(enabled=False)
        gap = float(raw)
        if gap <= 0:
            return cls(enabled=False)
        return cls(gap_pts=gap, enabled=True)

    def on_session_idle(self, last_price: float | None, last_poll_at: datetime | None) -> None:
        """Persist last night-session price when entering idle after night close."""
        self.note_night_session_price(last_price, last_poll_at)

    def note_night_session_price(
        self,
        price: float | None,
        when: datetime | None = None,
    ) -> None:
        """Track the latest price seen during the night session (≈03:00 reference)."""
        if not self.enabled or price is None or price <= 0 or when is None:
            return
        if is_night_trading_session(when):
            self.night_close_price = price

    def note_morning_open_if_due(
        self,
        open_price: float,
        *,
        was_flat: bool,
        now: datetime | None = None,
    ) -> bool:
        """
        Record today's 09:15 open vs stored night close once per morning.
        Returns True when blackout was newly activated.
        """
        if not self.enabled or open_price <= 0:
            return False
        now_hk = (now or datetime.now(HK)).astimezone(HK)
        t = now_hk.timetz()
        if not (DAY_MORNING_OPEN <= t < DAY_MORNING_CLOSE):
            return False
        today = now_hk.date()
        if self.morning_open_day == today:
            return False

        self.morning_open_day = today
        self.morning_open_price = open_price
        self.was_flat_at_morning_open = was_flat
        self.active = False

        if not was_flat or self.night_close_price is None:
            return False

        gap = abs(open_price - self.night_close_price)
        if gap >= self.gap_pts:
            self.active = True
            return True
        return False

    def blocks_entry(self, now: datetime | None = None) -> tuple[bool, str]:
        if not self.enabled or not self.active or not self.was_flat_at_morning_open:
            return False, ""
        now_hk = (now or datetime.now(HK)).astimezone(HK)
        if not in_morning_gap_entry_blackout_window(now_hk):
            return False, ""
        gap = 0.0
        if self.morning_open_price is not None and self.night_close_price is not None:
            gap = abs(self.morning_open_price - self.night_close_price)
        return (
            True,
            f"morning gap blackout: open−night_close={gap:.0f}pts until 09:45",
        )

    def status_tag(self, now: datetime | None = None) -> str:
        blocked, _ = self.blocks_entry(now)
        if blocked:
            return " [GAP_BLACKOUT]"
        return ""
