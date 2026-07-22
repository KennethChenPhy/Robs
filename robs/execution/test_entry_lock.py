"""Tests for mhimain entry lock (manual assist mode)."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from robs.cli.mhimain import _process_signal
from robs.config import entry_lock_enabled
from robs.execution.order_gate import OrderGate
from robs.execution.position import UnitPositionBook
from robs.execution.risk import RiskManager
from robs.strategy.mhimain import MHImainStrategy
from robs.strategy.rules import Action, Signal
from robs.strategy.trend import TrendMode


class EntryLockConfigTests(unittest.TestCase):
    def test_default_off(self) -> None:
        self.assertFalse(entry_lock_enabled({"mhimain": {}}))

    def test_enabled_from_yaml(self) -> None:
        self.assertTrue(entry_lock_enabled({"mhimain": {"entry_lock": True}}))


class EntryLockStrategyTests(unittest.TestCase):
    def test_flat_holds_when_locked(self) -> None:
        strategy = MHImainStrategy.from_config(
            {"mhimain": {"entry_lock": True}},
            trend=TrendMode.BULL,
        )
        position = UnitPositionBook(contracts=0)
        signal = strategy.update(20000.0, position)
        self.assertEqual(signal.action, Action.HOLD)
        self.assertIn("entry lock on", signal.reason)

    def test_exit_still_signals_when_locked(self) -> None:
        strategy = MHImainStrategy.from_config(
            {"mhimain": {"entry_lock": True, "take_profit_pts": 100}},
            trend=TrendMode.BULL,
        )
        position = UnitPositionBook(contracts=1)
        strategy.entry_price = 19900.0
        signal = strategy.update(20150.0, position)
        self.assertEqual(signal.action, Action.FLAT)
        self.assertIn("take profit", signal.reason)

    def test_rearm_disabled_when_locked(self) -> None:
        strategy = MHImainStrategy.from_config(
            {"mhimain": {"entry_lock": True}},
            trend=TrendMode.BULL,
        )
        position = UnitPositionBook(contracts=0)
        strategy.rearm_entry_if_flat(position)
        self.assertFalse(strategy.entry_armed)


class EntryLockProcessSignalTests(unittest.TestCase):
    def test_blocks_new_entry(self) -> None:
        cfg = {"mhimain": {"entry_lock": True, "respect_hkex_hours": False}}
        risk = RiskManager({"risk": {"max_position_shares": 1}})
        position = UnitPositionBook(contracts=0)
        strategy = MHImainStrategy.from_config(cfg, trend=TrendMode.BULL)
        gate = OrderGate()

        with patch("robs.cli.mhimain.execute_unit_order") as mock_order:
            _process_signal(
                cfg,
                MagicMock(),
                position,
                strategy,
                risk,
                Signal("mhimain", Action.BUY, "HK.MHImain", "entry", {}),
                "HK.MHImain",
                None,
                20000.0,
                gate,
            )
        mock_order.assert_not_called()

    def test_allows_flat_when_locked(self) -> None:
        cfg = {"mhimain": {"entry_lock": True, "respect_hkex_hours": False}}
        risk = RiskManager({"risk": {"max_position_shares": 1}})
        position = UnitPositionBook(contracts=1)
        strategy = MHImainStrategy.from_config(cfg, trend=TrendMode.BULL)
        strategy.entry_price = 19900.0
        gate = OrderGate()

        with patch("robs.cli.mhimain.execute_unit_order") as mock_order:
            mock_order.return_value = {"ok": False, "status": "rejected", "reason": "test"}
            _process_signal(
                cfg,
                MagicMock(),
                position,
                strategy,
                risk,
                Signal("mhimain", Action.FLAT, "HK.MHImain", "take profit", {}),
                "HK.MHImain",
                None,
                20100.0,
                gate,
            )
        mock_order.assert_called_once()

    def test_blocks_rollover_open_even_with_bypass(self) -> None:
        cfg = {"mhimain": {"entry_lock": True, "respect_hkex_hours": False}}
        risk = RiskManager({"risk": {"max_position_shares": 1}})
        position = UnitPositionBook(contracts=0)
        strategy = MHImainStrategy.from_config(cfg, trend=TrendMode.BULL)
        gate = OrderGate()

        with patch("robs.cli.mhimain.execute_unit_order") as mock_order:
            _process_signal(
                cfg,
                MagicMock(),
                position,
                strategy,
                risk,
                Signal("rollover", Action.BUY, "HK.MHImain", "rollover open", {}),
                "HK.MHImain",
                None,
                20000.0,
                gate,
                bypass_entry_guards=True,
                order_code="HK.MHI2607",
            )
        mock_order.assert_not_called()


if __name__ == "__main__":
    unittest.main()
