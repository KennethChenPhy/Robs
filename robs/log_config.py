"""Structured logging for Robs CLI services."""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
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


def resolve_log_file(
    cfg: dict[str, Any],
    *,
    log_file: str | None = None,
    no_log_file: bool = False,
) -> Path | None:
    """Resolve log file path relative to project root; None disables file logging."""
    if no_log_file:
        return None

    log_cfg = cfg.get("logging", {})
    raw = log_file if log_file is not None else log_cfg.get("file", "logs/mhimain.jsonl")
    if raw in (None, False, ""):
        return None

    path = Path(str(raw))
    if not path.is_absolute():
        root = Path(str(cfg.get("_project_root", Path.cwd())))
        path = root / path
    return path


def setup_logging(
    cfg: dict[str, Any],
    *,
    level: str | None = None,
    fmt: str | None = None,
    log_file: str | None = None,
    no_log_file: bool = False,
) -> Path | None:
    log_cfg = cfg.get("logging", {})
    resolved_level = str(level or log_cfg.get("level", "INFO")).upper()
    resolved_fmt = str(fmt or log_cfg.get("format", "json")).lower()
    stdout_enabled = bool(log_cfg.get("stdout", True))

    formatter = StructuredFormatter(resolved_fmt)
    handlers: list[logging.Handler] = []

    if stdout_enabled:
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(formatter)
        handlers.append(stream_handler)

    file_path = resolve_log_file(cfg, log_file=log_file, no_log_file=no_log_file)
    if file_path is not None:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(file_path, encoding="utf-8")
        file_handler.setFormatter(formatter)
        handlers.append(file_handler)

    root = logging.getLogger("robs")
    root.handlers.clear()
    for handler in handlers:
        root.addHandler(handler)
    root.setLevel(resolved_level)
    root.propagate = False

    logging.getLogger("futu").setLevel(logging.WARNING)
    return file_path
