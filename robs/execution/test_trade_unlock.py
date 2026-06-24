"""Tests for trade unlock session."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from robs.execution.trade_unlock import TradeUnlockSession, resolve_trade_password


class TradeUnlockTests(unittest.TestCase):
    def test_simulate_skips_password(self) -> None:
        self.assertIsNone(resolve_trade_password({"mhimain": {"trd_env": "SIMULATE"}}))

    def test_expired_with_position_defers_lock(self) -> None:
        session = TradeUnlockSession(valid_days=30, password="secret")
        session.authorized_at = datetime.now(timezone.utc) - timedelta(days=31)
        trade = MagicMock()

        self.assertFalse(session.ensure_authorized({"mhimain": {}}, trade, position_contracts=1))
        self.assertFalse(session.locked)
        trade._ctx.unlock_trade.assert_not_called()

    def test_expired_locks_until_reauthorize(self) -> None:
        trade = MagicMock()
        trade._ctx.unlock_trade.return_value = (0, None)
        cfg = {"mhimain": {"trd_env": "REAL"}}

        session = TradeUnlockSession(valid_days=30)
        self.assertTrue(session.authorize(trade, "secret"))
        session.authorized_at = datetime.now(timezone.utc) - timedelta(days=31)

        with patch("sys.stdin.isatty", return_value=True), patch(
            "getpass.getpass", return_value="secret2"
        ):
            self.assertTrue(session.ensure_authorized(cfg, trade, position_contracts=0))
        trade._ctx.unlock_trade.assert_called_with("secret2")
        self.assertFalse(session.locked)

    def test_expired_non_tty_stays_locked(self) -> None:
        trade = MagicMock()
        session = TradeUnlockSession(valid_days=30, password="secret")
        session.authorized_at = datetime.now(timezone.utc) - timedelta(days=31)
        session.locked = False

        with patch("sys.stdin.isatty", return_value=False):
            self.assertFalse(session.ensure_authorized({"mhimain": {}}, trade, position_contracts=0))
        self.assertTrue(session.locked)

    def test_opend_recovery_does_not_call_unlock(self) -> None:
        trade = MagicMock()
        trade._ctx.unlock_trade.return_value = (0, None)
        session = TradeUnlockSession(valid_days=30, password="secret")
        session.authorize(trade, "secret")
        session.mark_opend_unhealthy()
        trade._ctx.unlock_trade.reset_mock()

        self.assertTrue(session.on_opend_recovered(trade))
        trade._ctx.unlock_trade.assert_not_called()


    def test_warns_when_valid_days_below_three(self) -> None:
        cfg = {"mhimain": {"trd_env": "REAL", "trade_unlock_valid_days": 2}}
        with patch("robs.execution.trade_unlock.LOG") as mock_log:
            from robs.execution.trade_unlock import warn_short_trade_unlock_window

            warn_short_trade_unlock_window(cfg)
        mock_log.warning.assert_called_once()
        self.assertIn("below 3", mock_log.warning.call_args.args[0])

    def test_no_warn_at_three_or_simulate(self) -> None:
        from robs.execution.trade_unlock import warn_short_trade_unlock_window

        with patch("robs.execution.trade_unlock.LOG") as mock_log:
            warn_short_trade_unlock_window({"mhimain": {"trd_env": "REAL", "trade_unlock_valid_days": 3}})
            warn_short_trade_unlock_window({"mhimain": {"trd_env": "SIMULATE", "trade_unlock_valid_days": 1}})
        mock_log.warning.assert_not_called()


if __name__ == "__main__":
    unittest.main()
