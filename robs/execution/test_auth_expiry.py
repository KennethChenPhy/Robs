"""Integration tests for auth-expiry flatten-before-lock."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from robs.cli.mhimain import _handle_auth_expiry_portfolio
from robs.execution.mhi_portfolio import MHIPortfolio
from robs.execution.order_gate import OrderGate
from robs.execution.risk import RiskManager
from robs.execution.trade_unlock import TradeUnlockSession
from robs.strategy.mhimain import MHImainStrategy
from robs.strategy.rules import Action
from robs.strategy.trend import TrendMode


def _portfolio(*, entry_contracts: int = 0) -> MHIPortfolio:
    portfolio = MHIPortfolio.create(
        {"mhimain": {}},
        trend=TrendMode.BULL,
        quote_symbol="HK.MHImain",
    )
    portfolio.update_front_context("HK.MHI2606", None)
    if entry_contracts:
        portfolio.entry_position.contracts = entry_contracts
        portfolio.entry_book_code = "HK.MHI2606"
    return portfolio


class AuthExpiryHandlerTests(unittest.TestCase):
    def _session_expired(self) -> TradeUnlockSession:
        session = TradeUnlockSession(valid_days=30, password="secret")
        session.authorized_at = datetime.now(timezone.utc) - timedelta(days=31)
        return session

    def test_not_expired_passes_through(self) -> None:
        session = TradeUnlockSession(valid_days=30, password="secret")
        session.authorized_at = datetime.now(timezone.utc)
        trade = MagicMock()
        portfolio = _portfolio(entry_contracts=1)
        risk = RiskManager({"risk": {"max_position_shares": 1}})

        self.assertFalse(
            _handle_auth_expiry_portfolio(
                {"mhimain": {}},
                trade,
                portfolio,
                risk,
                MagicMock(),
                "HK.MHImain",
                {},
                {"HK.MHImain": 20000.0},
                OrderGate(),
                session,
            )
        )

    def test_locked_without_expiry_does_not_flatten(self) -> None:
        trade = MagicMock()
        session = TradeUnlockSession(valid_days=30, password="secret")
        session.authorized_at = datetime.now(timezone.utc)
        session.locked = True
        portfolio = _portfolio(entry_contracts=1)
        risk = RiskManager({"risk": {"max_position_shares": 1}})

        with patch("robs.cli.mhimain._submit_forced_flat") as mock_flat:
            self.assertFalse(
                _handle_auth_expiry_portfolio(
                    {"mhimain": {}},
                    trade,
                    portfolio,
                    risk,
                    MagicMock(),
                    "HK.MHImain",
                    {},
                    {"HK.MHImain": 20000.0},
                    OrderGate(),
                    session,
                )
            )
        mock_flat.assert_not_called()

    def test_expired_cancels_pending_entry(self) -> None:
        session = self._session_expired()
        trade = MagicMock()
        portfolio = _portfolio()
        gate = OrderGate()
        gate.mark_submitted(order_id="9", side="BUY", signal_action=Action.BUY, qty=1.0)
        portfolio.entry_strategy.set_order_pending(True)
        risk = RiskManager({"risk": {"max_position_shares": 1}})

        with patch("robs.cli.mhimain._submit_forced_flat") as mock_flat:
            self.assertTrue(
                _handle_auth_expiry_portfolio(
                    {"mhimain": {}},
                    trade,
                    portfolio,
                    risk,
                    MagicMock(),
                    "HK.MHImain",
                    {},
                    {"HK.MHImain": 20000.0},
                    gate,
                    session,
                )
            )
        self.assertFalse(gate.pending)
        mock_flat.assert_not_called()

    @patch("robs.cli.mhimain._submit_forced_flat", return_value=True)
    def test_expired_with_position_submits_flat(self, mock_flat: MagicMock) -> None:
        trade = MagicMock()
        trade._ctx.unlock_trade.return_value = (0, None)
        session = self._session_expired()
        portfolio = _portfolio(entry_contracts=1)
        portfolio.entry_strategy.entry_price = 19900.0
        risk = RiskManager({"risk": {"max_position_shares": 1}})

        self.assertTrue(
            _handle_auth_expiry_portfolio(
                {"mhimain": {"trd_env": "REAL"}},
                trade,
                portfolio,
                risk,
                MagicMock(),
                "HK.MHImain",
                {},
                {"HK.MHImain": 20000.0, "HK.MHI2606": 20000.0},
                OrderGate(),
                session,
            )
        )
        mock_flat.assert_called_once()
        self.assertEqual(mock_flat.call_args.kwargs.get("order_code"), "HK.MHI2606")
        trade._ctx.unlock_trade.assert_called_with("secret")

    @patch("getpass.getpass", return_value="newsecret")
    def test_expired_flat_prompts_reauth(self, _mock_getpass: MagicMock) -> None:
        trade = MagicMock()
        trade._ctx.unlock_trade.return_value = (0, None)
        session = self._session_expired()
        portfolio = _portfolio()
        risk = RiskManager({"risk": {"max_position_shares": 1}})

        with patch("sys.stdin.isatty", return_value=True):
            self.assertFalse(
                _handle_auth_expiry_portfolio(
                    {"mhimain": {}},
                    trade,
                    portfolio,
                    risk,
                    MagicMock(),
                    "HK.MHImain",
                    {},
                    {"HK.MHImain": 20000.0},
                    OrderGate(),
                    session,
                )
            )
        self.assertFalse(session.locked)
        self.assertFalse(session.is_expired())

    def test_broker_unlock_does_not_extend_auth_window(self) -> None:
        trade = MagicMock()
        trade._ctx.unlock_trade.return_value = (0, None)
        session = self._session_expired()
        old_auth = session.authorized_at

        self.assertTrue(session.ensure_broker_unlocked(trade))
        self.assertEqual(session.authorized_at, old_auth)
        self.assertTrue(session.is_expired())

    def test_broker_unlock_cached_until_close_fails(self) -> None:
        trade = MagicMock()
        trade._ctx.unlock_trade.return_value = (0, None)
        session = TradeUnlockSession(valid_days=30, password="secret")

        self.assertTrue(session.ensure_broker_unlocked(trade))
        self.assertTrue(session.ensure_broker_unlocked(trade))
        trade._ctx.unlock_trade.assert_called_once()

        session.note_close_order_failed()
        self.assertTrue(session.ensure_broker_unlocked(trade))
        self.assertEqual(trade._ctx.unlock_trade.call_count, 2)


if __name__ == "__main__":
    unittest.main()
