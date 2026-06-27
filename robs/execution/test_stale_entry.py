"""Tests for stale-quote entry blocking in mhimain."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from robs.cli.mhimain import _log_quote_stale, _process_signal, _stale_log_gate, execute_unit_order
from robs.execution.order_gate import OrderGate
from robs.execution.position import UnitPositionBook
from robs.execution.quote_staleness import QuoteFreshness, assess_quote_freshness
from robs.execution.risk import RiskManager
from robs.strategy.mhimain import MHImainStrategy
from robs.strategy.rules import Action, Signal
from robs.strategy.trend import TrendMode

HK = ZoneInfo("Asia/Hong_Kong")


class StaleEntryBlockTests(unittest.TestCase):
    def setUp(self) -> None:
        _stale_log_gate._last = None

    def test_stale_blocks_new_entry_not_close(self) -> None:
        risk = RiskManager({"risk": {"max_position_shares": 1}})
        position = UnitPositionBook(contracts=0)
        strategy = MHImainStrategy.from_config(
            {"mhimain": {"respect_hkex_hours": False}}, trend=TrendMode.BULL
        )
        gate = OrderGate()
        stale = QuoteFreshness(poll_stale=True, data_stale=False, threshold_sec=30.0)

        with patch("robs.cli.mhimain.execute_unit_order") as mock_order:
            _process_signal(
                {"mhimain": {"respect_hkex_hours": False}},
                MagicMock(),
                position,
                strategy,
                risk,
                Signal("mhimain", Action.BUY, "HK.MHImain", "entry", {}),
                "HK.MHImain",
                None,
                20000.0,
                gate,
                quote_freshness=stale,
            )
        mock_order.assert_not_called()

    def test_stale_allows_close(self) -> None:
        risk = RiskManager({"risk": {"max_position_shares": 1}})
        position = UnitPositionBook(contracts=1)
        strategy = MHImainStrategy.from_config(
            {"mhimain": {"respect_hkex_hours": False}}, trend=TrendMode.BULL
        )
        strategy.entry_price = 19900.0
        gate = OrderGate()
        stale = QuoteFreshness(data_stale=True, threshold_sec=30.0)

        with patch("robs.cli.mhimain.execute_unit_order") as mock_order:
            mock_order.return_value = {"ok": False, "status": "rejected", "reason": "test"}
            _process_signal(
                {"mhimain": {"respect_hkex_hours": False}},
                MagicMock(),
                position,
                strategy,
                risk,
                Signal("mhimain", Action.FLAT, "HK.MHImain", "cut loss", {}),
                "HK.MHImain",
                None,
                20000.0,
                gate,
                quote_freshness=stale,
            )
        mock_order.assert_called_once()

    def test_force_flat_bypasses_stale(self) -> None:
        risk = RiskManager({"risk": {"max_position_shares": 1}})
        position = UnitPositionBook(contracts=1)
        strategy = MHImainStrategy.from_config(
            {"mhimain": {"respect_hkex_hours": False}}, trend=TrendMode.BULL
        )
        strategy.entry_price = 19900.0
        gate = OrderGate()
        stale = QuoteFreshness(poll_stale=True, data_stale=True, threshold_sec=30.0)

        with patch("robs.cli.mhimain.execute_unit_order") as mock_order:
            mock_order.return_value = {"ok": False, "status": "rejected", "reason": "test"}
            _process_signal(
                {"mhimain": {"respect_hkex_hours": False}},
                MagicMock(),
                position,
                strategy,
                risk,
                Signal("kill", Action.FLAT, "HK.MHImain", "close", {}),
                "HK.MHImain",
                None,
                20000.0,
                gate,
                quote_freshness=stale,
                force_flat=True,
            )
        mock_order.assert_called_once()

    def test_stale_log_gate_dedupes(self) -> None:
        fresh = QuoteFreshness(poll_stale=True, threshold_sec=30.0)
        with patch("robs.cli.mhimain.LOG") as mock_log:
            _log_quote_stale(fresh, event="quote_stale")
            _log_quote_stale(fresh, event="quote_stale")
        mock_log.warning.assert_called_once()

    def test_recovery_after_gap_blocks_then_clears(self) -> None:
        hk_now = datetime(2026, 6, 24, 16, 30, 0, tzinfo=HK)
        prev = hk_now.astimezone(timezone.utc) - timedelta(seconds=35)
        now = hk_now.astimezone(timezone.utc)
        cfg = {"risk": {"stale_poll_multiplier": 10.0}}

        stale = assess_quote_freshness(
            cfg, 1.0, last_successful_poll_at=prev, data_time="16:29:00", now=now
        )
        self.assertTrue(stale.block_entries)

        fresh = assess_quote_freshness(
            cfg,
            1.0,
            last_successful_poll_at=now,
            data_time="16:29:59",
            now=(hk_now + timedelta(seconds=1)).astimezone(timezone.utc),
        )
        self.assertFalse(fresh.block_entries)


class ExecuteUnitOrderTests(unittest.TestCase):
    def test_rejects_when_portfolio_exceeds_max_position(self) -> None:
        risk = RiskManager({"risk": {"max_position_shares": 1}})
        risk.position_shares = 1
        position = UnitPositionBook(contracts=0)
        trade = MagicMock()
        result = execute_unit_order(
            {"mhimain": {"respect_hkex_hours": False}},
            trade,
            position,
            Action.BUY,
            "HK.MHImain",
            quote_row={"last_price": 20000.0, "ask_price": 20001.0, "bid_price": 19999.0},
            risk=risk,
            portfolio_total_signed=1,
        )
        self.assertEqual(result["status"], "rejected")
        trade.place_market_order.assert_not_called()


if __name__ == "__main__":
    unittest.main()
