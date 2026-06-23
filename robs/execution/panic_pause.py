"""Defer cut loss after a sudden large move — avoid panic exits."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone


@dataclass
class PanicGuard:
    """If price moves move_pts within window_sec, block cut loss for wait_min minutes."""

    move_pts: float = 200.0
    window_sec: float = 200.0
    wait_min: float = 30.0
    _history: deque[tuple[datetime, float]] = field(default_factory=deque)
    panic_until: datetime | None = None

    def update(self, price: float, now: datetime | None = None) -> bool:
        """Update price window. Returns True when panic pause newly starts."""
        now = now or datetime.now(timezone.utc)
        was_active = self.panic_until is not None and now < self.panic_until
        self._history.append((now, price))
        cutoff = now - timedelta(seconds=self.window_sec)
        while self._history and self._history[0][0] < cutoff:
            self._history.popleft()

        if len(self._history) < 2:
            return False

        prices = [p for _, p in self._history]
        if max(prices) - min(prices) >= self.move_pts:
            self.panic_until = now + timedelta(minutes=self.wait_min)
            return not was_active
        return False

    def blocks_cut_loss(self, now: datetime | None = None) -> tuple[bool, str]:
        now = now or datetime.now(timezone.utc)
        if self.panic_until is None:
            return False, ""
        if now < self.panic_until:
            remaining_min = (self.panic_until - now).total_seconds() / 60.0
            return True, f"panic pause: no cut loss for {remaining_min:.0f}min more"
        self.panic_until = None
        return False, ""

    @property
    def active(self) -> bool:
        blocked, _ = self.blocks_cut_loss()
        return blocked
