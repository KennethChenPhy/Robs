"""HK.MHImain continuous-future contract rollover (close expiring month, open front)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

HK = ZoneInfo("Asia/Hong_Kong")

# HKT on the contract last trading day: roll open positions before this time.
LTD_ROLLOVER_DEADLINE = time(11, 58)
# HKT on LTD: no new opens on the expiring month from rollover time through day close.
LTD_DAY_SESSION_END = time(16, 30)
# HKT: MHImain front month flips to next month when night (AHT) session starts.
AHT_SESSION_START = time(17, 15)

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


def is_hk_mhi_product_code(code: str) -> bool:
    """True for HK.MHImain and named months (HK.MHIyyMM); excludes HTI, HSI, etc."""
    return str(code or "").upper().startswith("HK.MHI")


def contract_month_key(code: str) -> tuple[int, int] | None:
    """Parse HK.MHIyyMM into (year, month) for ordering any named month (2606, 2607, 2608, …)."""
    text = str(code or "").upper()
    if not is_named_mhi_contract(text):
        return None
    suffix = text[len("HK.MHI") :]
    if len(suffix) != 4 or not suffix.isdigit():
        return None
    yy = int(suffix[:2])
    mm = int(suffix[2:])
    if not 1 <= mm <= 12:
        return None
    return (2000 + yy, mm)


def is_held_behind_front(held_contract: str, front_contract: str) -> bool:
    """True when held month expires before front (older yyMM vs newer yyMM)."""
    held_key = contract_month_key(held_contract)
    front_key = contract_month_key(front_contract)
    if held_key is None or front_key is None:
        return False
    return held_key < front_key


def is_held_ahead_of_front(held_contract: str, front_contract: str) -> bool:
    """True when held month is newer than the broker front month."""
    held_key = contract_month_key(held_contract)
    front_key = contract_month_key(front_contract)
    if held_key is None or front_key is None:
        return False
    return held_key > front_key


def contract_log_label(code: str | None) -> str:
    """Short contract name for logs: MHI2607, MHImain."""
    if not code:
        return ""
    text = str(code).strip().upper()
    if text == "HK.MHIMAIN":
        return "MHImain"
    if text.startswith("HK."):
        return text[3:]
    return text


def quote_symbols_for_portfolio(
    quote_symbol: str,
    held_codes: list[str],
    *,
    watch_code: str | None = None,
) -> list[str]:
    """MHImain when flat; + each held or watched named month for its own quote."""
    sym = str(quote_symbol or "")
    if not held_codes:
        out = [sym]
        if watch_code and is_named_mhi_contract(watch_code) and watch_code not in out:
            out.append(watch_code)
        return out
    out = [sym]
    for code in sorted(held_codes, key=lambda c: contract_month_key(c) or (0, 0)):
        if is_named_mhi_contract(code) and code not in out:
            out.append(code)
    return out


def next_named_mhi_contract(code: str) -> str | None:
    """Next calendar month contract after HK.MHIyyMM (2606 → 2607, 2512 → 2601)."""
    key = contract_month_key(code)
    if key is None:
        return None
    year, month = key
    month += 1
    if month > 12:
        month = 1
        year += 1
    return f"HK.MHI{year % 100:02d}{month:02d}"


def is_ltd_expiring_open_window(
    front_contract: str | None,
    front_last_trade_time: str | None,
    *,
    now: datetime | None = None,
) -> bool:
    """True 11:58–16:30 HKT on the front month's last trading day."""
    if not front_contract or not is_named_mhi_contract(front_contract):
        return False
    norm = normalize_last_trade_time(front_last_trade_time)
    if not norm or not is_last_trading_day(norm, now=now):
        return False
    ref = (now or datetime.now(HK)).astimezone(HK)
    start = ltd_rollover_deadline(norm, normalized=True)
    if start is None:
        return False
    last_day = parse_last_trade_date(norm)
    if last_day is None:
        return False
    end = datetime.strptime(
        f"{last_day.strftime('%Y-%m-%d')} {LTD_DAY_SESSION_END.strftime('%H:%M:%S')}",
        "%Y-%m-%d %H:%M:%S",
    ).replace(tzinfo=HK)
    return start <= ref < end


def is_ltd_expiring_month_open_banned(
    order_code: str,
    front_contract: str | None,
    front_last_trade_time: str | None,
    *,
    now: datetime | None = None,
) -> bool:
    """True when ``order_code`` is the expiring spot month during the LTD open ban window."""
    if not is_ltd_expiring_open_window(front_contract, front_last_trade_time, now=now):
        return False
    return str(order_code or "").upper() == str(front_contract or "").upper()


def roll_forward_entry_code(
    front_contract: str | None,
    front_last_trade_time: str | None,
    *,
    enabled: bool = True,
    now: datetime | None = None,
) -> str | None:
    """From 11:58 HKT on the front month's LTD, flat bot entries target the next month."""
    if not enabled:
        return None
    if not front_contract or not is_named_mhi_contract(front_contract):
        return None
    norm = normalize_last_trade_time(front_last_trade_time)
    if not norm or not is_ltd_rollover_active(norm, now=now, normalized=True):
        return None
    return next_named_mhi_contract(front_contract)


def resolve_entry_order_code(
    quote_symbol: str,
    front_contract: str | None,
    *,
    front_last_trade_time: str | None = None,
    roll_on_last_trade_day: bool = True,
    now: datetime | None = None,
) -> str:
    """Flat entry on front month; from 11:58–16:30 HKT on LTD, open next month instead."""
    if not is_continuous_mhi(quote_symbol):
        return quote_symbol
    if is_ltd_expiring_open_window(front_contract, front_last_trade_time, now=now):
        nxt = next_named_mhi_contract(front_contract or "")
        if nxt:
            return nxt
    roll_code = roll_forward_entry_code(
        front_contract,
        front_last_trade_time,
        enabled=roll_on_last_trade_day,
        now=now,
    )
    if roll_code:
        return roll_code
    norm = normalize_last_trade_time(front_last_trade_time)
    if (
        front_contract
        and norm
        and is_ltd_rollover_active(norm, now=now, normalized=True)
    ):
        nxt = next_named_mhi_contract(front_contract)
        if nxt:
            return nxt
    return front_contract or quote_symbol


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


def normalize_last_trade_time(last_trade_time: str | None) -> str | None:
    """Map broker last-trade date to LTD rollover deadline (11:58 HKT on that day)."""
    last_day = parse_last_trade_date(last_trade_time or "")
    if last_day is None:
        return None
    return f"{last_day.strftime('%Y-%m-%d')} {LTD_ROLLOVER_DEADLINE.strftime('%H:%M:%S')}"


def ltd_rollover_deadline(
    last_trade_time: str | None,
    *,
    normalized: bool = False,
) -> datetime | None:
    text = last_trade_time if normalized else normalize_last_trade_time(last_trade_time)
    if not text:
        return None
    for fmt in _LAST_TRADE_FORMATS:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=HK)
        except ValueError:
            continue
    return None


def is_past_ltd_rollover_deadline(
    last_trade_time: str | None,
    *,
    now: datetime | None = None,
    normalized: bool = False,
) -> bool:
    ref = (now or datetime.now(HK)).astimezone(HK)
    deadline = ltd_rollover_deadline(last_trade_time, normalized=normalized)
    if deadline is None:
        return False
    return ref >= deadline


def is_ltd_rollover_active(
    last_trade_time: str | None,
    *,
    now: datetime | None = None,
    normalized: bool = False,
) -> bool:
    """True on LTD at or after 11:58 HKT — rollover window and next-month flat entries."""
    text = last_trade_time if normalized else normalize_last_trade_time(last_trade_time)
    if not text or not is_last_trading_day(text, now=now):
        return False
    return is_past_ltd_rollover_deadline(text, now=now, normalized=True)


def is_last_trading_day(last_trade_time: str, *, now: datetime | None = None) -> bool:
    last_day = parse_last_trade_date(last_trade_time)
    if last_day is None:
        return False
    today = (now or datetime.now(HK)).astimezone(HK).date()
    return today == last_day


def pick_hkex_front_month(
    contracts: list[tuple[str, str | None]],
    *,
    now: datetime | None = None,
    today: date | None = None,
) -> str | None:
    """HKEX spot month: nearest listed MHI contract with last trading day still live.

    On a contract's last trading day, spot is that month until 17:15 HKT; from the
    night (AHT) session onward the expiring month has no session and front flips to
    the next month.
    """
    if now is not None:
        ref = now.astimezone(HK) if now.tzinfo else now.replace(tzinfo=HK)
    elif today is not None:
        ref = datetime.combine(today, time.min, tzinfo=HK)
    else:
        ref = datetime.now(HK)

    ref_day = ref.date()
    listed = {code for code, _ in contracts if is_named_mhi_contract(code)}
    best_code: str | None = None
    best_last: date | None = None
    for code, last_trade_time in contracts:
        if not is_named_mhi_contract(code):
            continue
        last_day = parse_last_trade_date(last_trade_time or "")
        if last_day is None or last_day < ref_day:
            continue
        if best_last is None or last_day < best_last:
            best_last = last_day
            best_code = code

    if (
        best_code
        and best_last == ref_day
        and ref.timetz() >= datetime.combine(ref_day, AHT_SESSION_START, tzinfo=HK).timetz()
    ):
        nxt = next_named_mhi_contract(best_code)
        if nxt and nxt in listed:
            return nxt
    return best_code


def resolve_rollover_target(
    held_contract: str,
    front_contract: str | None,
    *,
    last_trade_time: str | None = None,
    now: datetime | None = None,
) -> str | None:
    """Roll target: broker front when held is behind front; else next month from 11:58 on LTD."""
    if not is_named_mhi_contract(held_contract):
        return None

    ref = (now or datetime.now(HK)).astimezone(HK)
    if front_contract and is_held_behind_front(held_contract, front_contract):
        return front_contract

    norm = normalize_last_trade_time(last_trade_time)
    if not is_ltd_rollover_active(norm, now=ref, normalized=True):
        return None
    if front_contract and is_held_ahead_of_front(held_contract, front_contract):
        return None
    return next_named_mhi_contract(held_contract)


def should_rollover(
    held_contract: str,
    front_contract: str | None,
    last_trade_time: str | None,
    *,
    now: datetime | None = None,
    on_last_trade_day: bool = True,
) -> tuple[bool, str]:
    """True when held is behind broker front, or from 11:58 HKT on held's LTD."""
    if not held_contract or not is_named_mhi_contract(held_contract):
        return False, ""
    ref = (now or datetime.now(HK)).astimezone(HK)
    if front_contract and is_named_mhi_contract(front_contract):
        if is_held_ahead_of_front(held_contract, front_contract):
            return False, ""
        if is_held_behind_front(held_contract, front_contract):
            target = resolve_rollover_target(
                held_contract,
                front_contract,
                last_trade_time=last_trade_time,
                now=ref,
            )
            if target:
                return True, "held_behind_front"

    if not on_last_trade_day:
        return False, ""
    norm = normalize_last_trade_time(last_trade_time)
    if not is_ltd_rollover_active(norm, now=ref, normalized=True):
        return False, ""
    if resolve_rollover_target(
        held_contract,
        front_contract,
        last_trade_time=norm,
        now=ref,
    ) is None:
        return False, ""
    return True, "last_trading_day"


@dataclass
class RolloverState:
    phase: str = "idle"  # idle | close | open
    held_contract: str | None = None
    target_contract: str | None = None
    direction: int = 0
    reason: str = ""


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

    def advance_after_close(self, contracts: int) -> None:
        if self.state.phase == "close" and contracts == 0:
            self.state.phase = "open"

    def complete_if_opened(self, contracts: int) -> None:
        if self.state.phase == "open" and contracts != 0:
            self.reset()

    def complete_without_open(self) -> None:
        """Close finished; target month already held — skip broker open."""
        if self.state.phase == "open":
            self.reset()

    def reset(self) -> None:
        self.state = RolloverState()
