"""HKEX securities-market public holidays (HKT calendar dates).

Source: HKEX participant circulars / holiday schedule (SEHK CT/063/24, CT/075/25, CT/077/26).
Full-day closures and half-day sessions (morning only, no afternoon/night).
"""

from __future__ import annotations

from datetime import date

# Full-day closures — no trading (day or night sessions starting that evening).
HK_FULL_HOLIDAYS: dict[int, frozenset[date]] = {
    2025: frozenset(
        {
            date(2025, 1, 1),
            date(2025, 1, 29),
            date(2025, 1, 30),
            date(2025, 1, 31),
            date(2025, 4, 4),
            date(2025, 4, 18),
            date(2025, 4, 19),
            date(2025, 4, 21),
            date(2025, 5, 1),
            date(2025, 5, 5),
            date(2025, 7, 1),
            date(2025, 10, 1),
            date(2025, 10, 7),
            date(2025, 10, 29),
            date(2025, 12, 25),
            date(2025, 12, 26),
        }
    ),
    2026: frozenset(
        {
            date(2026, 1, 1),
            date(2026, 2, 17),
            date(2026, 2, 18),
            date(2026, 2, 19),
            date(2026, 4, 3),
            date(2026, 4, 6),
            date(2026, 4, 7),
            date(2026, 5, 1),
            date(2026, 5, 25),
            date(2026, 6, 19),
            date(2026, 7, 1),
            date(2026, 10, 1),
            date(2026, 10, 19),
            date(2026, 12, 25),
        }
    ),
    2027: frozenset(
        {
            date(2027, 1, 1),
            date(2027, 2, 8),
            date(2027, 2, 9),
            date(2027, 3, 26),
            date(2027, 3, 29),
            date(2027, 4, 5),
            date(2027, 5, 13),
            date(2027, 6, 9),
            date(2027, 7, 1),
            date(2027, 9, 16),
            date(2027, 10, 1),
            date(2027, 10, 8),
            date(2027, 12, 27),
        }
    ),
}

# Half-day trading: morning session only (09:15–12:00 for MHI); no afternoon/night.
HK_HALF_DAYS: dict[int, frozenset[date]] = {
    2025: frozenset({date(2025, 1, 28), date(2025, 12, 24), date(2025, 12, 31)}),
    2026: frozenset({date(2026, 2, 16), date(2026, 12, 24), date(2026, 12, 31)}),
    2027: frozenset({date(2027, 2, 5), date(2027, 12, 24), date(2027, 12, 31)}),
}


def is_hk_full_holiday(d: date) -> bool:
    return d in HK_FULL_HOLIDAYS.get(d.year, frozenset())


def is_hk_half_day(d: date) -> bool:
    return d in HK_HALF_DAYS.get(d.year, frozenset())
