"""Unit tests for broker position sync and re-entry cooldown."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pandas as pd

from robs.execution.cut_loss import PositionPnLBaseline
from robs.execution.position import UnitPositionBook
from robs.execution.position_sync import (
    BrokerPosition,
    apply_broker_position,
    fetch_broker_mhi_legs,
    fetch_broker_position,
    fetch_broker_position_for_code,
    refresh_broker_position,
    _broker_position_from_row,
    _pick_cost,
    _signed_contracts_from_row,
)
from robs.strategy.mhimain import MHImainStrategy


class _Risk:
    position_shares: int = 0


def _strategy(**kwargs) -> MHImainStrategy:
    bl = PositionPnLBaseline(
        cut_loss_pts=200,
        take_profit_pts=400,
        cut_loss_min_hold_hours=24,
        reentry_move_pts=300,
        reentry_trading_hours=6,
    )
    return MHImainStrategy(pnl_baseline=bl, **kwargs)


def _broker(contracts: int, entry: float = 23500.0, price: float = 23550.0) -> BrokerPosition:
    pnl = (price - entry) if contracts > 0 else (entry - price) if contracts < 0 else 0.0
    return BrokerPosition(
        code="HK.MHImain",
        contracts=contracts,
        qty=abs(contracts),
        entry_price=entry if contracts else None,
        current_price=price,
        pnl_points=pnl,
        pnl_val=None,
    )


class PositionSyncTests(unittest.TestCase):
    def test_manual_open_starts_min_hold(self) -> None:
        pos = UnitPositionBook()
        strat = _strategy()
        risk = _Risk()

        changed = refresh_broker_position(_broker(1), pos, strat, risk)
        self.assertTrue(changed)
        self.assertEqual(pos.contracts, 1)
        self.assertIsNotNone(strat.position_opened_at)
        self.assertFalse(strat._entry_armed)
        blocked, _ = strat.pnl_baseline.blocks_cut_loss_for_hold(strat.position_opened_at)
        self.assertTrue(blocked)

    def test_manual_close_triggers_cooldown(self) -> None:
        pos = UnitPositionBook(contracts=1)
        strat = _strategy(entry_price=23500.0)
        strat.position_opened_at = strat.position_opened_at  # type: ignore
        risk = _Risk()
        risk.position_shares = 1

        changed = refresh_broker_position(_broker(0, price=23400.0), pos, strat, risk)
        self.assertTrue(changed)
        self.assertEqual(pos.contracts, 0)
        self.assertTrue(strat.cooldown.locked)
        self.assertAlmostEqual(strat.cooldown.cooldown_ref_price or 0, 23400.0)

    def test_manual_close_without_quote_still_cooldown(self) -> None:
        pos = UnitPositionBook(contracts=1)
        strat = _strategy(entry_price=23500.0)
        risk = _Risk()

        broker = BrokerPosition(
            code="HK.MHImain",
            contracts=0,
            qty=0,
            entry_price=None,
            current_price=None,
            pnl_points=0.0,
            pnl_val=None,
        )
        refresh_broker_position(broker, pos, strat, risk)
        self.assertTrue(strat.cooldown.locked)
        self.assertEqual(strat.cooldown.cooldown_ref_price, 23500.0)

    def test_flip_position_close_then_open(self) -> None:
        pos = UnitPositionBook(contracts=1)
        strat = _strategy(entry_price=23500.0)
        risk = _Risk()

        changed = refresh_broker_position(_broker(-1, entry=23600.0, price=23650.0), pos, strat, risk)
        self.assertTrue(changed)
        self.assertEqual(pos.contracts, -1)
        self.assertTrue(strat.cooldown.locked)
        self.assertIsNotNone(strat.position_opened_at)

    def test_apply_only_does_not_trigger_manual_hooks(self) -> None:
        pos = UnitPositionBook()
        strat = _strategy()
        apply_broker_position(_broker(1), pos, strat, bootstrap=False)
        self.assertIsNone(strat.position_opened_at)


class ReentryCooldownTests(unittest.TestCase):
    def test_move_does_not_clear_before_minimum_hours(self) -> None:
        bl = PositionPnLBaseline(
            reentry_move_pts=300,
            reentry_minimum_hours=4,
            reentry_trading_hours=24,
        )
        bl.record_exit_cooldown(23500.0)
        bl.cooldown_started_at = datetime.now(timezone.utc) - timedelta(hours=1)

        blocked, reason = bl.blocks_entry(23900.0)
        self.assertTrue(blocked)
        self.assertIn("min wait", reason)

    def test_move_clears_after_minimum_hours(self) -> None:
        bl = PositionPnLBaseline(
            reentry_move_pts=300,
            reentry_minimum_hours=4,
            reentry_trading_hours=24,
        )
        bl.record_exit_cooldown(23500.0)
        bl.cooldown_started_at = datetime.now(timezone.utc) - timedelta(hours=5)

        blocked, reason = bl.blocks_entry(23900.0)
        self.assertFalse(blocked)
        self.assertIn("cleared", reason)


class NonMhiBrokerFetchTests(unittest.TestCase):
    def test_fetch_broker_position_for_code_rejects_hti(self) -> None:
        trade = MagicMock()
        broker = fetch_broker_position_for_code(trade, "HK.HTI2606", {"trd_env": "SIMULATE"})
        self.assertEqual(broker.contracts, 0)
        trade._ctx.position_list_query.assert_not_called()


class MultiContractPositionFetchTests(unittest.TestCase):
    def _multi_leg_df(self) -> pd.DataFrame:
        return pd.DataFrame(
            [
                {
                    "code": "HK.MHI2606",
                    "qty": 2,
                    "position_side": "LONG",
                    "cost_price": 19900.0,
                },
                {
                    "code": "HK.MHI2607",
                    "qty": 1,
                    "position_side": "SHORT",
                    "cost_price": 20050.0,
                },
            ]
        )

    def test_fetch_for_code_picks_matching_row_not_first(self) -> None:
        trade = MagicMock()
        trade._ctx.position_list_query.return_value = (0, self._multi_leg_df())
        broker = fetch_broker_position_for_code(
            trade, "HK.MHI2607", {"trd_env": "SIMULATE"}, quote_price=20010.0
        )
        self.assertEqual(broker.code, "HK.MHI2607")
        self.assertEqual(broker.contracts, -1)
        self.assertEqual(broker.entry_price, 20050.0)

    def test_fetch_for_code_falls_back_to_account_query(self) -> None:
        trade = MagicMock()
        trade._ctx.position_list_query.side_effect = [
            (0, pd.DataFrame()),
            (0, self._multi_leg_df()),
        ]
        broker = fetch_broker_position_for_code(trade, "HK.MHI2606", {"trd_env": "SIMULATE"})
        self.assertEqual(broker.code, "HK.MHI2606")
        self.assertEqual(broker.contracts, 2)

    def test_fetch_broker_position_named_month_exact(self) -> None:
        trade = MagicMock()
        trade._ctx.position_list_query.return_value = (0, self._multi_leg_df())
        broker = fetch_broker_position(trade, "HK.MHI2607", {"trd_env": "SIMULATE"})
        self.assertEqual(broker.contracts, -1)

    def test_fetch_mhi_legs_preserves_per_contract_qty(self) -> None:
        trade = MagicMock()
        trade._ctx.position_list_query.side_effect = [
            (0, pd.DataFrame()),
            (0, self._multi_leg_df()),
        ]
        legs = fetch_broker_mhi_legs(
            trade,
            "HK.MHImain",
            {"trd_env": "SIMULATE"},
            quote_prices={"HK.MHI2606": 19950.0, "HK.MHI2607": 20010.0},
        )
        by_code = {leg.code: leg for leg in legs}
        self.assertEqual(by_code["HK.MHI2606"].contracts, 2)
        self.assertEqual(by_code["HK.MHI2607"].contracts, -1)
        self.assertEqual(by_code["HK.MHI2606"].current_price, 19950.0)
        self.assertEqual(by_code["HK.MHI2607"].current_price, 20010.0)

    def test_fetch_mhi_legs_requeries_duplicate_rows_for_same_code(self) -> None:
        dup_df = pd.DataFrame(
            [
                {
                    "code": "HK.MHI2606",
                    "qty": 1,
                    "position_side": "LONG",
                    "cost_price": 23031.0,
                },
                {
                    "code": "HK.MHI2606",
                    "qty": 1,
                    "position_side": "LONG",
                    "cost_price": 23031.0,
                },
            ]
        )
        trade = MagicMock()
        trade._ctx.position_list_query.side_effect = [
            (0, pd.DataFrame()),
            (0, dup_df),
            (0, pd.DataFrame(
                [{
                    "code": "HK.MHI2606",
                    "qty": 1,
                    "position_side": "LONG",
                    "cost_price": 23031.0,
                }]
            )),
        ]
        legs = fetch_broker_mhi_legs(
            trade,
            "HK.MHImain",
            {"trd_env": "SIMULATE"},
            quote_prices={"HK.MHI2606": 23031.0},
        )
        self.assertEqual(len(legs), 1)
        self.assertEqual(legs[0].contracts, 1)

    def test_fetch_mhi_legs_does_not_substitute_mhimain_for_missing_month(self) -> None:
        trade = MagicMock()
        trade._ctx.position_list_query.side_effect = [
            (0, pd.DataFrame()),
            (0, self._multi_leg_df()),
        ]
        legs = fetch_broker_mhi_legs(
            trade,
            "HK.MHImain",
            {"trd_env": "SIMULATE"},
            quote_prices={"HK.MHImain": 19990.0, "HK.MHI2606": 19950.0},
        )
        by_code = {leg.code: leg for leg in legs}
        self.assertIsNone(by_code["HK.MHI2607"].current_price)


class SignedContractsFromRowTests(unittest.TestCase):
    def test_na_position_side_infers_short_from_pnl(self) -> None:
        row = pd.Series(
            {
                "qty": 6.0,
                "position_side": "N/A",
                "cost_price": 23000.0,
                "nominal_price": 23050.0,
                "pl_val": -300.0,
            }
        )
        self.assertEqual(_signed_contracts_from_row(row), -6)

    def test_na_position_side_infers_long_from_pnl(self) -> None:
        row = pd.Series(
            {
                "qty": 6.0,
                "position_side": "N/A",
                "cost_price": 23000.0,
                "nominal_price": 23050.0,
                "pl_val": 300.0,
            }
        )
        self.assertEqual(_signed_contracts_from_row(row), 6)

    def test_na_position_side_infers_short_from_can_sell_qty(self) -> None:
        row = pd.Series(
            {
                "qty": 6.0,
                "can_sell_qty": 0.0,
                "position_side": "N/A",
                "cost_price": 23000.0,
                "nominal_price": 23050.0,
            }
        )
        self.assertEqual(_signed_contracts_from_row(row), -6)

    def test_na_position_side_infers_short_from_loss_price_up(self) -> None:
        row = pd.Series(
            {
                "qty": 6.0,
                "position_side": "N/A",
                "cost_price": 23000.0,
                "nominal_price": 23050.0,
                "pl_val": -300.0,
            }
        )
        self.assertEqual(_signed_contracts_from_row(row), -6)


class BrokerNumericParsingTests(unittest.TestCase):
    def test_pick_cost_ignores_na_strings(self) -> None:
        row = pd.Series(
            {
                "cost_price": "N/A",
                "average_cost": "N/A",
                "diluted_cost": "N/A",
            }
        )
        self.assertIsNone(_pick_cost(row))

    def test_flat_row_with_na_fields(self) -> None:
        row = pd.Series(
            {
                "code": "HK.MHI2606",
                "qty": 0,
                "position_side": "N/A",
                "cost_price": "N/A",
                "nominal_price": "N/A",
                "pl_val": "N/A",
            }
        )
        broker = _broker_position_from_row(row, "HK.MHI2606", 20000.0)
        self.assertEqual(broker.contracts, 0)
        self.assertIsNone(broker.entry_price)
        self.assertIsNone(broker.pnl_val)

    def test_open_row_with_na_cost_does_not_crash(self) -> None:
        """Manual open in Futu can briefly return N/A cost fields."""
        row = pd.Series(
            {
                "code": "HK.MHI2606",
                "qty": 1.0,
                "position_side": "LONG",
                "cost_price": "N/A",
                "nominal_price": "N/A",
                "pl_val": "N/A",
            }
        )
        broker = _broker_position_from_row(row, "HK.MHI2606", 23050.0)
        self.assertEqual(broker.contracts, 1)
        self.assertIsNone(broker.entry_price)
        self.assertEqual(broker.current_price, 23050.0)

    def test_fetch_mhi_legs_skips_flat_na_row(self) -> None:
        trade = MagicMock()
        trade._ctx.position_list_query.return_value = (
            0,
            pd.DataFrame(
                [
                    {
                        "code": "HK.MHI2606",
                        "qty": 0,
                        "position_side": "N/A",
                        "cost_price": "N/A",
                        "nominal_price": "N/A",
                        "pl_val": "N/A",
                    }
                ]
            ),
        )
        legs = fetch_broker_mhi_legs(
            trade,
            "HK.MHImain",
            {"trd_env": "SIMULATE"},
            quote_prices={"HK.MHImain": 20000.0},
        )
        self.assertEqual(legs, [])


if __name__ == "__main__":
    unittest.main()
