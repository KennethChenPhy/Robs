"""HK.MHImain continuous-future contract rollover (close expiring month, open front)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

HK = ZoneInfo("Asia/Hong_Kong")

_LAST_TRADE_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%d",
)


def is_continuous_mhi(symbol: str) -> bool:
    return str(symbol or "").upper() == "HK.MHIMAIN"


def is_named_mhi_contract(code: str) -> bool:
    text = str(code or "").upper()
    return text.startswith("HK.MHI") and text != "HK.MHIMAIN"


def rollover_enabled(cfg: dict[str, Any], quote_symbol: str) -> bool:
    if not is_continuous_mhi(quote_symbol):
        return False
    return bool(cfg.get("mhimain", {}).get("contract_rollover", True))


def parse_last_trade_date(last_trade_time: str) -> date | None:
    text = str(last_trade_time or "").strip()
    if not text or text.upper() in ("N/A", "NA", "NONE"):
        return None
    for fmt in _LAST_TRADE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    if len(text) >= 10:
        try:
            return datetime.strptime(text[:10], "%Y-%m-%d").date()
        except ValueError:
            return None
    return None


def is_last_trading_day(last_trade_time: str, *, now: datetime | None = None) -> bool:
    last_day = parse_last_trade_date(last_trade_time)
    if last_day is None:
        return False
    today = (now or datetime.now(HK)).astimezone(HK).date()
    return today == last_day


def should_rollover(
    held_contract: str,
    front_contract: str | None,
    last_trade_time: str | None,
    *,
    now: datetime | None = None,
    on_front_change: bool = True,
    on_last_trade_day: bool = True,
) -> tuple[bool, str]:
    if not held_contract or not is_named_mhi_contract(held_contract):
        return False, ""
    if on_front_change and front_contract and held_contract.upper() != front_contract.upper():
        return True, "front_month_changed"
    if on_last_trade_day and last_trade_time and is_last_trading_day(last_trade_time, now=now):
        return True, "last_trading_day"
    return False, ""


@dataclass
class RolloverState:
    phase: str = "idle"  # idle | close | open
    held_contract: str | None = None
    front_contract: str | None = None
    target_contract: str | None = None
    direction: int = 0
    reason: str = ""
    announced: bool = False


@dataclass
class ContractRolloverManager:
    """State machine: close held month, reopen same direction on front month."""

    quote_symbol: str
    cfg: dict[str, Any]
    state: RolloverState = field(default_factory=RolloverState)

    def active(self) -> bool:
        return rollover_enabled(self.cfg, self.quote_symbol)

    def busy(self) -> bool:
        return self.state.phase != "idle"

    def note_held_contract(self, code: str | None, contracts: int) -> None:
        if contracts != 0 and code and is_named_mhi_contract(code):
            self.state.held_contract = code

    def begin(self, *, held: str, front: str, direction: int, reason: str) -> None:
        self.state.phase = "close"
        self.state.held_contract = held
        self.state.target_contract = front
        self.state.direction = direction
        self.state.reason = reason
        self.state.announced = True

    def advance_after_close(self, contracts: int) -> None:
        if self.state.phase == "close" and contracts == 0:
            self.state.phase = "open"

    def complete_if_opened(self, contracts: int) -> None:
        if self.state.phase == "open" and contracts != 0:
            self.reset()

    def reset(self) -> None:
        self.state = RolloverState()


def order_code_for_position(
    quote_symbol: str,
    position_contracts: int,
    held_contract: str | None,
    front_contract: str | None,
) -> str:
    """Pick broker order code: held month when in position, else front month."""
    if position_contracts != 0 and held_contract and is_named_mhi_contract(held_contract):
        return held_contract
    if is_continuous_mhi(quote_symbol) and front_contract:
        return front_contract
    return quote_symbol
