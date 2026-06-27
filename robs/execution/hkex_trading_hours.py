"""HKEX Mini-Hang Seng Index (MHI) futures trading session (HKT)."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

HK = ZoneInfo("Asia/Hong_Kong")

# Continuous trading (HKEX derivatives — MHI matches HSI schedule).
DAY_MORNING_OPEN = time(9, 15)
DAY_MORNING_CLOSE = time(12, 0)
MORNING_GAP_BLACKOUT_END = time(9, 45)
DAY_AFTERNOON_OPEN = time(13, 0)
DAY_AFTERNOON_CLOSE = time(16, 30)
NIGHT_OPEN = time(17, 15)
NIGHT_CLOSE = time(3, 0)  # next calendar day


def _as_hk(ref: datetime | None) -> datetime:
    if ref is None:
        return datetime.now(HK)
    if ref.tzinfo is None:
        return ref.replace(tzinfo=HK)
    return ref.astimezone(HK)


def is_hkex_mhi_trading_session(ref: datetime | None = None) -> bool:
    """True during HKEX MHI day or T+1 night continuous trading (Mon–Fri schedule)."""
    dt = _as_hk(ref)
    t = dt.timetz()
    wd = dt.weekday()  # Mon=0 … Sun=6

    # Night session 17:15 → 03:00 (early morning belongs to prior session day).
    if t < NIGHT_CLOSE:
        prev = (dt - timedelta(days=1)).weekday()
        return prev <= 4

    if t < DAY_MORNING_OPEN:
        return False

    if DAY_MORNING_OPEN <= t < DAY_MORNING_CLOSE:
        return wd <= 4

    if DAY_MORNING_CLOSE <= t < DAY_AFTERNOON_OPEN:
        return False

    if DAY_AFTERNOON_OPEN <= t < DAY_AFTERNOON_CLOSE:
        return wd <= 4

    if DAY_AFTERNOON_CLOSE <= t < NIGHT_OPEN:
        return False

    if t >= NIGHT_OPEN:
        return wd <= 4

    return False


def opend_hkfuture_is_open(opend_state: Any) -> bool | None:
    """Parse OpenD global_state market_hkfuture; None if unknown."""
    if opend_state is None:
        return None
    raw: Any
    if isinstance(opend_state, dict):
        raw = opend_state.get("market_hkfuture")
    elif hasattr(opend_state, "get"):
        raw = opend_state.get("market_hkfuture")  # type: ignore[union-attr]
    else:
        raw = getattr(opend_state, "market_hkfuture", None)
    status = str(raw or "").upper().strip()
    if not status:
        return None
    if "OPEN" in status:
        return True
    if "CLOSE" in status or status in ("NONE", "UNKNOWN", "N/A"):
        return False
    return None


def assess_hkex_mhi_session(
    *,
    now: datetime | None = None,
    opend_state: Any = None,
) -> tuple[bool, str]:
    """
    Return (active, reason). Closed if OpenD says closed; else local HKEX schedule.
    """
    local_open = is_hkex_mhi_trading_session(now)
    opend_open = opend_hkfuture_is_open(opend_state)
    if opend_open is False:
        return False, "opend_hkfuture_closed"
    if not local_open:
        return False, "outside_hkex_hours"
    if opend_open is True:
        return True, "opend_hkfuture_open"
    return True, "hkex_hours"


def is_night_trading_session(ref: datetime | None = None) -> bool:
    """True during HKEX MHI night segment (17:15 → 03:00 next day, HKT)."""
    dt = _as_hk(ref)
    t = dt.timetz()
    return t >= NIGHT_OPEN or t < NIGHT_CLOSE


def in_morning_gap_entry_blackout_window(ref: datetime | None = None) -> bool:
    """True 09:15–09:45 HKT on a weekday morning session day."""
    dt = _as_hk(ref)
    if not is_hkex_mhi_trading_session(dt):
        return False
    t = dt.timetz()
    return DAY_MORNING_OPEN <= t < MORNING_GAP_BLACKOUT_END


def current_hkex_session_start(ref: datetime | None = None) -> datetime | None:
    """Start of the HKEX segment containing ref (HKT), or None if closed."""
    dt = _as_hk(ref)
    if not is_hkex_mhi_trading_session(dt):
        return None
    t = dt.timetz()
    d = dt.date()
    if DAY_MORNING_OPEN <= t < DAY_MORNING_CLOSE:
        return datetime.combine(d, DAY_MORNING_OPEN, tzinfo=HK)
    if DAY_AFTERNOON_OPEN <= t < DAY_AFTERNOON_CLOSE:
        return datetime.combine(d, DAY_AFTERNOON_OPEN, tzinfo=HK)
    if t >= NIGHT_OPEN:
        return datetime.combine(d, NIGHT_OPEN, tzinfo=HK)
    # Night session before 03:00 (calendar morning).
    return datetime.combine(d - timedelta(days=1), NIGHT_OPEN, tzinfo=HK)


def session_open_grace_sec(cfg: dict[str, Any]) -> float:
    """Seconds after session open to ignore pre-session quote data_time for entries."""
    raw = cfg.get("mhimain", {}).get("session_open_grace_sec", 1800)
    return max(0.0, float(raw))


def next_hkex_mhi_session_open(ref: datetime | None = None) -> datetime:
    """Next session open (HKT) at or after ref (for idle logging)."""
    dt = _as_hk(ref)
    for minutes in range(0, 8 * 24 * 60, 15):
        candidate = dt + timedelta(minutes=minutes)
        if is_hkex_mhi_trading_session(candidate):
            t = candidate.timetz()
            if t < DAY_MORNING_OPEN:
                return candidate.replace(hour=9, minute=15, second=0, microsecond=0)
            if DAY_MORNING_CLOSE <= t < DAY_AFTERNOON_OPEN:
                return candidate.replace(hour=13, minute=0, second=0, microsecond=0)
            if DAY_AFTERNOON_CLOSE <= t < NIGHT_OPEN:
                return candidate.replace(hour=17, minute=15, second=0, microsecond=0)
            if t >= NIGHT_CLOSE and t < DAY_MORNING_OPEN:
                return candidate.replace(hour=9, minute=15, second=0, microsecond=0)
            return candidate.replace(second=0, microsecond=0)
    return dt + timedelta(hours=1)
