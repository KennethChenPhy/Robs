"""Tests for filled-order finalization."""

from __future__ import annotations

import unittest

from robs.execution.order_gate import OrderGate
from robs.execution.position import UnitPositionBook
from robs.execution.risk import RiskManager
from robs.cli.mhimain import _finalize_filled_order
from robs.strategy.mhimain import MHImainStrategy
from robs.strategy.rules import Action
from robs.strategy.trend import TrendMode


class FinalizeFilledOrderTests(unittest.TestCase):
    def test_open_fill_updates_position_when_broker_sync_lagged(self) -> None:
        strategy = MHImainStrategy.from_config({"mhimain": {}}, trend=TrendMode.BULL)
        position = UnitPositionBook(contracts=0)
        risk = RiskManager({"risk": {"max_position_shares": 1}})
        gate = OrderGate()
        gate.mark_submitted(order_id="1", side="BUY", signal_action=Action.BUY, qty=1.0)

        _finalize_filled_order(strategy, risk, position, 20000.0, gate, fill_price=20000.0)

        self.assertEqual(position.contracts, 1)
        self.assertEqual(strategy.entry_price, 20000.0)
        self.assertEqual(risk.position_shares, 1)


if __name__ == "__main__":
    unittest.main()
