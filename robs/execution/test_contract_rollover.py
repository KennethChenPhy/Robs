"""Tests for HK.MHImain contract rollover."""

from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from robs.cli.mhimain import _handle_contract_rollover
from robs.execution.contract_rollover import (
    ContractRolloverManager,
    is_last_trading_day,
    order_code_for_position,
    parse_last_trade_date,
    should_rollover,
)
from robs.execution.order_gate import OrderGate
from robs.execution.position import UnitPositionBook
from robs.execution.risk import RiskManager
from robs.strategy.mhimain import MHImainStrategy
from robs.strategy.trend import TrendMode

HK = ZoneInfo("Asia/Hong_Kong")


class ContractRolloverLogicTests(unittest.TestCase):
    def test_parse_last_trade_date(self) -> None:
        self.assertEqual(parse_last_trade_date("2026-06-27 16:15:00"), datetime(2026, 6, 27).date())

    def test_is_last_trading_day(self) -> None:
        now = datetime(2026, 6, 27, 10, 0, tzinfo=HK)
        self.assertTrue(is_last_trading_day("2026-06-27 16:15:00", now=now))
        self.assertFalse(is_last_trading_day("2026-06-26 16:15:00", now=now))

    def test_should_rollover_on_front_change(self) -> None:
        ok, reason = should_rollover("HK.MHI2606", "HK.MHI2607", None)
        self.assertTrue(ok)
        self.assertEqual(reason, "front_month_changed")

    def test_order_code_for_position(self) -> None:
        self.assertEqual(
            order_code_for_position("HK.MHImain", 1, "HK.MHI2606", "HK.MHI2607"),
            "HK.MHI2606",
        )
        self.assertEqual(
            order_code_for_position("HK.MHImain", 0, None, "HK.MHI2607"),
            "HK.MHI2607",
        )


class ContractRolloverHandlerTests(unittest.TestCase):
    @patch("robs.cli.mhimain._process_signal")
    def test_starts_close_on_front_change(self, mock_process: MagicMock) -> None:
        cfg = {"mhimain": {"contract_rollover": True}}
        rollover = ContractRolloverManager("HK.MHImain", cfg)
        position = UnitPositionBook(contracts=1)
        strategy = MHImainStrategy.from_config(cfg, trend=TrendMode.BULL)
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
            held_contract="HK.MHI2606",
            last_trade_time="2026-07-30 16:15:00",
        )
        self.assertTrue(busy)
        mock_process.assert_called_once()
        self.assertEqual(mock_process.call_args.kwargs.get("order_code"), "HK.MHI2606")
        self.assertEqual(rollover.state.phase, "close")

    @patch("robs.cli.mhimain._process_signal")
    def test_opens_front_after_flat(self, mock_process: MagicMock) -> None:
        cfg = {"mhimain": {"contract_rollover": True}}
        rollover = ContractRolloverManager("HK.MHImain", cfg)
        rollover.state.phase = "open"
        rollover.state.target_contract = "HK.MHI2607"
        rollover.state.direction = -1
        rollover.state.reason = "front_month_changed"
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
        signal = mock_process.call_args.args[5]
        from robs.strategy.rules import Action

        self.assertEqual(signal.action, Action.SELL)
        self.assertEqual(mock_process.call_args.kwargs.get("order_code"), "HK.MHI2607")

    @patch("robs.cli.mhimain._process_signal")
    def test_open_reject_keeps_rollover_busy(self, mock_process: MagicMock) -> None:
        cfg = {"mhimain": {"contract_rollover": True}}
        rollover = ContractRolloverManager("HK.MHImain", cfg)
        rollover.state.phase = "open"
        rollover.state.held_contract = "HK.MHI2606"
        rollover.state.target_contract = "HK.MHI2607"
        rollover.state.direction = 1
        rollover.state.reason = "front_month_changed"
        position = UnitPositionBook(contracts=0)
        strategy = MHImainStrategy.from_config(cfg, trend=TrendMode.BULL)
        risk = RiskManager({"risk": {"max_position_shares": 1}})
        gate = OrderGate()

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
            gate,
            None,
            rollover,
            front_contract="HK.MHI2607",
            held_contract=None,
            last_trade_time=None,
        )
        self.assertTrue(busy)
        self.assertEqual(rollover.state.phase, "open")
        mock_process.assert_called_once()

        gate.pending = True
        busy2 = _handle_contract_rollover(
            cfg,
            MagicMock(),
            MagicMock(),
            position,
            strategy,
            risk,
            "HK.MHImain",
            None,
            20000.0,
            gate,
            None,
            rollover,
            front_contract="HK.MHI2607",
            held_contract=None,
            last_trade_time=None,
        )
        self.assertTrue(busy2)
        mock_process.assert_called_once()

    @patch("robs.cli.mhimain._process_signal")
    def test_aborts_open_when_front_same_as_held(self, mock_process: MagicMock) -> None:
        cfg = {"mhimain": {"contract_rollover": True}}
        rollover = ContractRolloverManager("HK.MHImain", cfg)
        rollover.state.phase = "open"
        rollover.state.held_contract = "HK.MHI2606"
        rollover.state.target_contract = "HK.MHI2606"
        rollover.state.direction = 1
        position = UnitPositionBook(contracts=0)
        strategy = MHImainStrategy.from_config(cfg, trend=TrendMode.BULL)
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
            front_contract="HK.MHI2606",
            held_contract=None,
            last_trade_time=None,
        )
        self.assertTrue(busy)
        self.assertEqual(rollover.state.phase, "idle")
        mock_process.assert_not_called()

    @patch("robs.cli.mhimain._process_signal")
    def test_close_to_open_same_iteration(self, mock_process: MagicMock) -> None:
        cfg = {"mhimain": {"contract_rollover": True}}
        rollover = ContractRolloverManager("HK.MHImain", cfg)
        rollover.state.phase = "close"
        rollover.state.held_contract = "HK.MHI2606"
        rollover.state.target_contract = "HK.MHI2607"
        rollover.state.direction = 1
        rollover.state.reason = "front_month_changed"
        position = UnitPositionBook(contracts=0)
        strategy = MHImainStrategy.from_config(cfg, trend=TrendMode.BULL)
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
        self.assertEqual(rollover.state.phase, "open")
        mock_process.assert_called_once()
        from robs.strategy.rules import Action

        self.assertEqual(mock_process.call_args.args[5].action, Action.BUY)


if __name__ == "__main__":
    unittest.main()
