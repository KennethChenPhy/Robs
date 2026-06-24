"""Tests for order gate fill sync."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from robs.execution.order_gate import OrderGate
from robs.execution.position import UnitPositionBook
from robs.execution.position_sync import BrokerPosition
from robs.strategy.mhimain import MHImainStrategy
from robs.strategy.rules import Action
from robs.strategy.trend import TrendMode


class OrderGateFillTests(unittest.TestCase):
    @patch("robs.execution.order_gate.fetch_broker_position")
    def test_close_fill_preserves_entry_for_finalize(self, mock_fetch: MagicMock) -> None:
        mock_fetch.return_value = BrokerPosition(
            code="HK.MHImain",
            contracts=0,
            qty=0,
            entry_price=None,
            current_price=20000.0,
            pnl_points=0.0,
            pnl_val=None,
        )
        trade = MagicMock()
        trade._ctx.order_list_query.return_value = (1, None)

        position = UnitPositionBook(contracts=1)
        strategy = MHImainStrategy.from_config({"mhimain": {}}, trend=TrendMode.BULL)
        strategy.entry_price = 19900.0
        risk = MagicMock()
        risk.position_shares = 1

        gate = OrderGate()
        gate.mark_submitted(order_id="1", side="SELL", signal_action=Action.FLAT, qty=1.0)

        outcome = gate.try_resolve(
            trade, {"mhimain": {}}, "HK.MHImain", position, strategy, risk, 20000.0
        )

        self.assertEqual(outcome, "filled")
        self.assertEqual(position.contracts, 0)
        self.assertEqual(strategy.entry_price, 19900.0)

    def test_is_close_intent(self) -> None:
        gate = OrderGate()
        gate.mark_submitted(order_id="1", side="BUY", signal_action=Action.BUY, qty=1.0)
        self.assertFalse(gate.is_close_intent(0))

        gate.mark_submitted(order_id="2", side="SELL", signal_action=Action.FLAT, qty=1.0)
        self.assertTrue(gate.is_close_intent(1))


if __name__ == "__main__":
    unittest.main()
