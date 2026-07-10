"""Tests for HK public holidays and trading-hours elapsed."""

from __future__ import annotations

import unittest
from datetime import datetime

from robs.execution.cut_loss import PositionPnLBaseline
from robs.execution.hk_public_holidays import is_hk_full_holiday, is_hk_half_day
from robs.execution.hkex_trading_hours import (
    HK,
    hkex_trading_hours_add,
    hkex_trading_hours_elapsed,
    is_hkex_mhi_trading_session,
)


class HKPublicHolidayTests(unittest.TestCase):
    def test_2026_national_day(self) -> None:
        self.assertTrue(is_hk_full_holiday(datetime(2026, 10, 1, tzinfo=HK).date()))

    def test_2026_half_day_christmas_eve(self) -> None:
        self.assertTrue(is_hk_half_day(datetime(2026, 12, 24, tzinfo=HK).date()))


class HKEXHolidaySessionTests(unittest.TestCase):
    def test_national_day_closed(self) -> None:
        dt = datetime(2026, 10, 1, 10, 0, tzinfo=HK)
        self.assertFalse(is_hkex_mhi_trading_session(dt))

    def test_half_day_morning_only(self) -> None:
        morning = datetime(2026, 12, 24, 10, 0, tzinfo=HK)
        afternoon = datetime(2026, 12, 24, 14, 0, tzinfo=HK)
        self.assertTrue(is_hkex_mhi_trading_session(morning))
        self.assertFalse(is_hkex_mhi_trading_session(afternoon))

    def test_holiday_night_spill_still_open(self) -> None:
        """Mon night → Tue 02:00 still trades when Tue is a public holiday."""
        dt = datetime(2026, 10, 1, 2, 0, tzinfo=HK)  # National Day morning spill
        self.assertTrue(is_hkex_mhi_trading_session(dt))


class HKEXTradingHoursElapsedTests(unittest.TestCase):
    def test_one_hour_same_session(self) -> None:
        start = datetime(2026, 6, 29, 10, 0, tzinfo=HK)
        end = datetime(2026, 6, 29, 11, 0, tzinfo=HK)
        self.assertAlmostEqual(hkex_trading_hours_elapsed(start, end), 1.0)

    def test_add_one_trading_hour(self) -> None:
        start = datetime(2026, 6, 29, 10, 0, tzinfo=HK)
        expires = hkex_trading_hours_add(start, 1.0)
        self.assertEqual(expires, datetime(2026, 6, 29, 11, 0, tzinfo=HK))

    def test_add_skips_weekend(self) -> None:
        start = datetime(2026, 7, 3, 10, 0, tzinfo=HK)  # Friday
        expires = hkex_trading_hours_add(start, 20.0)
        self.assertGreater(expires, datetime(2026, 7, 6, 9, 0, tzinfo=HK))

    def test_weekend_excluded(self) -> None:
        """Fri 10:00 → Sat 10:00 wall clock is 24h but far fewer trading hours."""
        start = datetime(2026, 7, 3, 10, 0, tzinfo=HK)  # Friday
        end = datetime(2026, 7, 4, 10, 0, tzinfo=HK)  # Saturday
        wall_h = (end - start).total_seconds() / 3600.0
        trading_h = hkex_trading_hours_elapsed(start, end)
        self.assertAlmostEqual(wall_h, 24.0)
        self.assertLess(trading_h, 20.0)
        self.assertGreater(trading_h, 10.0)

    def test_public_holiday_day_has_zero_hours(self) -> None:
        start = datetime(2026, 10, 1, 10, 0, tzinfo=HK)
        end = datetime(2026, 10, 1, 16, 0, tzinfo=HK)
        self.assertAlmostEqual(hkex_trading_hours_elapsed(start, end), 0.0)


class CutLossMinHoldTradingHoursTests(unittest.TestCase):
    def test_expires_at_after_required_trading_hours(self) -> None:
        bl = PositionPnLBaseline(cut_loss_min_hold_hours=5.0)
        opened = datetime(2026, 6, 29, 10, 0, tzinfo=HK)
        expires = bl.cut_loss_min_hold_expires_at(opened)
        self.assertIsNotNone(expires)
        assert expires is not None
        self.assertAlmostEqual(hkex_trading_hours_elapsed(opened, expires), 5.0)

    def test_weekend_does_not_count_toward_min_hold(self) -> None:
        bl = PositionPnLBaseline(cut_loss_min_hold_hours=20.0)
        opened = datetime(2026, 7, 3, 10, 0, tzinfo=HK)  # Friday 10:00
        check = datetime(2026, 7, 4, 10, 0, tzinfo=HK)  # Saturday 10:00
        blocked, reason = bl.blocks_cut_loss_for_hold(opened, now=check)
        self.assertTrue(blocked)
        self.assertIn("trading h", reason)

    def test_enough_trading_hours_allows_cut_loss(self) -> None:
        bl = PositionPnLBaseline(cut_loss_min_hold_hours=5.0)
        opened = datetime(2026, 6, 29, 10, 0, tzinfo=HK)
        check = datetime(2026, 6, 29, 16, 0, tzinfo=HK)
        blocked, _ = bl.blocks_cut_loss_for_hold(opened, now=check)
        self.assertFalse(blocked)


if __name__ == "__main__":
    unittest.main()
