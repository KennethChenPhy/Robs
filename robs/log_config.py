"""Structured logging for Robs CLI services."""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any

_LOG_RECORD_SKIP = frozenset(
    logging.LogRecord("", 0, "", 0, "", (), None, None).__dict__
) | {"message", "asctime", "msg", "args"}


class StructuredFormatter(logging.Formatter):
    """Emit JSON lines or key=value text with arbitrary extra fields."""

    def __init__(self, style: str = "json") -> None:
        super().__init__()
        self.style = style

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat()
        fields: dict[str, Any] = {
            k: v
            for k, v in record.__dict__.items()
            if k not in _LOG_RECORD_SKIP and v is not None and not k.startswith("_")
        }
        message = record.getMessage()
        if self.style == "text":
            parts = [f"ts={ts}", f"level={record.levelname}", f"logger={record.name}"]
            for key in sorted(fields):
                parts.append(f"{key}={fields[key]}")
            parts.append(f"msg={message}")
            return " ".join(parts)
        payload = {
            "ts": ts,
            "level": record.levelname,
            "logger": record.name,
            "message": message,
            **fields,
        }
        return json.dumps(payload, default=str, ensure_ascii=False)


def setup_logging(cfg: dict[str, Any], *, level: str | None = None, fmt: str | None = None) -> None:
    log_cfg = cfg.get("logging", {})
    resolved_level = str(level or log_cfg.get("level", "INFO")).upper()
    resolved_fmt = str(fmt or log_cfg.get("format", "json")).lower()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(StructuredFormatter(resolved_fmt))

    root = logging.getLogger("robs")
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(resolved_level)
    root.propagate = False

    logging.getLogger("futu").setLevel(logging.WARNING)
