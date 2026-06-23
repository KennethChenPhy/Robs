#!/usr/bin/env python3
"""Print live feature + rule state without placing orders."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from robs.config import load_config
from robs.data.futu_client import QuoteClient, endpoints_from_config
from robs.data.poller import SnapshotPoller
from robs.strategy.engine import StrategyEngine


def main() -> None:
    parser = argparse.ArgumentParser(description="Debug rule state")
    parser.add_argument("--config", default="default.yaml")
    parser.add_argument("--iterations", type=int, default=20)
    args = parser.parse_args()

    cfg = load_config(args.config)
    tickers = list(cfg.get("universe", {}).get("tickers", []))
    poll_interval = float(cfg.get("data", {}).get("poll_interval_sec", 5))
    endpoints = endpoints_from_config(cfg)
    engine = StrategyEngine(cfg)

    with QuoteClient(endpoints) as quote:
        poller = SnapshotPoller(quote, tickers)
        for _ in range(args.iterations):
            frame = poller.poll_once()
            if frame.empty:
                time.sleep(poll_interval)
                continue
            signals = engine.update_from_snapshot_frame(frame)
            for sig in signals:
                print(f"{sig.rule}: {sig.action.value} — {sig.reason} {sig.metadata}")
            time.sleep(poll_interval)


if __name__ == "__main__":
    main()
