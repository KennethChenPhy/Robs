"""Tests for daily loss kill switch."""

from __future__ import annotations

import unittest

from robs.execution.risk import RiskManager


class DailyLossKillSwitchTests(unittest.TestCase):
    def test_kills_at_threshold(self) -> None:
        risk = RiskManager({"risk": {"max_daily_loss_pct": 50.0}})
        risk.set_equity(100_000.0)
        risk.refresh_equity(49_999.0)
        self.assertTrue(risk.killed)
        self.assertIn("daily loss", risk.kill_reason)

    def test_no_kill_above_threshold(self) -> None:
        risk = RiskManager({"risk": {"max_daily_loss_pct": 50.0}})
        risk.set_equity(100_000.0)
        risk.refresh_equity(50_001.0)
        self.assertFalse(risk.killed)

    def test_day_start_preserved_on_refresh(self) -> None:
        risk = RiskManager({"risk": {"max_daily_loss_pct": 50.0}})
        risk.set_equity(100_000.0)
        risk.refresh_equity(90_000.0)
        self.assertEqual(risk.day_start_equity, 100_000.0)
        self.assertAlmostEqual(risk.equity_loss_pct(), 10.0)

    def test_disabled_when_no_baseline(self) -> None:
        risk = RiskManager({"risk": {"max_daily_loss_pct": 1.0}})
        risk.refresh_equity(0.0)
        self.assertFalse(risk.killed)

    def test_fetch_account_equity_rejects_na(self) -> None:
        from unittest.mock import MagicMock

        from robs.data.futu_client import TradeClient

        trade = TradeClient.__new__(TradeClient)
        row = MagicMock()
        row.get.return_value = "N/A"
        data = MagicMock()
        data.__len__ = lambda self: 1
        data.iloc = [row]
        trade._ctx = MagicMock()
        trade._ctx.accinfo_query.return_value = (0, data)
        self.assertIsNone(trade.fetch_account_equity({"mhimain": {"trd_env": "SIMULATE"}}))


    def test_stale_when_never_polled(self) -> None:
        risk = RiskManager({"risk": {"stale_poll_multiplier": 10.0}})
        self.assertTrue(risk.check_stale(None, 1.0))

    def test_not_stale_within_threshold(self) -> None:
        risk = RiskManager({"risk": {"stale_poll_multiplier": 10.0}})
        from datetime import datetime, timedelta, timezone

        recent = datetime.now(timezone.utc) - timedelta(seconds=5)
        self.assertFalse(risk.check_stale(recent, 1.0))

    def test_stale_after_threshold(self) -> None:
        risk = RiskManager({"risk": {"stale_poll_multiplier": 10.0}})
        from datetime import datetime, timedelta, timezone

        old = datetime.now(timezone.utc) - timedelta(seconds=11)
        self.assertTrue(risk.check_stale(old, 1.0))


if __name__ == "__main__":
    unittest.main()
