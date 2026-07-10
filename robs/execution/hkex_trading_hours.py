"""HKEX Mini-Hang Seng Index (MHI) futures trading session (HKT)."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from robs.execution.hk_public_holidays import is_hk_full_holiday, is_hk_half_day

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


def _session_weekday_time_allows(dt: datetime) -> bool:
    """Weekday/time-window check only (no public-holiday calendar)."""
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


def _holiday_allows(dt: datetime) -> bool:
    """False when HK public holidays block this instant (half-day aware)."""
    t = dt.timetz()
    d = dt.date()

    # Night spill 00:00–03:00 is attributed to the prior evening session.
    if t < NIGHT_CLOSE:
        return True

    if is_hk_full_holiday(d):
        return False

    if is_hk_half_day(d):
        return DAY_MORNING_OPEN <= t < DAY_MORNING_CLOSE

    return True


def is_hkex_mhi_trading_session(ref: datetime | None = None) -> bool:
    """True during HKEX MHI day or T+1 night continuous trading (Mon–Fri schedule)."""
    dt = _as_hk(ref)
    if not _session_weekday_time_allows(dt):
        return False
    return _holiday_allows(dt)


def hkex_trading_hours_elapsed(start: datetime, end: datetime) -> float:
    """HKEX MHI session hours between start and end (excludes weekends and HK holidays)."""
    start_hk = _as_hk(start)
    end_hk = _as_hk(end)
    if end_hk <= start_hk:
        return 0.0

    cursor = start_hk.replace(second=0, microsecond=0)
    if cursor < start_hk:
        cursor += timedelta(minutes=1)

    minutes = 0
    while cursor + timedelta(minutes=1) <= end_hk:
        if is_hkex_mhi_trading_session(cursor):
            minutes += 1
        cursor += timedelta(minutes=1)
    return minutes / 60.0


def hkex_trading_hours_add(start: datetime, hours: float, *, max_calendar_days: int = 90) -> datetime:
    """Return the HKT instant when ``hours`` of HKEX session time have elapsed from ``start``."""
    start_hk = _as_hk(start)
    needed = hours * 60.0
    if needed <= 0:
        return start_hk

    accumulated = 0.0
    cursor = start_hk.replace(second=0, microsecond=0)
    if cursor < start_hk:
        cursor += timedelta(minutes=1)
    deadline = start_hk + timedelta(days=max_calendar_days)

    while accumulated < needed - 1e-9 and cursor < deadline:
        if is_hkex_mhi_trading_session(cursor):
            accumulated += 1.0
        cursor += timedelta(minutes=1)
    return cursor


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


def _session_open_is_scheduled(open_dt: datetime) -> bool:
    """True if this open time is valid (not blocked by HK public holidays)."""
    d = open_dt.date()
    t = open_dt.timetz()
    if is_hk_full_holiday(d):
        return False
    if is_hk_half_day(d):
        return t == DAY_MORNING_OPEN
    return True


def next_hkex_mhi_session_open(ref: datetime | None = None) -> datetime:
    """Next scheduled session open (HKT) strictly after ref."""
    dt = _as_hk(ref)
    for day_offset in range(0, 15):
        d = dt.date() + timedelta(days=day_offset)
        if d.weekday() > 4:
            continue
        for open_time in (DAY_MORNING_OPEN, DAY_AFTERNOON_OPEN, NIGHT_OPEN):
            open_dt = datetime.combine(d, open_time, tzinfo=HK)
            if open_dt > dt and _session_open_is_scheduled(open_dt):
                return open_dt
    return datetime.combine(
        dt.date() + timedelta(days=1),
        DAY_MORNING_OPEN,
        tzinfo=HK,
    )
