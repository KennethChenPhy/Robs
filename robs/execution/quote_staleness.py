"""Quote feed freshness: poll gap and market data_time age."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

HK = ZoneInfo("Asia/Hong_Kong")

_DATA_TIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M:%S.%f",
    "%H:%M:%S",
    "%H:%M:%S.%f",
)


def stale_threshold_sec(cfg: dict[str, Any], poll_interval_sec: float) -> float:
    multiplier = float(cfg.get("risk", {}).get("stale_poll_multiplier", 2.0))
    return poll_interval_sec * multiplier


def parse_quote_data_time(raw: str, *, now: datetime | None = None) -> datetime | None:
    text = str(raw or "").strip()
    if not text or text.upper() in ("N/A", "NA", "NONE"):
        return None

    now_hk = (now or datetime.now(HK)).astimezone(HK)

    for fmt in _DATA_TIME_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if "%Y" not in fmt:
            parsed = parsed.replace(year=now_hk.year, month=now_hk.month, day=now_hk.day)
        parsed = parsed.replace(tzinfo=HK)
        if parsed > now_hk + timedelta(minutes=5):
            parsed -= timedelta(days=1)
        return parsed
    return None


def quote_data_age_sec(data_time: str, *, now: datetime | None = None) -> float | None:
    parsed = parse_quote_data_time(data_time, now=now)
    if parsed is None:
        return None
    now_hk = (now or datetime.now(HK)).astimezone(HK)
    return max(0.0, (now_hk - parsed).total_seconds())


def _data_time_stale(data_time: str, *, threshold: float, now: datetime | None) -> tuple[bool, float | None]:
    raw = str(data_time or "").strip()
    if not raw or raw.upper() in ("N/A", "NA", "NONE"):
        return True, None
    age = quote_data_age_sec(raw, now=now)
    if age is None:
        return True, None
    return age > threshold, age


@dataclass
class QuoteFreshness:
    poll_stale: bool = False
    data_stale: bool = False
    data_age_sec: float | None = None
    data_time_raw: str = ""
    threshold_sec: float = 0.0

    @property
    def block_entries(self) -> bool:
        return self.poll_stale or self.data_stale

    @property
    def status_tag(self) -> str:
        if self.poll_stale and self.data_stale:
            return " [STALE_POLL+DATA]"
        if self.poll_stale:
            return " [STALE_POLL]"
        if self.data_stale:
            return " [STALE_DATA]"
        return ""


def assess_quote_freshness(
    cfg: dict[str, Any],
    poll_interval_sec: float,
    *,
    last_successful_poll_at: datetime | None,
    data_time: str | None = "",
    poll_failed: bool = False,
    now: datetime | None = None,
) -> QuoteFreshness:
    threshold = stale_threshold_sec(cfg, poll_interval_sec)
    now_utc = now or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)

    poll_stale = False
    if last_successful_poll_at is not None:
        started = last_successful_poll_at
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        else:
            started = started.astimezone(timezone.utc)
        poll_age = (now_utc - started).total_seconds()
        poll_stale = poll_age > threshold

    if data_time is None:
        data_stale = False
        data_age = None
        data_time_raw = ""
    else:
        data_stale, data_age = _data_time_stale(data_time, threshold=threshold, now=now)
        data_time_raw = str(data_time or "")

    # poll_failed kept for API compatibility; poll gap is always measured from last success.
    _ = poll_failed

    return QuoteFreshness(
        poll_stale=poll_stale,
        data_stale=data_stale,
        data_age_sec=data_age,
        data_time_raw=data_time_raw,
        threshold_sec=threshold,
    )


def is_new_entry(position_contracts: int, action: Any) -> bool:
    from robs.strategy.rules import Action

    return position_contracts == 0 and action in (Action.BUY, Action.SELL)
