"""Manual session trend: bull, bear, or uncertain."""

from __future__ import annotations

from enum import Enum

from robs.execution.position import UnitPositionBook
from robs.strategy.rules import Action, Signal


class TrendMode(str, Enum):
    BULL = "bull"
    BEAR = "bear"
    UNCERTAIN = "uncertain"

    @classmethod
    def from_user_input(cls, text: str) -> TrendMode | None:
        key = text.strip().lower()
        aliases = {
            "1": cls.BULL,
            "bull": cls.BULL,
            "b": cls.BULL,
            "2": cls.BEAR,
            "bear": cls.BEAR,
            "s": cls.BEAR,
            "3": cls.UNCERTAIN,
            "uncertain": cls.UNCERTAIN,
            "u": cls.UNCERTAIN,
            "both": cls.UNCERTAIN,
        }
        return aliases.get(key)


def prompt_trend_mode() -> TrendMode:
    print("\nSelect session trend:")
    print("  1) bull       — open long only; close long for profit or stop loss")
    print("  2) bear       — open short only; close short for profit or stop loss")
    print("  3) uncertain  — open long or short")
    while True:
        choice = input("Enter 1/2/3 or bull/bear/uncertain: ").strip()
        mode = TrendMode.from_user_input(choice)
        if mode is not None:
            return mode
        print("Invalid choice, try again.")


def apply_trend_filter(
    signal: Signal,
    position: UnitPositionBook,
    trend: TrendMode,
) -> Signal:
    if trend == TrendMode.UNCERTAIN:
        return signal

    # Exits: profit take, stop loss, flat — always allowed when in a position.
    if position.position != 0:
        if signal.action in (Action.FLAT, Action.HOLD):
            return signal
        if position.position == 1 and signal.action == Action.SELL:
            return signal
        if position.position == -1 and signal.action == Action.BUY:
            return signal
        return _blocked(signal, trend, "exit-only while in position")

    # Flat: filter new entries only.
    if trend == TrendMode.BULL and signal.action == Action.SELL:
        return _blocked(signal, trend, "bull mode: short entries disabled")
    if trend == TrendMode.BEAR and signal.action == Action.BUY:
        return _blocked(signal, trend, "bear mode: long entries disabled")

    return signal


def _blocked(signal: Signal, trend: TrendMode, reason: str) -> Signal:
    return Signal(
        rule=signal.rule,
        action=Action.HOLD,
        trade_ticker=signal.trade_ticker,
        reason=f"{reason} (trend={trend.value})",
        metadata={**signal.metadata, "blocked_by_trend": trend.value},
    )
