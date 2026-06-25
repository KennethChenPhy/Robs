"""Tests for HK.MHImain contract rollover."""

from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from robs.cli.mhimain import _handle_contract_rollover, _order_line
from robs.execution.contract_rollover import (
    ContractRolloverManager,
    HKEXFrontCalendar,
    HKEXMHISpot,
    clamp_rollover_target,
    contract_log_label,
    estimate_mhi_last_trade_day,
    hkex_spot_startup_message,
    is_held_ahead_of_front,
    is_held_behind_front,
    is_last_trading_day,
    is_ltd_expiring_month_open_banned,
    is_ltd_expiring_open_window,
    is_ltd_rollover_active,
    is_past_ltd_rollover_deadline,
    is_plausible_rollover_front,
    next_named_mhi_contract,
    normalize_last_trade_time,
    parse_last_trade_date,
    pick_hkex_front_month,
    resolve_entry_order_code,
    resolve_rollover_target,
    should_rollover,
)
from robs.execution.order_gate import OrderGate
from robs.execution.position import UnitPositionBook
from robs.execution.risk import RiskManager
from robs.strategy.mhimain import MHImainStrategy
from robs.strategy.rules import Action
from robs.strategy.trend import TrendMode

HK = ZoneInfo("Asia/Hong_Kong")
JUN_29_2026_MORNING = datetime(2026, 6, 29, 10, 0, tzinfo=HK)
JUN_29_2026_AT_ROLL = datetime(2026, 6, 29, 11, 58, tzinfo=HK)
JUN_29_2026_AFTER_ROLL = datetime(2026, 6, 29, 12, 0, tzinfo=HK)
JUN_29_2026_AFTER_DAY = datetime(2026, 6, 29, 16, 45, tzinfo=HK)
JUN_29_2026_NIGHT = datetime(2026, 6, 29, 18, 0, tzinfo=HK)
JUN_25_2026 = datetime(2026, 6, 25, 10, 0, tzinfo=HK)
LTD_2606 = "2026-06-29 11:58:00"
MHI_CONTRACTS = [
    ("HK.MHI2606", "2026-06-29 16:00:00"),
    ("HK.MHI2607", "2026-07-30 16:15:00"),
    ("HK.MHI2609", "2026-09-29 16:15:00"),
]


class ContractRolloverLogicTests(unittest.TestCase):
    def test_normalize_last_trade_time(self) -> None:
        self.assertEqual(normalize_last_trade_time("2026-06-29 16:00:00"), LTD_2606)
        self.assertEqual(normalize_last_trade_time("2026-06-29"), LTD_2606)

    def test_parse_last_trade_date(self) -> None:
        self.assertEqual(parse_last_trade_date(LTD_2606), datetime(2026, 6, 29).date())

    def test_is_last_trading_day(self) -> None:
        self.assertTrue(is_last_trading_day(LTD_2606, now=JUN_29_2026_MORNING))
        self.assertFalse(is_last_trading_day("2026-06-28 11:58:00", now=JUN_29_2026_MORNING))

    def test_is_past_ltd_rollover_deadline(self) -> None:
        self.assertFalse(
            is_past_ltd_rollover_deadline(LTD_2606, now=JUN_29_2026_MORNING, normalized=True),
        )
        self.assertTrue(
            is_past_ltd_rollover_deadline(LTD_2606, now=JUN_29_2026_AFTER_ROLL, normalized=True),
        )

    def test_is_ltd_rollover_active(self) -> None:
        self.assertFalse(is_ltd_rollover_active(LTD_2606, now=JUN_29_2026_MORNING, normalized=True))
        self.assertTrue(is_ltd_rollover_active(LTD_2606, now=JUN_29_2026_AT_ROLL, normalized=True))
        self.assertTrue(is_ltd_rollover_active(LTD_2606, now=JUN_29_2026_AFTER_ROLL, normalized=True))

    def test_ltd_expiring_open_ban_window(self) -> None:
        self.assertFalse(
            is_ltd_expiring_open_window("HK.MHI2606", LTD_2606, now=JUN_29_2026_MORNING),
        )
        self.assertTrue(
            is_ltd_expiring_open_window("HK.MHI2606", LTD_2606, now=JUN_29_2026_AT_ROLL),
        )
        self.assertTrue(
            is_ltd_expiring_open_window("HK.MHI2606", LTD_2606, now=JUN_29_2026_AFTER_ROLL),
        )
        self.assertFalse(
            is_ltd_expiring_open_window("HK.MHI2606", LTD_2606, now=JUN_29_2026_AFTER_DAY),
        )
        self.assertTrue(
            is_ltd_expiring_month_open_banned(
                "HK.MHI2606", "HK.MHI2606", LTD_2606, now=JUN_29_2026_AFTER_ROLL,
            ),
        )
        self.assertFalse(
            is_ltd_expiring_month_open_banned(
                "HK.MHI2607", "HK.MHI2606", LTD_2606, now=JUN_29_2026_AFTER_ROLL,
            ),
        )

    def test_pick_hkex_front_month_on_last_day_morning(self) -> None:
        self.assertEqual(
            pick_hkex_front_month(MHI_CONTRACTS, now=JUN_29_2026_MORNING),
            "HK.MHI2606",
        )

    def test_pick_hkex_front_month_flips_at_night(self) -> None:
        self.assertEqual(
            pick_hkex_front_month(MHI_CONTRACTS, now=JUN_29_2026_NIGHT),
            "HK.MHI2607",
        )

    def test_pick_hkex_front_month_after_june_expires(self) -> None:
        jun_30 = datetime(2026, 6, 30, 10, 0, tzinfo=HK)
        self.assertEqual(pick_hkex_front_month(MHI_CONTRACTS, now=jun_30), "HK.MHI2607")

    def test_trade_normally_before_ltd_rollover_time(self) -> None:
        front = pick_hkex_front_month(MHI_CONTRACTS, now=JUN_29_2026_MORNING)
        held = "HK.MHI2606"
        self.assertEqual(front, held)
        ok, reason = should_rollover(held, front, LTD_2606, now=JUN_29_2026_MORNING)
        self.assertFalse(ok)
        self.assertEqual(reason, "")
        self.assertEqual(
            resolve_entry_order_code(
                "HK.MHImain",
                "HK.MHI2606",
                front_last_trade_time=LTD_2606,
                now=JUN_29_2026_MORNING,
            ),
            "HK.MHI2606",
        )

    def test_rollover_triggers_at_ltd_1158(self) -> None:
        front = pick_hkex_front_month(MHI_CONTRACTS, now=JUN_29_2026_AT_ROLL)
        self.assertEqual(front, "HK.MHI2606")
        ok, reason = should_rollover(
            "HK.MHI2606",
            front,
            LTD_2606,
            now=JUN_29_2026_AT_ROLL,
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "last_trading_day")

    def test_no_rollover_when_not_last_trading_day(self) -> None:
        ok, reason = should_rollover(
            "HK.MHI2606",
            "HK.MHI2606",
            "2026-07-30 11:58:00",
            now=JUN_29_2026_MORNING,
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "")

    def test_no_rollover_when_held_ahead_of_front(self) -> None:
        ok, reason = should_rollover(
            "HK.MHI2607",
            "HK.MHI2606",
            LTD_2606,
            now=JUN_29_2026_MORNING,
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "")

    def test_is_held_behind_front(self) -> None:
        self.assertTrue(is_held_behind_front("HK.MHI2606", "HK.MHI2607"))
        self.assertFalse(is_held_behind_front("HK.MHI2607", "HK.MHI2606"))
        self.assertFalse(is_held_behind_front("HK.MHI2606", "HK.MHI2606"))

    def test_is_held_ahead_of_front(self) -> None:
        self.assertTrue(is_held_ahead_of_front("HK.MHI2607", "HK.MHI2606"))
        self.assertFalse(is_held_ahead_of_front("HK.MHI2606", "HK.MHI2607"))

    def test_no_rollover_when_disabled(self) -> None:
        ok, reason = should_rollover(
            "HK.MHI2606",
            "HK.MHI2606",
            LTD_2606,
            now=JUN_29_2026_AFTER_ROLL,
            on_last_trade_day=False,
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "")

    def test_order_line_includes_contract(self) -> None:
        line = _order_line(
            "BUY",
            1,
            20000.0,
            {"ok": True, "filled": True, "status": "FILLED"},
            contract="HK.MHI2607",
        )
        self.assertIn("MHI2607", line)
        self.assertIn("BUY x1", line)

    def test_no_rollover_before_deadline_even_when_held_is_front(self) -> None:
        ok, reason = should_rollover(
            "HK.MHI2606",
            "HK.MHI2606",
            LTD_2606,
            now=JUN_29_2026_MORNING,
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "")

    def test_catch_up_rollover_when_held_behind_front(self) -> None:
        """After front flips (e.g. 17:15 on LTD), stale entry on expiring month must roll."""
        ok, reason = should_rollover(
            "HK.MHI2606",
            "HK.MHI2607",
            LTD_2606,
            now=JUN_29_2026_NIGHT,
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "held_behind_front")
        self.assertEqual(
            resolve_rollover_target(
                "HK.MHI2606",
                "HK.MHI2607",
                last_trade_time=LTD_2606,
                now=JUN_29_2026_NIGHT,
            ),
            "HK.MHI2607",
        )

    def test_catch_up_rollover_after_ltd_day(self) -> None:
        jun_30 = datetime(2026, 6, 30, 10, 0, tzinfo=HK)
        ok, reason = should_rollover(
            "HK.MHI2606",
            "HK.MHI2607",
            LTD_2606,
            now=jun_30,
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "held_behind_front")

    def test_enforce_rollover_after_deadline_when_still_in_expiring_month(self) -> None:
        ok, reason = should_rollover(
            "HK.MHI2606",
            "HK.MHI2607",
            LTD_2606,
            now=JUN_29_2026_AFTER_ROLL,
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "held_behind_front")
        self.assertEqual(
            resolve_rollover_target(
                "HK.MHI2606",
                "HK.MHI2607",
                last_trade_time=LTD_2606,
                now=JUN_29_2026_AFTER_ROLL,
            ),
            "HK.MHI2607",
        )

    def test_rollover_when_held_is_front_after_deadline(self) -> None:
        ok, reason = should_rollover(
            "HK.MHI2606",
            "HK.MHI2606",
            LTD_2606,
            now=JUN_29_2026_AFTER_ROLL,
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "last_trading_day")
        self.assertEqual(
            resolve_rollover_target(
                "HK.MHI2606",
                "HK.MHI2606",
                last_trade_time=LTD_2606,
                now=JUN_29_2026_AFTER_ROLL,
            ),
            "HK.MHI2607",
        )

    def test_contract_log_label(self) -> None:
        self.assertEqual(contract_log_label("HK.MHI2607"), "MHI2607")
        self.assertEqual(contract_log_label("HK.MHImain"), "MHImain")

    def test_year_end_rollover_after_deadline(self) -> None:
        dec_30_noon = datetime(2025, 12, 30, 12, 0, tzinfo=HK)
        ltd = "2025-12-30 11:58:00"
        ok, reason = should_rollover(
            "HK.MHI2512",
            "HK.MHI2512",
            ltd,
            now=dec_30_noon,
        )
        self.assertTrue(ok)
        self.assertEqual(
            resolve_rollover_target(
                "HK.MHI2512",
                "HK.MHI2512",
                last_trade_time=ltd,
                now=dec_30_noon,
            ),
            "HK.MHI2601",
        )

    def test_resolve_entry_always_front(self) -> None:
        self.assertEqual(resolve_entry_order_code("HK.MHImain", "HK.MHI2606"), "HK.MHI2606")

    def test_resolve_entry_roll_day_opens_next_month_after_1158(self) -> None:
        self.assertEqual(
            resolve_entry_order_code(
                "HK.MHImain",
                "HK.MHI2606",
                front_last_trade_time=LTD_2606,
                now=JUN_29_2026_AFTER_ROLL,
            ),
            "HK.MHI2607",
        )

    def test_resolve_entry_stays_front_before_1158_on_ltd(self) -> None:
        self.assertEqual(
            resolve_entry_order_code(
                "HK.MHImain",
                "HK.MHI2606",
                front_last_trade_time=LTD_2606,
                now=JUN_29_2026_MORNING,
            ),
            "HK.MHI2606",
        )

    def test_resolve_entry_next_month_even_if_roll_flag_off_in_ban_window(self) -> None:
        self.assertEqual(
            resolve_entry_order_code(
                "HK.MHImain",
                "HK.MHI2606",
                front_last_trade_time=LTD_2606,
                roll_on_last_trade_day=False,
                now=JUN_29_2026_AFTER_ROLL,
            ),
            "HK.MHI2607",
        )

    def test_resolve_entry_next_month_after_day_close_gap_on_ltd(self) -> None:
        """16:30–17:15 on LTD: ban window ended but front still expiring month."""
        self.assertEqual(
            resolve_entry_order_code(
                "HK.MHImain",
                "HK.MHI2606",
                front_last_trade_time=LTD_2606,
                roll_on_last_trade_day=False,
                now=JUN_29_2026_AFTER_DAY,
            ),
            "HK.MHI2607",
        )

    def test_next_named_mhi_contract_year_roll(self) -> None:
        self.assertEqual(next_named_mhi_contract("HK.MHI2512"), "HK.MHI2601")

    def test_estimate_mhi_last_trade_day_june_2026(self) -> None:
        self.assertEqual(estimate_mhi_last_trade_day(2026, 6), datetime(2026, 6, 29).date())

    def test_hkex_mhi_spot_jun_25_2026(self) -> None:
        spot = HKEXMHISpot.resolve(now=JUN_25_2026)
        self.assertIsNotNone(spot)
        assert spot is not None
        self.assertEqual(spot.front, "HK.MHI2606")
        self.assertEqual(spot.next, "HK.MHI2607")
        self.assertIsNotNone(spot.front_ltd)
        self.assertIsNotNone(spot.next_ltd)

    def test_hkex_spot_startup_message(self) -> None:
        spot = HKEXMHISpot.resolve(now=JUN_25_2026)
        assert spot is not None
        msg = hkex_spot_startup_message(spot)
        self.assertIn("MHImain MHI2606", msg)
        self.assertIn("next MHI2607", msg)
        self.assertIn("LTD 2026-06-29", msg)
        self.assertIn("LTD 2026-07-30", msg)

    def test_hkex_calendar_cached_without_opend(self) -> None:
        cal = HKEXFrontCalendar()
        a = cal.spot(now=JUN_25_2026)
        b = cal.spot(now=JUN_25_2026.replace(hour=14))
        self.assertIs(a, b)

    def test_hkex_calendar_flips_after_ltd_night(self) -> None:
        cal = HKEXFrontCalendar()
        morning = cal.spot(now=JUN_29_2026_MORNING)
        assert morning is not None
        self.assertEqual(morning.front, "HK.MHI2606")
        night = cal.spot(now=JUN_29_2026_NIGHT)
        assert night is not None
        self.assertEqual(night.front, "HK.MHI2607")
        self.assertNotEqual(cal.spot_key(), (morning.front, morning.next))

    def test_hkex_calendar_flips_day_after_ltd(self) -> None:
        cal = HKEXFrontCalendar()
        morning = cal.spot(now=JUN_29_2026_MORNING)
        assert morning is not None
        self.assertEqual(morning.front, "HK.MHI2606")
        jun_30 = datetime(2026, 6, 30, 10, 0, tzinfo=HK)
        after = cal.spot(now=jun_30)
        assert after is not None
        self.assertEqual(after.front, "HK.MHI2607")
        self.assertEqual(after.next, "HK.MHI2608")

    def test_catch_up_rollover_when_two_months_behind(self) -> None:
        """Stale held month two months behind front rolls one step at a time."""
        jun_30 = datetime(2026, 6, 30, 10, 0, tzinfo=HK)
        ok, reason = should_rollover(
            "HK.MHI2605",
            "HK.MHI2607",
            "2026-05-29 11:58:00",
            now=jun_30,
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "held_behind_front")
        self.assertEqual(
            resolve_rollover_target(
                "HK.MHI2605",
                "HK.MHI2607",
                last_trade_time="2026-05-29 11:58:00",
                now=jun_30,
            ),
            "HK.MHI2606",
        )

    def test_implausible_broker_front_clamped_to_next_month(self) -> None:
        """Bad OpenD sim data (2606 held, 2612 front) must roll one month only."""
        self.assertFalse(is_plausible_rollover_front("HK.MHI2606", "HK.MHI2612"))
        self.assertEqual(
            clamp_rollover_target("HK.MHI2606", "HK.MHI2612"),
            "HK.MHI2607",
        )
        target = resolve_rollover_target(
            "HK.MHI2606",
            "HK.MHI2612",
            last_trade_time=LTD_2606,
            now=JUN_29_2026_NIGHT,
        )
        self.assertEqual(target, "HK.MHI2607")

    def test_should_rollover_rejects_multi_month_broker_front(self) -> None:
        ok, reason = should_rollover(
            "HK.MHI2606",
            "HK.MHI2612",
            LTD_2606,
            now=JUN_29_2026_NIGHT,
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "held_behind_front")
        self.assertEqual(
            resolve_rollover_target(
                "HK.MHI2606",
                "HK.MHI2612",
                last_trade_time=LTD_2606,
                now=JUN_29_2026_NIGHT,
            ),
            "HK.MHI2607",
        )


class ContractRolloverHandlerTests(unittest.TestCase):
    @patch("robs.cli.mhimain._process_signal")
    def test_starts_close_on_last_trading_day(self, mock_process: MagicMock) -> None:
        cfg = {"mhimain": {"contract_rollover": True}}
        rollover = ContractRolloverManager("HK.MHImain", cfg)
        position = UnitPositionBook(contracts=1)
        strategy = MHImainStrategy.from_config(cfg, trend=TrendMode.BULL)
        risk = RiskManager({"risk": {"max_position_shares": 1}})

        with patch("robs.cli.mhimain.datetime") as mock_dt:
            mock_dt.now.return_value = JUN_29_2026_AFTER_ROLL
            mock_dt.strptime = datetime.strptime
            busy = _handle_contract_rollover(
                cfg,
                MagicMock(),
                MagicMock(),
                position,
                strategy,
                risk,
                "HK.MHImain",
                None,
                20000.0,
                OrderGate(),
                None,
                rollover,
                front_contract="HK.MHI2606",
                held_contract="HK.MHI2606",
                last_trade_time=LTD_2606,
            )
        self.assertTrue(busy)
        mock_process.assert_called_once()
        self.assertEqual(mock_process.call_args.kwargs.get("order_code"), "HK.MHI2606")
        self.assertEqual(rollover.state.phase, "close")
        self.assertEqual(rollover.state.target_contract, "HK.MHI2607")

    @patch("robs.cli.mhimain._process_signal")
    def test_no_close_before_ltd_rollover_time(self, mock_process: MagicMock) -> None:
        cfg = {"mhimain": {"contract_rollover": True}}
        rollover = ContractRolloverManager("HK.MHImain", cfg)
        position = UnitPositionBook(contracts=1)
        strategy = MHImainStrategy.from_config(cfg, trend=TrendMode.BULL)
        risk = RiskManager({"risk": {"max_position_shares": 1}})

        with patch("robs.cli.mhimain.datetime") as mock_dt:
            mock_dt.now.return_value = JUN_29_2026_MORNING
            mock_dt.strptime = datetime.strptime
            busy = _handle_contract_rollover(
                cfg,
                MagicMock(),
                MagicMock(),
                position,
                strategy,
                risk,
                "HK.MHImain",
                None,
                20000.0,
                OrderGate(),
                None,
                rollover,
                front_contract="HK.MHI2606",
                held_contract="HK.MHI2606",
                last_trade_time=LTD_2606,
            )
        self.assertFalse(busy)
        mock_process.assert_not_called()
        self.assertEqual(rollover.state.phase, "idle")

    @patch("robs.cli.mhimain._process_signal")
    def test_no_close_when_not_last_trading_day(self, mock_process: MagicMock) -> None:
        cfg = {"mhimain": {"contract_rollover": True}}
        rollover = ContractRolloverManager("HK.MHImain", cfg)
        position = UnitPositionBook(contracts=1)
        strategy = MHImainStrategy.from_config(cfg, trend=TrendMode.BULL)
        risk = RiskManager({"risk": {"max_position_shares": 1}})

        with patch("robs.cli.mhimain.datetime") as mock_dt:
            mock_dt.now.return_value = JUN_29_2026_MORNING
            mock_dt.strptime = datetime.strptime
            busy = _handle_contract_rollover(
                cfg,
                MagicMock(),
                MagicMock(),
                position,
                strategy,
                risk,
                "HK.MHImain",
                None,
                20000.0,
                OrderGate(),
                None,
                rollover,
                front_contract="HK.MHI2606",
                held_contract="HK.MHI2606",
                last_trade_time="2026-07-30 11:58:00",
            )
        self.assertFalse(busy)
        mock_process.assert_not_called()
        self.assertEqual(rollover.state.phase, "idle")

    @patch("robs.cli.mhimain._process_signal")
    def test_no_rollover_when_held_ahead_of_front(self, mock_process: MagicMock) -> None:
        cfg = {"mhimain": {"contract_rollover": True}}
        rollover = ContractRolloverManager("HK.MHImain", cfg)
        position = UnitPositionBook(contracts=1)
        strategy = MHImainStrategy.from_config(cfg, trend=TrendMode.BULL)
        risk = RiskManager({"risk": {"max_position_shares": 1}})

        with patch("robs.cli.mhimain.datetime") as mock_dt:
            mock_dt.now.return_value = JUN_29_2026_MORNING
            mock_dt.strptime = datetime.strptime
            busy = _handle_contract_rollover(
                cfg,
                MagicMock(),
                MagicMock(),
                position,
                strategy,
                risk,
                "HK.MHImain",
                None,
                20000.0,
                OrderGate(),
                None,
                rollover,
                front_contract="HK.MHI2606",
                held_contract="HK.MHI2607",
                last_trade_time=LTD_2606,
            )
        self.assertFalse(busy)
        mock_process.assert_not_called()
        self.assertEqual(rollover.state.phase, "idle")

    @patch("robs.cli.mhimain._process_signal")
    def test_enforces_close_after_rollover_deadline(self, mock_process: MagicMock) -> None:
        cfg = {"mhimain": {"contract_rollover": True}}
        rollover = ContractRolloverManager("HK.MHImain", cfg)
        position = UnitPositionBook(contracts=1)
        strategy = MHImainStrategy.from_config(cfg, trend=TrendMode.BULL)
        risk = RiskManager({"risk": {"max_position_shares": 1}})

        with patch("robs.cli.mhimain.datetime") as mock_dt:
            mock_dt.now.return_value = JUN_29_2026_AFTER_ROLL
            mock_dt.strptime = datetime.strptime
            busy = _handle_contract_rollover(
                cfg,
                MagicMock(),
                MagicMock(),
                position,
                strategy,
                risk,
                "HK.MHImain",
                None,
                20000.0,
                OrderGate(),
                None,
                rollover,
                front_contract="HK.MHI2607",
                held_contract="HK.MHI2606",
                last_trade_time=LTD_2606,
            )
        self.assertTrue(busy)
        mock_process.assert_called_once()
        self.assertEqual(rollover.state.target_contract, "HK.MHI2607")

    @patch("robs.cli.mhimain._process_signal")
    def test_opens_front_after_flat(self, mock_process: MagicMock) -> None:
        cfg = {"mhimain": {"contract_rollover": True}}
        rollover = ContractRolloverManager("HK.MHImain", cfg)
        rollover.state.phase = "open"
        rollover.state.target_contract = "HK.MHI2607"
        rollover.state.direction = -1
        rollover.state.reason = "last_trading_day"
        position = UnitPositionBook(contracts=0)
        strategy = MHImainStrategy.from_config(cfg, trend=TrendMode.BEAR)
        risk = RiskManager({"risk": {"max_position_shares": 1}})

        busy = _handle_contract_rollover(
            cfg,
            MagicMock(),
            MagicMock(),
            position,
            strategy,
            risk,
            "HK.MHImain",
            None,
            20000.0,
            OrderGate(),
            None,
            rollover,
            front_contract="HK.MHI2607",
            held_contract=None,
            last_trade_time=None,
        )
        self.assertTrue(busy)
        mock_process.assert_called_once()
        self.assertEqual(mock_process.call_args.kwargs.get("order_code"), "HK.MHI2607")

    @patch("robs.cli.mhimain._process_signal")
    def test_rollover_on_last_day_when_held_equals_front(self, mock_process: MagicMock) -> None:
        cfg = {"mhimain": {"contract_rollover": True}}
        rollover = ContractRolloverManager("HK.MHImain", cfg)
        position = UnitPositionBook(contracts=1)
        strategy = MHImainStrategy.from_config(cfg, trend=TrendMode.BULL)
        risk = RiskManager({"risk": {"max_position_shares": 1}})

        with patch("robs.cli.mhimain.datetime") as mock_dt:
            mock_dt.now.return_value = JUN_29_2026_AFTER_ROLL
            mock_dt.strptime = datetime.strptime
            busy = _handle_contract_rollover(
                cfg,
                MagicMock(),
                MagicMock(),
                position,
                strategy,
                risk,
                "HK.MHImain",
                None,
                20000.0,
                OrderGate(),
                None,
                rollover,
                front_contract="HK.MHI2606",
                held_contract="HK.MHI2606",
                last_trade_time=LTD_2606,
            )
        self.assertTrue(busy)
        self.assertEqual(rollover.state.target_contract, "HK.MHI2607")
        self.assertEqual(mock_process.call_args.kwargs.get("order_code"), "HK.MHI2606")

    def test_resolve_pending_close_advances_to_open(self) -> None:
        from robs.cli.mhimain import _resolve_rollover_pending
        from robs.execution.position_sync import BrokerPosition

        cfg = {"mhimain": {"contract_rollover": True}}
        rollover = ContractRolloverManager("HK.MHImain", cfg)
        rollover.state.phase = "close"
        rollover.state.held_contract = "HK.MHI2606"
        rollover.state.target_contract = "HK.MHI2607"
        rollover.state.direction = 1
        position = UnitPositionBook(contracts=1)
        strategy = MHImainStrategy.from_config(cfg, trend=TrendMode.BULL)
        risk = RiskManager({"risk": {"max_position_shares": 1}})
        gate = OrderGate()
        gate.mark_submitted(
            order_id="1",
            side="SELL",
            signal_action=Action.FLAT,
            qty=1.0,
            order_code="HK.MHI2606",
        )
        strategy.set_order_pending(True)
        trade = MagicMock()

        with patch.object(gate, "try_resolve", return_value="filled") as mock_resolve:
            with patch.object(gate, "_fetch_broker_for_gate") as mock_fetch:
                mock_fetch.return_value = BrokerPosition(
                    code="HK.MHI2606",
                    contracts=0,
                    qty=0,
                    entry_price=None,
                    current_price=20000.0,
                    pnl_points=0.0,
                    pnl_val=None,
                )
                still_pending = _resolve_rollover_pending(
                    cfg, trade, position, strategy, risk, "HK.MHImain", 20000.0, gate, rollover,
                )
        mock_resolve.assert_called_once()
        self.assertFalse(still_pending)
        self.assertEqual(position.contracts, 0)
        self.assertEqual(rollover.state.phase, "open")


if __name__ == "__main__":
    unittest.main()
