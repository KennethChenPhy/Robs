#!/usr/bin/env python3
"""Record snapshots and 1m bars from Futu OpenD."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from robs.config import load_config
from robs.data.bars import BarAggregator
from robs.data.futu_client import QuoteClient, endpoints_from_config
from robs.data.poller import SnapshotPoller
from robs.data.store import append_bars, append_snapshots


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect Futu snapshots and bars")
    parser.add_argument("--config", default="default.yaml")
    parser.add_argument("--iterations", type=int, default=None, help="Stop after N polls")
    args = parser.parse_args()

    cfg = load_config(args.config)
    tickers = cfg.get("universe", {}).get("tickers", [])
    interval = cfg.get("data", {}).get("bar_interval", "1m")
    poll_interval = float(cfg.get("data", {}).get("poll_interval_sec", 5))

    endpoints = endpoints_from_config(cfg)
    bars = BarAggregator(interval=interval)

    with QuoteClient(endpoints) as quote:
        ok, state = quote.global_state()
        if not ok:
            print("Failed to connect to OpenD")
            sys.exit(1)
        print("Connected:", state)

        poller = SnapshotPoller(quote, tickers)

        def on_snapshot(frame):
            append_snapshots(cfg, frame)
            closed = bars.ingest(frame)
            if not closed.empty:
                append_bars(cfg, closed, interval)
                print(f"closed {len(closed)} bar(s)")

        print(f"Collecting {tickers} every {poll_interval}s ... Ctrl+C to stop")
        poller.run(poll_interval, on_snapshot, max_iterations=args.iterations)


if __name__ == "__main__":
    main()
