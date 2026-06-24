"""Tests for kill-switch flatten and forced flat bypass."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from robs.cli.mhimain import _handle_kill_switch, _process_signal
from robs.execution.order_gate import OrderGate
from robs.execution.position import UnitPositionBook
from robs.execution.risk import RiskManager
from robs.strategy.mhimain import MHImainStrategy
from robs.strategy.rules import Action, Signal
from robs.strategy.trend import TrendMode


class KillSwitchFlattenTests(unittest.TestCase):
    def test_force_flat_bypasses_killed(self) -> None:
        risk = RiskManager({"risk": {"max_position_shares": 1}})
        risk.killed = True
        risk.kill_reason = "daily loss"
        position = UnitPositionBook(contracts=1)
        strategy = MHImainStrategy.from_config({"mhimain": {}}, trend=TrendMode.BULL)
        gate = OrderGate()

        with patch("robs.cli.mhimain.execute_unit_order") as mock_order:
            mock_order.return_value = {"ok": False, "status": "rejected", "reason": "test"}
            _process_signal(
                {"mhimain": {}},
                MagicMock(),
                position,
                strategy,
                risk,
                Signal("kill", Action.FLAT, "HK.MHImain", "close", {}),
                "HK.MHImain",
                None,
                20000.0,
                gate,
                force_flat=True,
            )
        mock_order.assert_called_once()

    @patch("robs.cli.mhimain._submit_forced_flat", return_value=False)
    def test_kill_switch_flat_returns_false(self, _mock_flat: MagicMock) -> None:
        risk = RiskManager({"risk": {"max_position_shares": 1}})
        risk.killed = True
        risk.kill_reason = "daily loss"
        position = UnitPositionBook(contracts=0)
        strategy = MHImainStrategy.from_config({"mhimain": {}}, trend=TrendMode.BULL)

        self.assertFalse(
            _handle_kill_switch(
                {"mhimain": {}},
                MagicMock(),
                position,
                strategy,
                risk,
                "HK.MHImain",
                None,
                20000.0,
                OrderGate(),
                None,
            )
        )

    @patch("robs.cli.mhimain._submit_forced_flat", return_value=True)
    def test_kill_switch_submits_flat(self, mock_flat: MagicMock) -> None:
        risk = RiskManager({"risk": {"max_position_shares": 1}})
        risk.killed = True
        risk.kill_reason = "daily loss 50.00% >= 50.0%"
        position = UnitPositionBook(contracts=1)
        strategy = MHImainStrategy.from_config({"mhimain": {}}, trend=TrendMode.BULL)

        self.assertTrue(
            _handle_kill_switch(
                {"mhimain": {}},
                MagicMock(),
                position,
                strategy,
                risk,
                "HK.MHImain",
                None,
                20000.0,
                OrderGate(),
                None,
            )
        )
        mock_flat.assert_called_once()


if __name__ == "__main__":
    unittest.main()
