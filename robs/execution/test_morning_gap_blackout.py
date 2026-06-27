"""Tests for morning gap entry blackout."""

from __future__ import annotations

import unittest
from datetime import datetime

from robs.execution.hkex_trading_hours import HK, in_morning_gap_entry_blackout_window
from robs.execution.morning_gap_blackout import MorningGapBlackout


class MorningGapBlackoutTests(unittest.TestCase):
    def test_disabled_when_config_zero(self) -> None:
        guard = MorningGapBlackout.from_config({"mhimain": {"morning_gap_blackout_pts": 0}})
        self.assertFalse(guard.enabled)

    def test_night_close_captured_on_idle(self) -> None:
        guard = MorningGapBlackout(gap_pts=200.0)
        night_end = datetime(2026, 6, 27, 2, 59, 0, tzinfo=HK)
        guard.on_session_idle(20100.0, night_end)
        self.assertEqual(guard.night_close_price, 20100.0)

    def test_night_close_updated_on_active_poll(self) -> None:
        guard = MorningGapBlackout(gap_pts=200.0)
        t1 = datetime(2026, 6, 27, 22, 0, 0, tzinfo=HK)
        t2 = datetime(2026, 6, 28, 2, 30, 0, tzinfo=HK)
        guard.note_night_session_price(20050.0, t1)
        guard.note_night_session_price(20100.0, t2)
        self.assertEqual(guard.night_close_price, 20100.0)

    def test_day_poll_does_not_set_night_close(self) -> None:
        guard = MorningGapBlackout(gap_pts=200.0)
        noon = datetime(2026, 6, 27, 10, 0, 0, tzinfo=HK)
        guard.note_night_session_price(20200.0, noon)
        self.assertIsNone(guard.night_close_price)

    def test_lunch_idle_does_not_update_night_close(self) -> None:
        guard = MorningGapBlackout(gap_pts=200.0)
        guard.night_close_price = 20100.0
        lunch = datetime(2026, 6, 27, 12, 5, 0, tzinfo=HK)
        guard.on_session_idle(20250.0, lunch)
        self.assertEqual(guard.night_close_price, 20100.0)

    def test_large_gap_flat_blocks_until_0945(self) -> None:
        guard = MorningGapBlackout(gap_pts=200.0)
        guard.night_close_price = 20000.0
        open_time = datetime(2026, 6, 30, 9, 20, 0, tzinfo=HK)
        guard.note_morning_open_if_due(20300.0, was_flat=True, now=open_time)
        self.assertTrue(guard.active)
        blocked, reason = guard.blocks_entry(open_time)
        self.assertTrue(blocked)
        self.assertIn("09:45", reason)

    def test_small_gap_allows_entry(self) -> None:
        guard = MorningGapBlackout(gap_pts=200.0)
        guard.night_close_price = 20000.0
        open_time = datetime(2026, 6, 30, 9, 20, 0, tzinfo=HK)
        guard.note_morning_open_if_due(20050.0, was_flat=True, now=open_time)
        self.assertFalse(guard.active)
        self.assertFalse(guard.blocks_entry(open_time)[0])

    def test_holding_overnight_not_blocked(self) -> None:
        guard = MorningGapBlackout(gap_pts=200.0)
        guard.night_close_price = 20000.0
        open_time = datetime(2026, 6, 30, 9, 20, 0, tzinfo=HK)
        guard.note_morning_open_if_due(20300.0, was_flat=False, now=open_time)
        self.assertFalse(guard.active)
        self.assertFalse(guard.blocks_entry(open_time)[0])

    def test_blackout_expires_after_0945(self) -> None:
        guard = MorningGapBlackout(gap_pts=200.0)
        guard.night_close_price = 20000.0
        open_time = datetime(2026, 6, 30, 9, 20, 0, tzinfo=HK)
        guard.note_morning_open_if_due(20300.0, was_flat=True, now=open_time)
        after = datetime(2026, 6, 30, 9, 46, 0, tzinfo=HK)
        self.assertFalse(guard.blocks_entry(after)[0])

    def test_gap_at_threshold_triggers_blackout(self) -> None:
        guard = MorningGapBlackout(gap_pts=200.0)
        guard.night_close_price = 20000.0
        open_time = datetime(2026, 6, 30, 9, 20, 0, tzinfo=HK)
        guard.note_morning_open_if_due(20200.0, was_flat=True, now=open_time)
        self.assertTrue(guard.active)

    def test_gap_just_below_threshold_allows_entry(self) -> None:
        guard = MorningGapBlackout(gap_pts=200.0)
        guard.night_close_price = 20000.0
        open_time = datetime(2026, 6, 30, 9, 20, 0, tzinfo=HK)
        guard.note_morning_open_if_due(20199.0, was_flat=True, now=open_time)
        self.assertFalse(guard.active)

    def test_morning_window_helper(self) -> None:
        inside = datetime(2026, 6, 30, 9, 30, 0, tzinfo=HK)
        outside = datetime(2026, 6, 30, 9, 46, 0, tzinfo=HK)
        self.assertTrue(in_morning_gap_entry_blackout_window(inside))
        self.assertFalse(in_morning_gap_entry_blackout_window(outside))


class MorningGapIntegrationTests(unittest.TestCase):
    def test_process_signal_blocks_new_entry(self) -> None:
        from unittest.mock import MagicMock, patch

        from robs.cli.mhimain import _process_signal
        from robs.execution.order_gate import OrderGate
        from robs.execution.position import UnitPositionBook
        from robs.execution.risk import RiskManager
        from robs.strategy.mhimain import MHImainStrategy
        from robs.strategy.rules import Action, Signal
        from robs.strategy.trend import TrendMode

        guard = MorningGapBlackout(gap_pts=200.0)
        guard.night_close_price = 20000.0
        open_time = datetime(2026, 6, 30, 9, 20, 0, tzinfo=HK)
        guard.note_morning_open_if_due(20300.0, was_flat=True, now=open_time)

        risk = RiskManager({"risk": {"max_position_shares": 1}})
        position = UnitPositionBook(contracts=0)
        strategy = MHImainStrategy.from_config(
            {"mhimain": {"respect_hkex_hours": False}}, trend=TrendMode.BULL
        )
        gate = OrderGate()

        with patch("robs.cli.mhimain.execute_unit_order") as mock_order, patch(
            "robs.cli.mhimain.datetime"
        ) as mock_dt:
            mock_dt.now.return_value = open_time.astimezone(
                __import__("datetime").timezone.utc
            )
            mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
            _process_signal(
                {"mhimain": {"respect_hkex_hours": False}},
                MagicMock(),
                position,
                strategy,
                risk,
                Signal("mhimain", Action.BUY, "HK.MHImain", "entry", {}),
                "HK.MHImain",
                {},
                20300.0,
                gate,
                morning_gap=guard,
            )
        mock_order.assert_not_called()

    def test_strategy_holds_during_gap_blackout(self) -> None:
        from unittest.mock import patch

        from robs.execution.position import UnitPositionBook
        from robs.strategy.mhimain import MHImainStrategy
        from robs.strategy.rules import Action
        from robs.strategy.trend import TrendMode

        guard = MorningGapBlackout(gap_pts=200.0)
        guard.night_close_price = 20000.0
        open_time = datetime(2026, 6, 30, 9, 20, 0, tzinfo=HK)
        guard.note_morning_open_if_due(20300.0, was_flat=True, now=open_time)
        strat = MHImainStrategy.from_config(
            {"mhimain": {"respect_hkex_hours": False}}, trend=TrendMode.BULL
        )
        strat.morning_gap = guard
        strat._entry_armed = True
        with patch("robs.execution.morning_gap_blackout.datetime") as mock_dt:
            mock_dt.now.return_value = open_time
            mock_dt.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)
            signal = strat.update(20300.0, UnitPositionBook(contracts=0))
        self.assertEqual(signal.action, Action.HOLD)
        self.assertIn("morning gap blackout", signal.reason)


if __name__ == "__main__":
    unittest.main()
