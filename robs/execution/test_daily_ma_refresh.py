"""Tests for uncertain-trend daily MA refresh."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from robs.execution.daily_ma_refresh import refresh_uncertain_daily_ma
from robs.strategy.mhimain import MHImainStrategy
from robs.strategy.trend import TrendMode


class DailyMaRefreshTests(unittest.TestCase):
    def test_skips_bull_trend(self) -> None:
        strat = MHImainStrategy.from_config({"mhimain": {}}, trend=TrendMode.BULL)
        quote = MagicMock()
        out = refresh_uncertain_daily_ma(
            quote,
            strat,
            "HK.MHImain",
            10,
            entry_armed=True,
            last_refresh_mono=0.0,
            now_mono=100.0,
            refresh_sec=30.0,
        )
        self.assertEqual(out, 0.0)
        quote.daily_ma.assert_not_called()

    def test_skips_when_not_armed(self) -> None:
        strat = MHImainStrategy.from_config({"mhimain": {}}, trend=TrendMode.UNCERTAIN)
        quote = MagicMock()
        out = refresh_uncertain_daily_ma(
            quote,
            strat,
            "HK.MHImain",
            10,
            entry_armed=False,
            last_refresh_mono=None,
            now_mono=100.0,
            refresh_sec=30.0,
        )
        self.assertIsNone(out)
        quote.daily_ma.assert_not_called()

    def test_refreshes_when_armed(self) -> None:
        strat = MHImainStrategy.from_config(
            {"mhimain": {"ma_period": 10}}, trend=TrendMode.UNCERTAIN
        )
        quote = MagicMock()
        quote.daily_ma.return_value = (True, 20123.4)
        out = refresh_uncertain_daily_ma(
            quote,
            strat,
            "HK.MHImain",
            10,
            entry_armed=True,
            last_refresh_mono=None,
            now_mono=100.0,
            refresh_sec=30.0,
        )
        self.assertEqual(out, 100.0)
        self.assertEqual(strat.ma5, 20123.4)
        quote.daily_ma.assert_called_once_with("HK.MHImain", period=10)

    def test_throttle_skips_until_elapsed(self) -> None:
        strat = MHImainStrategy.from_config({"mhimain": {}}, trend=TrendMode.UNCERTAIN)
        quote = MagicMock()
        out = refresh_uncertain_daily_ma(
            quote,
            strat,
            "HK.MHImain",
            10,
            entry_armed=True,
            last_refresh_mono=100.0,
            now_mono=120.0,
            refresh_sec=30.0,
        )
        self.assertEqual(out, 100.0)
        quote.daily_ma.assert_not_called()

    def test_force_bypasses_throttle(self) -> None:
        strat = MHImainStrategy.from_config({"mhimain": {}}, trend=TrendMode.UNCERTAIN)
        quote = MagicMock()
        quote.daily_ma.return_value = (True, 20000.0)
        out = refresh_uncertain_daily_ma(
            quote,
            strat,
            "HK.MHImain",
            10,
            entry_armed=True,
            last_refresh_mono=100.0,
            now_mono=110.0,
            refresh_sec=30.0,
            force=True,
        )
        self.assertEqual(out, 110.0)
        quote.daily_ma.assert_called_once()


if __name__ == "__main__":
    unittest.main()
