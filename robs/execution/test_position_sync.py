"""Unit tests for broker position sync and re-entry cooldown."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from robs.execution.cut_loss import PositionPnLBaseline
from robs.execution.position import UnitPositionBook
from robs.execution.position_sync import BrokerPosition, apply_broker_position, refresh_broker_position
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


if __name__ == "__main__":
    unittest.main()
