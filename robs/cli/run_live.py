#!/usr/bin/env python3
"""Live trading loop: poll -> rules -> risk -> orders."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from robs.config import load_config
from robs.data.bars import BarAggregator
from robs.data.futu_client import QuoteClient, TradeClient, endpoints_from_config
from robs.data.poller import SnapshotPoller
from robs.data.store import append_bars, append_snapshots
from robs.execution.risk import RiskManager
from robs.execution.trader import Trader
from robs.strategy.engine import StrategyEngine


def main() -> None:
    parser = argparse.ArgumentParser(description="Run live/paper strategy loop")
    parser.add_argument("--config", default="default.yaml")
    parser.add_argument("--iterations", type=int, default=None)
    args = parser.parse_args()

    cfg = load_config(args.config)
    tickers = list(cfg.get("universe", {}).get("tickers", []))
    rules_cfg = cfg.get("rules", {})
    if "point_breakout" in rules_cfg:
        sym = rules_cfg["point_breakout"].get("symbol")
        if sym and sym not in tickers:
            tickers.append(str(sym))

    interval = cfg.get("data", {}).get("bar_interval", "1m")
    poll_interval = float(cfg.get("data", {}).get("poll_interval_sec", 5))
    endpoints = endpoints_from_config(cfg)

    engine = StrategyEngine(cfg)
    risk = RiskManager(cfg)
    risk.set_equity(1.0)

    bars = BarAggregator(interval=interval)

    with QuoteClient(endpoints) as quote, TradeClient(endpoints) as trade:
        poller = SnapshotPoller(quote, tickers)
        trader = Trader(cfg=cfg, trade_client=trade, risk=risk)

        print(f"Live loop paper={trader.paper} tickers={tickers}")

        count = 0
        try:
            while args.iterations is None or count < args.iterations:
                frame = poller.poll_once()
                if frame.empty:
                    time.sleep(poll_interval)
                    count += 1
                    continue

                if risk.check_stale(poller.last_poll_at, poll_interval):
                    print("WARN: stale OpenD snapshot")

                append_snapshots(cfg, frame)
                closed = bars.ingest(frame)
                if not closed.empty:
                    append_bars(cfg, closed, interval)

                signals = engine.update_from_snapshot_frame(frame)
                signal = engine.pick_action(signals)
                if signal is not None:
                    print(f"signal={signal.action.value} rule={signal.rule} reason={signal.reason}")
                    result = trader.execute(signal)
                    print("order:", result)

                if risk.killed:
                    print("KILL SWITCH:", risk.kill_reason)
                    break

                time.sleep(poll_interval)
                count += 1
        except KeyboardInterrupt:
            print("\nStopped.")


if __name__ == "__main__":
    main()
