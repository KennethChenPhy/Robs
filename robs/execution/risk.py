"""Pre-trade risk checks and kill switch."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class RiskManager:
    cfg: dict[str, Any]
    day_start_equity: float = 0.0
    current_equity: float = 0.0
    position_shares: int = 0
    killed: bool = False
    kill_reason: str = ""

    @property
    def max_position(self) -> int:
        return int(self.cfg.get("risk", {}).get("max_position_shares", 1000))

    @property
    def max_daily_loss_pct(self) -> float:
        return float(self.cfg.get("risk", {}).get("max_daily_loss_pct", 2.0))

    def set_equity(self, equity: float) -> None:
        if self.day_start_equity == 0.0:
            self.day_start_equity = equity
        self.current_equity = equity
        self._check_daily_loss()

    def _check_daily_loss(self) -> None:
        if self.day_start_equity <= 0:
            return
        loss_pct = (self.day_start_equity - self.current_equity) / self.day_start_equity * 100.0
        if loss_pct >= self.max_daily_loss_pct:
            self.killed = True
            self.kill_reason = f"daily loss {loss_pct:.2f}% >= {self.max_daily_loss_pct}%"

    def check_stale(self, last_poll_at: datetime | None, interval_sec: float) -> bool:
        multiplier = float(self.cfg.get("risk", {}).get("stale_poll_multiplier", 2.0))
        if last_poll_at is None:
            return True
        age = (datetime.now(timezone.utc) - last_poll_at).total_seconds()
        return age > interval_sec * multiplier

    def approve_order(self, side: str, qty: int) -> tuple[bool, str]:
        if self.killed:
            return False, self.kill_reason or "kill switch active"

        if qty <= 0:
            return False, "qty must be positive"

        projected = self.position_shares + qty if side.upper() == "BUY" else self.position_shares - qty
        if abs(projected) > self.max_position:
            return False, f"position {projected} exceeds max {self.max_position}"

        return True, "ok"

    def on_fill(self, side: str, qty: int) -> None:
        if side.upper() == "BUY":
            self.position_shares += qty
        else:
            self.position_shares -= qty
