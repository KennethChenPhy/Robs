"""Shared signal types for MHImain execution."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class Action(str, Enum):
    HOLD = "HOLD"
    BUY = "BUY"
    SELL = "SELL"
    FLAT = "FLAT"


@dataclass
class Signal:
    rule: str
    action: Action
    trade_ticker: str
    reason: str
    metadata: dict[str, Any]
