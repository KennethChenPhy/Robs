"""Tests for HKEX MHI trading session hours."""

from __future__ import annotations

import unittest
from datetime import datetime

from robs.execution.hkex_trading_hours import (
    HK,
    assess_hkex_mhi_session,
    current_hkex_session_start,
    is_hkex_mhi_trading_session,
    next_hkex_mhi_session_open,
    opend_hkfuture_is_open,
    session_open_grace_sec,
)


class HKEXTradingHoursTests(unittest.TestCase):
    def test_monday_morning_open(self) -> None:
        dt = datetime(2026, 6, 29, 10, 0, tzinfo=HK)
        self.assertTrue(is_hkex_mhi_trading_session(dt))

    def test_weekday_lunch_closed(self) -> None:
        dt = datetime(2026, 6, 29, 12, 30, tzinfo=HK)
        self.assertFalse(is_hkex_mhi_trading_session(dt))

    def test_weekday_night_open(self) -> None:
        dt = datetime(2026, 6, 29, 20, 0, tzinfo=HK)
        self.assertTrue(is_hkex_mhi_trading_session(dt))

    def test_friday_night_into_saturday_morning(self) -> None:
        dt = datetime(2026, 7, 4, 1, 0, tzinfo=HK)  # Sat 01:00 from Fri night
        self.assertTrue(is_hkex_mhi_trading_session(dt))

    def test_saturday_day_closed(self) -> None:
        dt = datetime(2026, 7, 4, 10, 0, tzinfo=HK)
        self.assertFalse(is_hkex_mhi_trading_session(dt))

    def test_sunday_closed(self) -> None:
        dt = datetime(2026, 7, 5, 15, 0, tzinfo=HK)
        self.assertFalse(is_hkex_mhi_trading_session(dt))

    def test_monday_pre_open_closed(self) -> None:
        dt = datetime(2026, 6, 29, 8, 0, tzinfo=HK)
        self.assertFalse(is_hkex_mhi_trading_session(dt))

    def test_opend_closed_overrides_local(self) -> None:
        dt = datetime(2026, 6, 29, 10, 0, tzinfo=HK)
        active, reason = assess_hkex_mhi_session(
            now=dt, opend_state={"market_hkfuture": "CLOSED"},
        )
        self.assertFalse(active)
        self.assertEqual(reason, "opend_hkfuture_closed")

    def test_opend_open_during_local_hours(self) -> None:
        dt = datetime(2026, 6, 29, 10, 0, tzinfo=HK)
        active, reason = assess_hkex_mhi_session(
            now=dt, opend_state={"market_hkfuture": "NIGHT_OPEN"},
        )
        self.assertTrue(active)
        self.assertEqual(reason, "opend_hkfuture_open")

    def test_opend_parser(self) -> None:
        self.assertTrue(opend_hkfuture_is_open({"market_hkfuture": "DAY_OPEN"}))
        self.assertFalse(opend_hkfuture_is_open({"market_hkfuture": "CLOSED"}))
        self.assertIsNone(opend_hkfuture_is_open({}))

    def test_current_session_start_morning(self) -> None:
        dt = datetime(2026, 6, 29, 9, 30, tzinfo=HK)
        start = current_hkex_session_start(dt)
        self.assertIsNotNone(start)
        assert start is not None
        self.assertEqual(start.hour, 9)
        self.assertEqual(start.minute, 15)

    def test_session_open_grace_default(self) -> None:
        self.assertEqual(session_open_grace_sec({"mhimain": {}}), 1800.0)

    def test_morning_gap_window(self) -> None:
        from robs.execution.hkex_trading_hours import in_morning_gap_entry_blackout_window

        inside = datetime(2026, 6, 29, 9, 30, tzinfo=HK)
        self.assertTrue(in_morning_gap_entry_blackout_window(inside))
        outside = datetime(2026, 6, 29, 10, 0, tzinfo=HK)
        self.assertFalse(in_morning_gap_entry_blackout_window(outside))

    def test_next_open_saturday_evening_is_monday_0915(self) -> None:
        """Regression: 15-min stepping from :43 produced 09:28 instead of 09:15."""
        sat = datetime(2026, 6, 27, 17, 43, 17, tzinfo=HK)
        nxt = next_hkex_mhi_session_open(sat)
        self.assertEqual(nxt, datetime(2026, 6, 29, 9, 15, tzinfo=HK))

    def test_next_open_lunch_gap_is_afternoon_1300(self) -> None:
        mon = datetime(2026, 6, 29, 12, 30, tzinfo=HK)
        nxt = next_hkex_mhi_session_open(mon)
        self.assertEqual(nxt, datetime(2026, 6, 29, 13, 0, tzinfo=HK))

    def test_next_open_day_close_gap_is_night_1715(self) -> None:
        mon = datetime(2026, 6, 29, 16, 35, tzinfo=HK)
        nxt = next_hkex_mhi_session_open(mon)
        self.assertEqual(nxt, datetime(2026, 6, 29, 17, 15, tzinfo=HK))

    def test_next_open_pre_morning_is_same_day_0915(self) -> None:
        mon = datetime(2026, 6, 29, 8, 0, tzinfo=HK)
        nxt = next_hkex_mhi_session_open(mon)
        self.assertEqual(nxt, datetime(2026, 6, 29, 9, 15, tzinfo=HK))


if __name__ == "__main__":
    unittest.main()
