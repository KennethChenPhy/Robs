"""Audible alerts for trading events."""

from __future__ import annotations

import os
import sys


def play_panic_alarm() -> None:
    """Play system alert on macOS (same pattern as monitor_100points_playsound.py)."""
    if sys.platform != "darwin":
        return
    os.system("afplay /System/Library/Sounds/Submarine.aiff")
