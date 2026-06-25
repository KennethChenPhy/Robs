"""Tests for multi-leg MHI portfolio quoting and sync."""

from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd

from robs.cli.mhimain import PollDisplayGate, _allows_next_month_close
from robs.execution.contract_rollover import (
    contract_month_key,
    is_held_ahead_of_front,
    is_held_behind_front,
    next_named_mhi_contract,
    quote_symbols_for_portfolio,
    roll_forward_entry_code,
)
from robs.execution.mhi_portfolio import MHIPortfolio, parse_quote_batch
from robs.execution.position import UnitPositionBook
from robs.execution.position_sync import BrokerPosition, fetch_broker_mhi_legs
from robs.strategy.rules import Action
from robs.strategy.trend import TrendMode


class QuoteSymbolsTests(unittest.TestCase):
    def test_flat_quotes_mhimain_only(self) -> None:
        self.assertEqual(quote_symbols_for_portfolio("HK.MHImain", []), ["HK.MHImain"])

    def test_held_quotes_main_and_months(self) -> None:
        syms = quote_symbols_for_portfolio(
            "HK.MHImain",
            ["HK.MHI2607", "HK.MHI2606"],
        )
        self.assertEqual(syms[0], "HK.MHImain")
        self.assertIn("HK.MHI2606", syms)
        self.assertIn("HK.MHI2607", syms)
        self.assertEqual(len(syms), 3)

    def test_held_quotes_sorted_by_month_not_hardcoded(self) -> None:
        syms = quote_symbols_for_portfolio(
            "HK.MHImain",
            ["HK.MHI2608", "HK.MHI2607"],
        )
        self.assertEqual(syms, ["HK.MHImain", "HK.MHI2607", "HK.MHI2608"])

    def test_year_boundary_month_order(self) -> None:
        self.assertTrue(is_held_behind_front("HK.MHI2512", "HK.MHI2601"))
        self.assertFalse(is_held_ahead_of_front("HK.MHI2512", "HK.MHI2601"))


class ContractMonthKeyTests(unittest.TestCase):
    def test_parses_any_yyMM(self) -> None:
        self.assertEqual(contract_month_key("HK.MHI2606"), (2026, 6))
        self.assertEqual(contract_month_key("HK.MHI2607"), (2026, 7))
        self.assertEqual(contract_month_key("HK.MHI2608"), (2026, 8))
        self.assertEqual(contract_month_key("HK.MHI2512"), (2025, 12))
        self.assertEqual(contract_month_key("HK.MHI2601"), (2026, 1))
        self.assertIsNone(contract_month_key("HK.MHImain"))

    def test_next_named_mhi_contract(self) -> None:
        self.assertEqual(next_named_mhi_contract("HK.MHI2606"), "HK.MHI2607")
        self.assertEqual(next_named_mhi_contract("HK.MHI2512"), "HK.MHI2601")


class ParseQuoteBatchTests(unittest.TestCase):
    def test_maps_by_code(self) -> None:
        data = pd.DataFrame(
            [
                {"code": "HK.MHImain", "last_price": 20000.0, "data_time": "2026-06-27 10:00:00"},
                {"code": "HK.MHI2606", "last_price": 19990.0, "data_time": "2026-06-27 10:00:01"},
                {"code": "HK.MHI2607", "last_price": 20010.0, "data_time": "2026-06-27 10:00:02"},
            ]
        )
        prices, rows, times = parse_quote_batch(data, ["HK.MHImain", "HK.MHI2606", "HK.MHI2607"])
        self.assertEqual(prices["HK.MHI2606"], 19990.0)
        self.assertEqual(prices["HK.MHI2607"], 20010.0)
        self.assertEqual(times["HK.MHI2607"], "2026-06-27 10:00:02")
        self.assertIsNotNone(rows["HK.MHI2606"])


class MHIPortfolioTests(unittest.TestCase):
    def test_two_ahead_legs_independent(self) -> None:
        portfolio = MHIPortfolio.create(
            {"mhimain": {}},
            trend=TrendMode.BULL,
            quote_symbol="HK.MHImain",
        )
        portfolio.update_front_context("HK.MHI2606", None)
        leg7 = portfolio.ensure_leg("HK.MHI2607")
        leg8 = portfolio.ensure_leg("HK.MHI2608")
        leg7.position.contracts = 1
        leg8.position.contracts = -1
        self.assertFalse(portfolio.is_flat())
        self.assertEqual(len(portfolio.ahead_of_front_legs()), 2)
        syms = portfolio.quote_symbols()
        self.assertIn("HK.MHI2607", syms)
        self.assertIn("HK.MHI2608", syms)


class PortfolioRefreshMultiLegTests(unittest.TestCase):
    def test_refresh_keeps_legs_independent(self) -> None:
        from robs.execution.risk import RiskManager

        portfolio = MHIPortfolio.create(
            {"mhimain": {}},
            trend=TrendMode.BULL,
            quote_symbol="HK.MHImain",
        )
        portfolio.update_front_context("HK.MHI2606", None)
        risk = RiskManager({"risk": {"max_position_shares": 8}})
        broker_legs = [
            BrokerPosition(
                code="HK.MHI2606",
                contracts=2,
                qty=2,
                entry_price=19900.0,
                current_price=19950.0,
                pnl_points=50.0,
                pnl_val=None,
            ),
            BrokerPosition(
                code="HK.MHI2607",
                contracts=-1,
                qty=1,
                entry_price=20050.0,
                current_price=20010.0,
                pnl_points=40.0,
                pnl_val=None,
            ),
        ]
        portfolio.bootstrap_from_broker(broker_legs, risk, ma5=None, front_contract="HK.MHI2606")
        self.assertEqual(len(portfolio.ahead_of_front_legs()), 1)
        self.assertEqual(portfolio.leg_for_code("HK.MHI2607").position.contracts, -1)
        self.assertEqual(portfolio.entry_position.contracts, 2)

        updated = [
            BrokerPosition(
                code="HK.MHI2606",
                contracts=2,
                qty=2,
                entry_price=19900.0,
                current_price=19980.0,
                pnl_points=80.0,
                pnl_val=None,
            ),
        ]
        portfolio.refresh_from_broker(updated, risk)
        self.assertNotIn("HK.MHI2607", portfolio.legs)
        self.assertEqual(portfolio.entry_position.contracts, 2)


class FlatEntryAfterAheadLegTests(unittest.TestCase):
    def test_reopen_front_after_flatting_ahead_month(self) -> None:
        portfolio = MHIPortfolio.create(
            {"mhimain": {}},
            trend=TrendMode.BULL,
            quote_symbol="HK.MHImain",
        )
        portfolio.update_front_context("HK.MHI2606", "2026-06-29 11:58:00")
        position, strategy, order_code = portfolio.flat_entry_target("HK.MHI2606")
        self.assertEqual(order_code, "HK.MHI2606")
        self.assertIs(portfolio.entry_position, position)
        self.assertIs(portfolio.entry_strategy, strategy)

    def test_absorb_leg_into_entry(self) -> None:
        portfolio = MHIPortfolio.create(
            {"mhimain": {}},
            trend=TrendMode.BULL,
            quote_symbol="HK.MHImain",
        )
        portfolio.update_front_context("HK.MHI2606", None)
        leg = portfolio.ensure_leg("HK.MHI2607")
        leg.position.contracts = 1
        leg.strategy.entry_price = 20000.0
        portfolio.entry_position.contracts = 0
        portfolio.absorb_leg_into_entry("HK.MHI2607")
        self.assertEqual(portfolio.entry_position.contracts, 1)
        self.assertEqual(portfolio.entry_book_code, "HK.MHI2607")
        self.assertIsNone(portfolio.leg_for_code("HK.MHI2607"))
        self.assertEqual(portfolio.entry_strategy.entry_price, 20000.0)

    def test_bootstrap_adopts_front_and_next_month(self) -> None:
        from robs.execution.risk import RiskManager

        portfolio = MHIPortfolio.create(
            {"mhimain": {}},
            trend=TrendMode.BULL,
            quote_symbol="HK.MHImain",
        )
        risk = RiskManager({"risk": {"max_position_shares": 8}})
        portfolio.bootstrap_from_broker(
            [
                BrokerPosition(
                    code="HK.MHI2606",
                    contracts=1,
                    qty=1,
                    entry_price=19900.0,
                    current_price=19950.0,
                    pnl_points=50.0,
                    pnl_val=None,
                ),
                BrokerPosition(
                    code="HK.MHI2607",
                    contracts=1,
                    qty=1,
                    entry_price=20000.0,
                    current_price=20010.0,
                    pnl_points=10.0,
                    pnl_val=None,
                ),
            ],
            risk,
            ma5=None,
            front_contract="HK.MHI2606",
        )
        self.assertEqual(portfolio.entry_position.contracts, 1)
        self.assertEqual(len(portfolio.ahead_of_front_legs()), 1)
        self.assertEqual(portfolio.leg_for_code("HK.MHI2607").position.contracts, 1)
        self.assertIsNone(portfolio.leg_for_code("HK.MHI2606"))

    def test_refresh_adopts_manual_front_open(self) -> None:
        from robs.execution.risk import RiskManager

        portfolio = MHIPortfolio.create(
            {"mhimain": {}},
            trend=TrendMode.BULL,
            quote_symbol="HK.MHImain",
        )
        portfolio.update_front_context("HK.MHI2606", None)
        risk = RiskManager({"risk": {"max_position_shares": 8}})
        portfolio.refresh_from_broker(
            [
                BrokerPosition(
                    code="HK.MHI2606",
                    contracts=2,
                    qty=2,
                    entry_price=19900.0,
                    current_price=19950.0,
                    pnl_points=50.0,
                    pnl_val=None,
                ),
            ],
            risk,
        )
        self.assertEqual(portfolio.entry_position.contracts, 2)

    def test_portfolio_roll_day_entry(self) -> None:
        portfolio = MHIPortfolio.create(
            {"mhimain": {"rollover_on_last_trade_day": True}},
            trend=TrendMode.BULL,
            quote_symbol="HK.MHImain",
        )
        hk = ZoneInfo("Asia/Hong_Kong")
        portfolio.update_front_context("HK.MHI2606", "2026-06-29 11:58:00")
        with patch("robs.execution.contract_rollover.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 6, 29, 10, 0, tzinfo=hk)
            mock_dt.strptime = datetime.strptime
            _, _, order_code = portfolio.flat_entry_target("HK.MHI2606")
            self.assertEqual(order_code, "HK.MHI2606")
            self.assertIsNone(portfolio.entry_watch_code())
        with patch("robs.execution.contract_rollover.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 6, 29, 12, 0, tzinfo=hk)
            mock_dt.strptime = datetime.strptime
            _, _, order_code = portfolio.flat_entry_target("HK.MHI2606")
            self.assertEqual(order_code, "HK.MHI2607")
            self.assertEqual(portfolio.entry_watch_code(), "HK.MHI2607")

    def test_roll_day_broker_row_on_entry_not_leg(self) -> None:
        from robs.execution.risk import RiskManager

        portfolio = MHIPortfolio.create(
            {"mhimain": {"rollover_on_last_trade_day": True}},
            trend=TrendMode.BULL,
            quote_symbol="HK.MHImain",
        )
        hk = ZoneInfo("Asia/Hong_Kong")
        portfolio.update_front_context("HK.MHI2606", "2026-06-29 11:58:00")
        risk = RiskManager({"risk": {"max_position_shares": 8}})
        broker = BrokerPosition(
            code="HK.MHI2607",
            contracts=1,
            qty=1,
            entry_price=20000.0,
            current_price=20010.0,
            pnl_points=10.0,
            pnl_val=None,
        )
        with patch("robs.execution.contract_rollover.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 6, 29, 12, 0, tzinfo=hk)
            mock_dt.strptime = datetime.strptime
            portfolio.bootstrap_from_broker([broker], risk, ma5=None, front_contract="HK.MHI2606")
            self.assertEqual(portfolio.entry_position.contracts, 1)
            self.assertIsNone(portfolio.leg_for_code("HK.MHI2607"))
            portfolio.refresh_from_broker([broker], risk)
            self.assertIsNone(portfolio.leg_for_code("HK.MHI2607"))

    def test_manual_next_month_tracks_as_leg(self) -> None:
        portfolio = MHIPortfolio.create(
            {"mhimain": {}},
            trend=TrendMode.BULL,
            quote_symbol="HK.MHImain",
        )
        portfolio.update_front_context("HK.MHI2606", "2026-07-30 11:58:00")
        self.assertTrue(portfolio.tracks_as_next_month_leg("HK.MHI2607"))
        self.assertFalse(portfolio.tracks_on_entry_book("HK.MHI2607"))

    def test_roll_day_entry_persists_after_roll_day(self) -> None:
        from robs.execution.risk import RiskManager

        portfolio = MHIPortfolio.create(
            {"mhimain": {"rollover_on_last_trade_day": True}},
            trend=TrendMode.BULL,
            quote_symbol="HK.MHImain",
        )
        hk = ZoneInfo("Asia/Hong_Kong")
        portfolio.update_front_context("HK.MHI2606", "2026-06-29 11:58:00")
        risk = RiskManager({"risk": {"max_position_shares": 8}})
        broker = BrokerPosition(
            code="HK.MHI2607",
            contracts=1,
            qty=1,
            entry_price=20000.0,
            current_price=20010.0,
            pnl_points=10.0,
            pnl_val=None,
        )
        with patch("robs.execution.contract_rollover.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 6, 29, 12, 0, tzinfo=hk)
            mock_dt.strptime = datetime.strptime
            portfolio.bootstrap_from_broker([broker], risk, ma5=None, front_contract="HK.MHI2606")
        self.assertEqual(portfolio.entry_book_code, "HK.MHI2607")
        with patch("robs.execution.contract_rollover.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 6, 30, 10, 0, tzinfo=hk)
            mock_dt.strptime = datetime.strptime
            self.assertEqual(portfolio.managed_entry_code(), "HK.MHI2607")
            self.assertTrue(portfolio.tracks_on_entry_book("HK.MHI2607"))
            self.assertFalse(portfolio.tracks_as_next_month_leg("HK.MHI2607"))
            portfolio.refresh_from_broker([broker], risk)
            self.assertEqual(portfolio.entry_position.contracts, 1)
            self.assertIsNone(portfolio.leg_for_code("HK.MHI2607"))
            self.assertIn("HK.MHI2607", portfolio.quote_symbols())


class PollDisplayGateTests(unittest.TestCase):
    def test_note_price_tolerates_none(self) -> None:
        gate = PollDisplayGate(threshold_pts=10.0)
        gate.ref_price = 20000.0
        show, diff = gate.note_price(None)
        self.assertFalse(show)
        self.assertEqual(diff, 0.0)
        self.assertEqual(gate.ref_price, 20000.0)


class RollDayFrontMonthSyncTests(unittest.TestCase):
    def test_refresh_syncs_expiring_month_when_entry_flat_on_roll_day(self) -> None:
        from robs.execution.risk import RiskManager

        portfolio = MHIPortfolio.create(
            {"mhimain": {"rollover_on_last_trade_day": True}},
            trend=TrendMode.BULL,
            quote_symbol="HK.MHImain",
        )
        hk = ZoneInfo("Asia/Hong_Kong")
        portfolio.update_front_context("HK.MHI2606", "2026-06-29 11:58:00")
        risk = RiskManager({"risk": {"max_position_shares": 8}})
        broker = BrokerPosition(
            code="HK.MHI2606",
            contracts=1,
            qty=1,
            entry_price=19900.0,
            current_price=19950.0,
            pnl_points=50.0,
            pnl_val=None,
        )
        with patch("robs.execution.contract_rollover.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 6, 29, 12, 0, tzinfo=hk)
            mock_dt.strptime = datetime.strptime
            self.assertFalse(portfolio.tracks_on_entry_book("HK.MHI2606"))
            portfolio.refresh_from_broker([broker], risk)
        self.assertEqual(portfolio.entry_position.contracts, 1)
        self.assertEqual(portfolio.entry_book_code, "HK.MHI2606")
        self.assertIsNone(portfolio.leg_for_code("HK.MHI2606"))


class EntryBookRolloverTests(unittest.TestCase):
    @patch("robs.cli.mhimain._process_signal")
    def test_entry_last_day_rolls_while_still_front(self, mock_process: MagicMock) -> None:
        from unittest.mock import MagicMock
        from zoneinfo import ZoneInfo

        from robs.cli.mhimain import _handle_portfolio_rollovers
        from robs.execution.order_gate import OrderGate
        from robs.execution.risk import RiskManager

        hk = ZoneInfo("Asia/Hong_Kong")
        now = datetime(2026, 6, 29, 12, 0, tzinfo=hk)
        portfolio = MHIPortfolio.create(
            {"mhimain": {"contract_rollover": True}},
            trend=TrendMode.BULL,
            quote_symbol="HK.MHImain",
        )
        portfolio.update_front_context("HK.MHI2606", "2026-06-29 11:58:00")
        portfolio.entry_position.contracts = 1
        portfolio.entry_book_code = "HK.MHI2606"
        risk = RiskManager({"risk": {"max_position_shares": 8}})
        quote = MagicMock()
        quote.contract_last_trade_time.return_value = "2026-06-29 11:58:00"

        with patch("robs.cli.mhimain.datetime") as mock_dt:
            mock_dt.now.return_value = now
            mock_dt.strptime = datetime.strptime
            entry_skip, leg_skip = _handle_portfolio_rollovers(
                {"mhimain": {"contract_rollover": True}},
                MagicMock(),
                quote,
                portfolio,
                risk,
                "HK.MHImain",
                {},
                {"HK.MHImain": 20000.0, "HK.MHI2606": 20000.0},
                OrderGate(),
                None,
                front_contract="HK.MHI2606",
            )
        self.assertTrue(entry_skip)
        self.assertFalse(leg_skip)
        mock_process.assert_called_once()
        self.assertEqual(mock_process.call_args.kwargs.get("order_code"), "HK.MHI2606")
        self.assertEqual(portfolio.entry_rollover.state.target_contract, "HK.MHI2607")

    @patch("robs.cli.mhimain._process_signal")
    def test_entry_no_roll_before_ltd_1158(self, mock_process: MagicMock) -> None:
        from unittest.mock import MagicMock
        from zoneinfo import ZoneInfo

        from robs.cli.mhimain import _handle_portfolio_rollovers
        from robs.execution.order_gate import OrderGate
        from robs.execution.risk import RiskManager

        hk = ZoneInfo("Asia/Hong_Kong")
        now = datetime(2026, 6, 29, 10, 0, tzinfo=hk)
        portfolio = MHIPortfolio.create(
            {"mhimain": {"contract_rollover": True}},
            trend=TrendMode.BULL,
            quote_symbol="HK.MHImain",
        )
        portfolio.update_front_context("HK.MHI2606", "2026-06-29 11:58:00")
        portfolio.entry_position.contracts = 1
        portfolio.entry_book_code = "HK.MHI2606"
        risk = RiskManager({"risk": {"max_position_shares": 8}})
        quote = MagicMock()
        quote.contract_last_trade_time.return_value = "2026-06-29 11:58:00"

        with patch("robs.cli.mhimain.datetime") as mock_dt:
            mock_dt.now.return_value = now
            mock_dt.strptime = datetime.strptime
            entry_skip, leg_skip = _handle_portfolio_rollovers(
                {"mhimain": {"contract_rollover": True}},
                MagicMock(),
                quote,
                portfolio,
                risk,
                "HK.MHImain",
                {},
                {"HK.MHImain": 20000.0, "HK.MHI2606": 20000.0},
                OrderGate(),
                None,
                front_contract="HK.MHI2606",
            )
        self.assertFalse(entry_skip)
        self.assertFalse(leg_skip)
        mock_process.assert_not_called()

    @patch("robs.cli.mhimain._process_signal")
    def test_entry_rollover_skips_open_when_ahead_leg_already_held(self, mock_process: MagicMock) -> None:
        from unittest.mock import MagicMock
        from zoneinfo import ZoneInfo

        from robs.cli.mhimain import _handle_portfolio_rollovers
        from robs.execution.order_gate import OrderGate
        from robs.execution.position_sync import BrokerPosition
        from robs.execution.risk import RiskManager

        hk = ZoneInfo("Asia/Hong_Kong")
        now = datetime(2026, 6, 29, 12, 0, tzinfo=hk)
        portfolio = MHIPortfolio.create(
            {"mhimain": {"contract_rollover": True}},
            trend=TrendMode.BULL,
            quote_symbol="HK.MHImain",
        )
        portfolio.update_front_context("HK.MHI2606", "2026-06-29 11:58:00")
        portfolio.bootstrap_from_broker(
            [
                BrokerPosition(
                    code="HK.MHI2606",
                    contracts=1,
                    qty=1,
                    entry_price=19900.0,
                    current_price=19950.0,
                    pnl_points=50.0,
                    pnl_val=None,
                ),
                BrokerPosition(
                    code="HK.MHI2607",
                    contracts=1,
                    qty=1,
                    entry_price=20000.0,
                    current_price=20010.0,
                    pnl_points=10.0,
                    pnl_val=None,
                ),
            ],
            RiskManager({"risk": {"max_position_shares": 8}}),
            ma5=None,
            front_contract="HK.MHI2606",
        )
        risk = RiskManager({"risk": {"max_position_shares": 8}})
        quote = MagicMock()
        quote.contract_last_trade_time.return_value = "2026-06-29 11:58:00"

        with patch("robs.cli.mhimain.datetime") as mock_dt:
            mock_dt.now.return_value = now
            mock_dt.strptime = datetime.strptime
            entry_skip, leg_skip = _handle_portfolio_rollovers(
                {"mhimain": {"contract_rollover": True}},
                MagicMock(),
                quote,
                portfolio,
                risk,
                "HK.MHImain",
                {},
                {"HK.MHImain": 20000.0, "HK.MHI2606": 20000.0, "HK.MHI2607": 20010.0},
                OrderGate(),
                None,
                front_contract="HK.MHI2606",
            )
        self.assertTrue(entry_skip)
        self.assertFalse(leg_skip)
        mock_process.assert_called_once()
        self.assertEqual(mock_process.call_args.kwargs.get("order_code"), "HK.MHI2606")
        self.assertEqual(portfolio.entry_rollover.state.phase, "close")

        portfolio.entry_position.contracts = 0
        portfolio.entry_rollover.state.phase = "open"
        with patch("robs.cli.mhimain.datetime") as mock_dt:
            mock_dt.now.return_value = now
            mock_dt.strptime = datetime.strptime
            entry_skip, leg_skip = _handle_portfolio_rollovers(
                {"mhimain": {"contract_rollover": True}},
                MagicMock(),
                quote,
                portfolio,
                risk,
                "HK.MHImain",
                {},
                {"HK.MHImain": 20000.0, "HK.MHI2607": 20010.0},
                OrderGate(),
                None,
                front_contract="HK.MHI2606",
            )
        self.assertTrue(entry_skip)
        self.assertEqual(mock_process.call_count, 1)
        self.assertEqual(portfolio.entry_rollover.state.phase, "idle")
        self.assertEqual(portfolio.entry_position.contracts, 1)
        self.assertEqual(portfolio.entry_book_code, "HK.MHI2607")
        self.assertIsNone(portfolio.leg_for_code("HK.MHI2607"))
        self.assertEqual(len(portfolio.ahead_of_front_legs()), 0)

    @patch("robs.cli.mhimain._process_signal")
    def test_entry_rollover_adopts_opposite_ahead_leg(self, mock_process: MagicMock) -> None:
        from unittest.mock import MagicMock
        from zoneinfo import ZoneInfo

        from robs.cli.mhimain import _handle_portfolio_rollovers
        from robs.execution.order_gate import OrderGate
        from robs.execution.position_sync import BrokerPosition
        from robs.execution.risk import RiskManager

        hk = ZoneInfo("Asia/Hong_Kong")
        now = datetime(2026, 6, 29, 12, 0, tzinfo=hk)
        portfolio = MHIPortfolio.create(
            {"mhimain": {"contract_rollover": True}},
            trend=TrendMode.BULL,
            quote_symbol="HK.MHImain",
        )
        portfolio.update_front_context("HK.MHI2606", "2026-06-29 11:58:00")
        portfolio.bootstrap_from_broker(
            [
                BrokerPosition(
                    code="HK.MHI2606",
                    contracts=1,
                    qty=1,
                    entry_price=19900.0,
                    current_price=19950.0,
                    pnl_points=50.0,
                    pnl_val=None,
                ),
                BrokerPosition(
                    code="HK.MHI2607",
                    contracts=-1,
                    qty=-1,
                    entry_price=20000.0,
                    current_price=20010.0,
                    pnl_points=-10.0,
                    pnl_val=None,
                ),
            ],
            RiskManager({"risk": {"max_position_shares": 8}}),
            ma5=None,
            front_contract="HK.MHI2606",
        )
        risk = RiskManager({"risk": {"max_position_shares": 8}})
        quote = MagicMock()
        quote.contract_last_trade_time.return_value = "2026-06-29 11:58:00"

        with patch("robs.cli.mhimain.datetime") as mock_dt:
            mock_dt.now.return_value = now
            mock_dt.strptime = datetime.strptime
            _handle_portfolio_rollovers(
                {"mhimain": {"contract_rollover": True}},
                MagicMock(),
                quote,
                portfolio,
                risk,
                "HK.MHImain",
                {},
                {"HK.MHImain": 20000.0, "HK.MHI2606": 20000.0, "HK.MHI2607": 20010.0},
                OrderGate(),
                None,
                front_contract="HK.MHI2606",
            )
        mock_process.assert_called_once()
        self.assertEqual(mock_process.call_args.kwargs.get("order_code"), "HK.MHI2606")
        self.assertEqual(portfolio.entry_rollover.state.phase, "close")

        portfolio.entry_position.contracts = 0
        portfolio.entry_rollover.state.phase = "open"
        with patch("robs.cli.mhimain.datetime") as mock_dt:
            mock_dt.now.return_value = now
            mock_dt.strptime = datetime.strptime
            entry_skip, leg_skip = _handle_portfolio_rollovers(
                {"mhimain": {"contract_rollover": True}},
                MagicMock(),
                quote,
                portfolio,
                risk,
                "HK.MHImain",
                {},
                {"HK.MHImain": 20000.0, "HK.MHI2607": 20010.0},
                OrderGate(),
                None,
                front_contract="HK.MHI2606",
            )
        self.assertTrue(entry_skip)
        self.assertEqual(mock_process.call_count, 1)
        self.assertEqual(portfolio.entry_rollover.state.phase, "idle")
        self.assertEqual(portfolio.entry_position.contracts, -1)
        self.assertEqual(portfolio.entry_book_code, "HK.MHI2607")
        self.assertIsNone(portfolio.leg_for_code("HK.MHI2607"))
        self.assertEqual(len(portfolio.ahead_of_front_legs()), 0)

    @patch("robs.cli.mhimain._process_signal")
    def test_entry_waits_until_last_day(self, mock_process: MagicMock) -> None:
        from unittest.mock import MagicMock
        from zoneinfo import ZoneInfo

        from robs.cli.mhimain import _handle_portfolio_rollovers
        from robs.execution.order_gate import OrderGate
        from robs.execution.risk import RiskManager

        hk = ZoneInfo("Asia/Hong_Kong")
        now = datetime(2026, 6, 28, 10, 0, tzinfo=hk)
        portfolio = MHIPortfolio.create(
            {"mhimain": {"contract_rollover": True}},
            trend=TrendMode.BULL,
            quote_symbol="HK.MHImain",
        )
        portfolio.update_front_context("HK.MHI2606", "2026-06-29 11:58:00")
        portfolio.entry_position.contracts = 1
        portfolio.entry_book_code = "HK.MHI2606"
        risk = RiskManager({"risk": {"max_position_shares": 8}})
        quote = MagicMock()
        quote.contract_last_trade_time.return_value = "2026-06-29 11:58:00"

        with patch("robs.cli.mhimain.datetime") as mock_dt:
            mock_dt.now.return_value = now
            mock_dt.strptime = datetime.strptime
            entry_skip, leg_skip = _handle_portfolio_rollovers(
                {"mhimain": {"contract_rollover": True}},
                MagicMock(),
                quote,
                portfolio,
                risk,
                "HK.MHImain",
                {},
                {"HK.MHImain": 20000.0, "HK.MHI2606": 20000.0},
                OrderGate(),
                None,
                front_contract="HK.MHI2606",
            )
        self.assertFalse(entry_skip)
        self.assertFalse(leg_skip)
        mock_process.assert_not_called()

    @patch("robs.cli.mhimain._process_signal")
    def test_catch_up_rollover_when_entry_behind_front(self, mock_process: MagicMock) -> None:
        from unittest.mock import MagicMock

        from robs.cli.mhimain import _handle_portfolio_rollovers
        from robs.execution.order_gate import OrderGate
        from robs.execution.risk import RiskManager

        portfolio = MHIPortfolio.create(
            {"mhimain": {"contract_rollover": True}},
            trend=TrendMode.BULL,
            quote_symbol="HK.MHImain",
        )
        portfolio.update_front_context("HK.MHI2607", "2026-07-30 11:58:00")
        portfolio.entry_position.contracts = 1
        portfolio.entry_book_code = "HK.MHI2606"
        risk = RiskManager({"risk": {"max_position_shares": 8}})
        quote = MagicMock()
        quote.contract_last_trade_time.return_value = "2026-06-29 16:00:00"

        hk = ZoneInfo("Asia/Hong_Kong")
        now = datetime(2026, 6, 29, 18, 0, tzinfo=hk)
        with patch("robs.cli.mhimain.datetime") as mock_dt:
            mock_dt.now.return_value = now
            mock_dt.strptime = datetime.strptime
            entry_skip, leg_skip = _handle_portfolio_rollovers(
                {"mhimain": {"contract_rollover": True}},
                MagicMock(),
                quote,
                portfolio,
                risk,
                "HK.MHImain",
                {},
                {"HK.MHImain": 20000.0, "HK.MHI2606": 20000.0},
                OrderGate(),
                None,
                front_contract="HK.MHI2607",
            )
        self.assertTrue(entry_skip)
        self.assertFalse(leg_skip)
        mock_process.assert_called_once()
        self.assertEqual(portfolio.entry_rollover.state.phase, "close")
        self.assertEqual(portfolio.entry_rollover.state.target_contract, "HK.MHI2607")

    @patch("robs.cli.mhimain._process_signal")
    def test_roll_day_ahead_entry_skips_rollover(self, mock_process: MagicMock) -> None:
        from unittest.mock import MagicMock

        from robs.cli.mhimain import _handle_portfolio_rollovers
        from robs.execution.order_gate import OrderGate
        from robs.execution.risk import RiskManager

        portfolio = MHIPortfolio.create(
            {"mhimain": {"contract_rollover": True, "rollover_on_last_trade_day": True}},
            trend=TrendMode.BULL,
            quote_symbol="HK.MHImain",
        )
        portfolio.update_front_context("HK.MHI2606", "2026-06-29 11:58:00")
        portfolio.entry_position.contracts = 1
        portfolio.entry_book_code = "HK.MHI2607"
        risk = RiskManager({"risk": {"max_position_shares": 8}})
        quote = MagicMock()
        quote.contract_last_trade_time.return_value = "2026-06-29 11:58:00"

        entry_skip, leg_skip = _handle_portfolio_rollovers(
            {"mhimain": {"contract_rollover": True}},
            MagicMock(),
            quote,
            portfolio,
            risk,
            "HK.MHImain",
            {},
            {"HK.MHImain": 20000.0, "HK.MHI2607": 20010.0},
            OrderGate(),
            None,
            front_contract="HK.MHI2606",
        )
        self.assertFalse(entry_skip)
        self.assertFalse(leg_skip)
        mock_process.assert_not_called()
        self.assertEqual(portfolio.entry_rollover.state.phase, "idle")

    def test_on_opened_updates_entry_book_code(self) -> None:
        from unittest.mock import MagicMock

        from robs.cli.mhimain import _handle_contract_rollover
        from robs.execution.order_gate import OrderGate
        from robs.execution.risk import RiskManager

        portfolio = MHIPortfolio.create(
            {"mhimain": {"contract_rollover": True}},
            trend=TrendMode.BULL,
            quote_symbol="HK.MHImain",
        )
        portfolio.update_front_context("HK.MHI2607", None)
        portfolio.entry_book_code = "HK.MHI2606"
        rollover = portfolio.entry_rollover
        assert rollover is not None
        rollover.state.phase = "open"
        rollover.state.target_contract = "HK.MHI2607"
        rollover.state.direction = 1
        rollover.state.reason = "front_month_changed"
        portfolio.entry_position.contracts = 1
        risk = RiskManager({"risk": {"max_position_shares": 8}})

        _handle_contract_rollover(
            {"mhimain": {"contract_rollover": True}},
            MagicMock(),
            MagicMock(),
            portfolio.entry_position,
            portfolio.entry_strategy,
            risk,
            "HK.MHImain",
            None,
            20050.0,
            OrderGate(),
            None,
            rollover,
            front_contract="HK.MHI2607",
            held_contract="HK.MHI2606",
            last_trade_time=None,
            on_opened=portfolio._note_entry_book_code,
        )
        self.assertEqual(portfolio.entry_book_code, "HK.MHI2607")
        self.assertEqual(rollover.state.phase, "idle")

    def test_refresh_syncs_target_month_during_rollover_open(self) -> None:
        from robs.execution.risk import RiskManager

        portfolio = MHIPortfolio.create(
            {"mhimain": {"contract_rollover": True}},
            trend=TrendMode.BULL,
            quote_symbol="HK.MHImain",
        )
        portfolio.update_front_context("HK.MHI2606", "2026-06-29 11:58:00")
        portfolio.entry_book_code = "HK.MHI2606"
        portfolio.entry_position.contracts = 1
        rollover = portfolio.entry_rollover
        assert rollover is not None
        rollover.state.phase = "open"
        rollover.state.target_contract = "HK.MHI2607"
        rollover.state.held_contract = "HK.MHI2606"
        risk = RiskManager({"risk": {"max_position_shares": 8}})
        broker_legs = [
            BrokerPosition(
                code="HK.MHI2607",
                contracts=1,
                qty=1,
                entry_price=20000.0,
                current_price=20050.0,
                pnl_points=50.0,
                pnl_val=None,
            ),
        ]
        changed = portfolio.refresh_from_broker(broker_legs, risk)
        self.assertEqual(portfolio.entry_book_code, "HK.MHI2607")
        self.assertEqual(portfolio.entry_position.contracts, 1)
        self.assertNotIn("HK.MHI2607", portfolio.legs)
        self.assertEqual(changed, [])


class NextMonthCloseOnlyTests(unittest.TestCase):
    def test_allows_close_not_open(self) -> None:
        long_pos = UnitPositionBook(contracts=1)
        short_pos = UnitPositionBook(contracts=-1)
        flat_pos = UnitPositionBook(contracts=0)
        self.assertTrue(_allows_next_month_close(long_pos, Action.SELL))
        self.assertTrue(_allows_next_month_close(long_pos, Action.FLAT))
        self.assertFalse(_allows_next_month_close(long_pos, Action.BUY))
        self.assertTrue(_allows_next_month_close(short_pos, Action.BUY))
        self.assertFalse(_allows_next_month_close(flat_pos, Action.BUY))
        self.assertFalse(_allows_next_month_close(flat_pos, Action.SELL))


class FetchMhiLegsTests(unittest.TestCase):
    def test_returns_all_rows(self) -> None:
        from unittest.mock import MagicMock

        trade = MagicMock()
        trade._ctx.position_list_query.side_effect = [
            (0, pd.DataFrame()),
            (
                0,
                pd.DataFrame(
                    [
                        {
                            "code": "HK.MHI2606",
                            "qty": 1,
                            "position_side": "LONG",
                            "cost_price": 19900.0,
                        },
                        {
                            "code": "HK.MHI2607",
                            "qty": 1,
                            "position_side": "LONG",
                            "cost_price": 20000.0,
                        },
                    ]
                ),
            ),
        ]
        legs = fetch_broker_mhi_legs(
            trade,
            "HK.MHImain",
            {"trd_env": "SIMULATE"},
            quote_prices={"HK.MHI2606": 19950.0, "HK.MHI2607": 20050.0},
        )
        self.assertEqual(len(legs), 2)
        codes = {leg.code for leg in legs}
        self.assertEqual(codes, {"HK.MHI2606", "HK.MHI2607"})

    def test_ignores_non_mhi_positions(self) -> None:
        from unittest.mock import MagicMock

        trade = MagicMock()
        trade._ctx.position_list_query.side_effect = [
            (0, pd.DataFrame()),
            (
                0,
                pd.DataFrame(
                    [
                        {
                            "code": "HK.HTI2606",
                            "qty": 2,
                            "position_side": "LONG",
                            "cost_price": 4500.0,
                        },
                        {
                            "code": "HK.MHI2606",
                            "qty": 1,
                            "position_side": "LONG",
                            "cost_price": 19900.0,
                        },
                    ]
                ),
            ),
        ]
        legs = fetch_broker_mhi_legs(trade, "HK.MHImain", {"trd_env": "SIMULATE"})
        self.assertEqual(len(legs), 1)
        self.assertEqual(legs[0].code, "HK.MHI2606")

    def test_no_mhi_returns_empty_not_hti(self) -> None:
        from unittest.mock import MagicMock

        trade = MagicMock()
        trade._ctx.position_list_query.side_effect = [
            (0, pd.DataFrame()),
            (
                0,
                pd.DataFrame(
                    [
                        {
                            "code": "HK.HTI2606",
                            "qty": 1,
                            "position_side": "LONG",
                            "cost_price": 4500.0,
                        },
                    ]
                ),
            ),
        ]
        legs = fetch_broker_mhi_legs(trade, "HK.MHImain", {"trd_env": "SIMULATE"})
        self.assertEqual(legs, [])


class NonMhiProductCodeTests(unittest.TestCase):
    def test_is_hk_mhi_product_code(self) -> None:
        from robs.execution.contract_rollover import is_hk_mhi_product_code

        self.assertTrue(is_hk_mhi_product_code("HK.MHImain"))
        self.assertTrue(is_hk_mhi_product_code("HK.MHI2606"))
        self.assertFalse(is_hk_mhi_product_code("HK.HTImain"))
        self.assertFalse(is_hk_mhi_product_code("HK.HTI2606"))
        self.assertFalse(is_hk_mhi_product_code("HK.HSI2606"))


if __name__ == "__main__":
    unittest.main()
