"""Position state: signed contract count (+n long, -n short, 0 flat)."""

from __future__ import annotations

from dataclasses import dataclass

from robs.strategy.rules import Action, Signal


@dataclass
class UnitPositionBook:
    """Futures position as signed contract count."""

    contracts: int = 0
    default_order_qty: int = 1

    def reset_after_flat(self) -> None:
        """After flat (e.g. closed n>1), auto trades use 1 contract again."""
        self.default_order_qty = 1

    @property
    def position(self) -> int:
        """Direction only: +1 long, -1 short, 0 flat."""
        if self.contracts > 0:
            return 1
        if self.contracts < 0:
            return -1
        return 0

    @property
    def size(self) -> int:
        return abs(self.contracts)

    def order_qty(self, action: Action) -> int:
        """Close full stack when |position| > 1; otherwise use default_order_qty (1)."""
        n = self.size
        if self._is_closing(action) and n > 1:
            return n
        return max(1, self.default_order_qty)

    def _is_closing(self, action: Action) -> bool:
        if action == Action.FLAT:
            return self.contracts != 0
        if action == Action.SELL and self.contracts > 0:
            return True
        if action == Action.BUY and self.contracts < 0:
            return True
        return False

    def allowed_actions(self) -> set[Action]:
        if self.contracts > 0:
            return {Action.HOLD, Action.SELL, Action.FLAT}
        if self.contracts < 0:
            return {Action.HOLD, Action.BUY, Action.FLAT}
        return {Action.HOLD, Action.BUY, Action.SELL}

    def allowed_labels(self) -> str:
        return ", ".join(sorted(a.value for a in self.allowed_actions()))

    def filter_signal(self, signal: Signal) -> Signal:
        if signal.action in self.allowed_actions():
            return signal
        return Signal(
            rule=signal.rule,
            action=Action.HOLD,
            trade_ticker=signal.trade_ticker,
            reason=(
                f"blocked {signal.action.value}: contracts={self.contracts:+d}, "
                f"allowed=[{self.allowed_labels()}]"
            ),
            metadata={**signal.metadata, "blocked_action": signal.action.value, "contracts": self.contracts},
        )

    def resolve_order_side(self, action: Action) -> str | None:
        if action == Action.HOLD:
            return None
        if action == Action.BUY:
            return "BUY"
        if action == Action.SELL:
            return "SELL"
        if action == Action.FLAT:
            if self.contracts > 0:
                return "SELL"
            if self.contracts < 0:
                return "BUY"
        return None

    def on_fill(self, side: str, fill_qty: int = 1) -> None:
        side = side.upper()
        qty = max(1, int(fill_qty))
        if side == "BUY":
            if self.contracts < 0:
                self.contracts = min(0, self.contracts + qty)
            else:
                self.contracts += qty
        elif side == "SELL":
            if self.contracts > 0:
                self.contracts = max(0, self.contracts - qty)
            else:
                self.contracts -= qty
        if self.contracts == 0:
            self.reset_after_flat()

    def validate_transition(self, action: Action) -> tuple[bool, str]:
        if action not in self.allowed_actions():
            return False, f"contracts {self.contracts:+d} cannot {action.value}"
        return True, "ok"
