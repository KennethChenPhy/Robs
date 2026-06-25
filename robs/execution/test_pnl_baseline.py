"""Tests for cut-loss / take-profit baseline vs entry price."""

from __future__ import annotations

import unittest

from robs.execution.cut_loss import PositionPnLBaseline
from robs.execution.position import UnitPositionBook
from robs.execution.position_sync import apply_broker_position, BrokerPosition
from robs.strategy.mhimain import MHImainStrategy
from robs.strategy.rules import Action


class PnLBaselineTests(unittest.TestCase):
    def test_bootstrap_existing_position_uses_launch_pnl(self) -> None:
        bl = PositionPnLBaseline(cut_loss_pts=20, take_profit_pts=40)
        bl.bootstrap(23074.0, 23089.0, 1)
        self.assertEqual(bl.baseline_pnl_pts, 15.0)
        self.assertEqual(bl.take_profit_trigger(), 55.0)
        self.assertEqual(bl.cut_loss_trigger(), -5.0)
        self.assertFalse(bl.should_take_profit(23074.0, 23128.0, 1))
        self.assertTrue(bl.should_take_profit(23074.0, 23129.0, 1))
        self.assertFalse(bl.should_cut_loss(23074.0, 23070.0, 1))
        self.assertTrue(bl.should_cut_loss(23074.0, 23069.0, 1))

    def test_user_startup_long_cut_tp_from_current_not_entry(self) -> None:
        """+40 at launch: cut 23030 / tp 23090, not entry-based 22990 / 23050."""
        bl = PositionPnLBaseline(cut_loss_pts=20, take_profit_pts=40)
        strat = MHImainStrategy(pnl_baseline=bl)
        pos = UnitPositionBook(contracts=12)
        broker = BrokerPosition(
            code="HK.MHI2606",
            contracts=12,
            qty=12,
            entry_price=23010.0,
            current_price=23050.0,
            pnl_points=40.0,
            pnl_val=None,
        )
        apply_broker_position(broker, pos, strat, bootstrap=True)
        self.assertEqual(bl.baseline_pnl_pts, 40.0)
        self.assertFalse(bl.should_take_profit(23010.0, 23050.0, 1))
        self.assertFalse(bl.should_cut_loss(23010.0, 23050.0, 1))
        cut_px = 23010.0 + bl.cut_loss_trigger()
        tp_px = 23010.0 + bl.take_profit_trigger()
        self.assertEqual(cut_px, 23030.0)
        self.assertEqual(tp_px, 23090.0)

    def test_new_bot_entry_uses_entry_baseline(self) -> None:
        bl = PositionPnLBaseline(cut_loss_pts=20, take_profit_pts=40)
        strat = MHImainStrategy(pnl_baseline=bl)
        strat.on_new_entry(23010.0)
        self.assertEqual(bl.baseline_pnl_pts, 0.0)
        self.assertTrue(bl.should_take_profit(23010.0, 23050.0, 1))
        self.assertFalse(bl.should_cut_loss(23010.0, 23050.0, 1))

    def test_manual_open_while_running_uses_entry_baseline(self) -> None:
        bl = PositionPnLBaseline(cut_loss_pts=20, take_profit_pts=40)
        strat = MHImainStrategy(pnl_baseline=bl)
        pos = UnitPositionBook()
        broker = BrokerPosition(
            code="HK.MHI2607",
            contracts=6,
            qty=6,
            entry_price=23010.0,
            current_price=23050.0,
            pnl_points=40.0,
            pnl_val=None,
        )
        from robs.execution.position_sync import refresh_broker_position

        class _Risk:
            position_shares = 0

        refresh_broker_position(broker, pos, strat, _Risk(), update_risk=False)
        self.assertEqual(bl.baseline_pnl_pts, 0.0)
        self.assertTrue(bl.should_take_profit(23010.0, 23050.0, 1))

    def test_user_scenario_no_false_take_profit_or_cut(self) -> None:
        """+35 on 2607 and -5 on 2606 must not hit TP / cut immediately at launch."""
        bl = PositionPnLBaseline(cut_loss_pts=20, take_profit_pts=40)
        strat = MHImainStrategy(pnl_baseline=bl)
        pos = UnitPositionBook()
        broker = BrokerPosition(
            code="HK.MHI2607",
            contracts=9,
            qty=9,
            entry_price=23017.0,
            current_price=23052.0,
            pnl_points=35.0,
            pnl_val=None,
        )
        apply_broker_position(broker, pos, strat, bootstrap=True)
        self.assertFalse(bl.should_take_profit(strat.entry_price, 23052.0, 1))

        bl2 = PositionPnLBaseline(cut_loss_pts=20, take_profit_pts=40)
        strat2 = MHImainStrategy(pnl_baseline=bl2)
        pos2 = UnitPositionBook()
        broker2 = BrokerPosition(
            code="HK.MHI2606",
            contracts=7,
            qty=7,
            entry_price=23102.0,
            current_price=23097.0,
            pnl_points=-5.0,
            pnl_val=None,
        )
        apply_broker_position(broker2, pos2, strat2, bootstrap=True)
        self.assertFalse(bl2.should_cut_loss(strat2.entry_price, 23097.0, 1))

    def test_short_holds_when_quote_price_missing(self) -> None:
        bl = PositionPnLBaseline(cut_loss_pts=20, take_profit_pts=40)
        strat = MHImainStrategy(pnl_baseline=bl)
        pos = UnitPositionBook(contracts=-8)
        strat.entry_price = 23019.0
        strat.pnl_baseline.reset_on_new_entry()
        signal = strat.update(0.0, pos)
        self.assertEqual(signal.action, Action.HOLD)
        self.assertIn("no quote", signal.reason)

    def test_entry_baseline_would_misfire_at_launch(self) -> None:
        """Regression: baseline 0 at launch would TP immediately on +40."""
        bl = PositionPnLBaseline(cut_loss_pts=20, take_profit_pts=40)
        bl.baseline_pnl_pts = 0.0
        self.assertTrue(bl.should_take_profit(23010.0, 23050.0, 1))


if __name__ == "__main__":
    unittest.main()
