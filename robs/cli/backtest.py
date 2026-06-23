#!/usr/bin/env python3
"""Replay stored bars through experience rules."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from robs.backtest.runner import BacktestRunner
from robs.config import load_config
from robs.data.bars import BarAggregator
from robs.data.store import load_bars


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest experience rules")
    parser.add_argument("--config", default="default.yaml")
    args = parser.parse_args()

    cfg = load_config(args.config)
    tickers = cfg.get("universe", {}).get("tickers", [])
    interval = cfg.get("data", {}).get("bar_interval", "1m")

    bars = load_bars(cfg, tickers, interval)
    if bars.empty and interval == "1d":
        bars_1m = load_bars(cfg, tickers, "1m")
        bars = BarAggregator.resample_daily(bars_1m)

    runner = BacktestRunner.from_config(cfg)
    result = runner.run(bars)

    print(f"bars={result.total_bars} trades={result.trade_count} pnl={result.pnl:.4f}")
    print(f"max_drawdown={result.max_drawdown_pct:.2f}% gate={'PASS' if result.ok else 'FAIL'} ({result.reason})")
    if not result.ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
