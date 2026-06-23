"""Parquet persistence for snapshots and bars."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Iterable

import pandas as pd

from robs.config import data_dir


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def snapshot_path(root: Path, ticker: str, day: date) -> Path:
    safe = ticker.replace(".", "_")
    return root / "snapshots" / safe / f"{day.isoformat()}.parquet"


def bar_path(root: Path, ticker: str, interval: str, day: date) -> Path:
    safe = ticker.replace(".", "_")
    return root / "bars" / interval / safe / f"{day.isoformat()}.parquet"


def append_snapshots(cfg: dict, rows: pd.DataFrame) -> Path | None:
    if rows.empty:
        return None

    root = data_dir(cfg)
    written: Path | None = None
    for ticker, group in rows.groupby("code"):
        day = pd.Timestamp(group["timestamp"].iloc[-1]).date()
        path = snapshot_path(root, str(ticker), day)
        _ensure_dir(path.parent)
        if path.exists():
            existing = pd.read_parquet(path)
            combined = pd.concat([existing, group], ignore_index=True)
            combined = combined.drop_duplicates(subset=["timestamp", "code"], keep="last")
        else:
            combined = group
        combined.to_parquet(path, index=False)
        written = path
    return written


def append_bars(cfg: dict, bars: pd.DataFrame, interval: str) -> Path | None:
    if bars.empty:
        return None

    root = data_dir(cfg)
    written: Path | None = None
    for ticker, group in bars.groupby("code"):
        day = pd.Timestamp(group["bar_time"].iloc[-1]).date()
        path = bar_path(root, str(ticker), interval, day)
        _ensure_dir(path.parent)
        if path.exists():
            existing = pd.read_parquet(path)
            combined = pd.concat([existing, group], ignore_index=True)
            combined = combined.drop_duplicates(subset=["bar_time", "code"], keep="last")
        else:
            combined = group
        combined.to_parquet(path, index=False)
        written = path
    return written


def load_bars(cfg: dict, tickers: Iterable[str], interval: str) -> pd.DataFrame:
    root = data_dir(cfg)
    frames: list[pd.DataFrame] = []
    bar_root = root / "bars" / interval
    if not bar_root.exists():
        return pd.DataFrame()

    for ticker in tickers:
        safe = ticker.replace(".", "_")
        ticker_dir = bar_root / safe
        if not ticker_dir.exists():
            continue
        for path in sorted(ticker_dir.glob("*.parquet")):
            frames.append(pd.read_parquet(path))

    if not frames:
        return pd.DataFrame()

    df = pd.concat(frames, ignore_index=True)
    df = df.sort_values(["code", "bar_time"]).reset_index(drop=True)
    return df


def load_snapshots(cfg: dict, tickers: Iterable[str]) -> pd.DataFrame:
    root = data_dir(cfg)
    frames: list[pd.DataFrame] = []
    snap_root = root / "snapshots"
    if not snap_root.exists():
        return pd.DataFrame()

    for ticker in tickers:
        safe = ticker.replace(".", "_")
        ticker_dir = snap_root / safe
        if not ticker_dir.exists():
            continue
        for path in sorted(ticker_dir.glob("*.parquet")):
            frames.append(pd.read_parquet(path))

    if not frames:
        return pd.DataFrame()

    df = pd.concat(frames, ignore_index=True)
    df = df.sort_values(["code", "timestamp"]).reset_index(drop=True)
    return df
