"""Push notifications via ntfy (same topic as the mhimain watchdog)."""

from __future__ import annotations

import logging
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LOG = logging.getLogger("robs.ntfy")

_DEFAULT_ENV_FILE = Path.home() / ".config" / "robs" / "watchdog.env"
_last_sent: dict[str, float] = {}
_DEDUP_SEC = 90.0


@dataclass(frozen=True)
class NtfyConfig:
    enabled: bool
    server: str
    topic: str
    token: str | None = None

    @property
    def url(self) -> str:
        return f"{self.server.rstrip('/')}/{self.topic}"


def load_env_file(path: Path | None = None) -> None:
    """Load KEY=VALUE lines into os.environ (does not override existing keys)."""
    env_path = path or Path(os.environ.get("ROBS_WATCHDOG_ENV", str(_DEFAULT_ENV_FILE)))
    if not env_path.is_file():
        return
    try:
        text = env_path.read_text(encoding="utf-8")
    except OSError as exc:
        LOG.warning("could not read ntfy env file %s: %s", env_path, exc)
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key and key not in os.environ:
            os.environ[key] = value


def ntfy_config_from_env_and_cfg(cfg: dict[str, Any] | None = None) -> NtfyConfig | None:
    """Build config from env (watchdog) and optional mhimain.ntfy YAML."""
    load_env_file()
    ntfy_cfg = (cfg or {}).get("ntfy") or (cfg or {}).get("mhimain", {}).get("ntfy") or {}
    enabled = ntfy_cfg.get("enabled")
    if enabled is False:
        return None

    topic = (
        str(ntfy_cfg.get("topic") or "").strip()
        or os.environ.get("NTFY_TOPIC", "").strip()
        or os.environ.get("WATCHDOG_NTFY_TOPIC", "").strip()
    )
    if not topic or "CHANGE_ME" in topic:
        return None

    server = (
        str(ntfy_cfg.get("server") or "").strip()
        or os.environ.get("NTFY_SERVER", "").strip()
        or os.environ.get("WATCHDOG_NTFY_SERVER", "").strip()
        or "https://ntfy.sh"
    )
    token = (
        str(ntfy_cfg.get("token") or "").strip()
        or os.environ.get("NTFY_TOKEN", "").strip()
        or os.environ.get("WATCHDOG_NTFY_TOKEN", "").strip()
        or None
    )
    return NtfyConfig(enabled=True, server=server, topic=topic, token=token or None)


_active: NtfyConfig | None = None
_configured = False


def configure_ntfy(cfg: dict[str, Any] | None = None) -> NtfyConfig | None:
    """Call once at trader startup."""
    global _active, _configured
    _active = ntfy_config_from_env_and_cfg(cfg)
    _configured = True
    if _active is None:
        LOG.info("ntfy notifications disabled (no topic / enabled: false)")
    else:
        LOG.info(
            "ntfy notifications enabled",
            extra={"event": "ntfy_config", "server": _active.server, "topic": _active.topic},
        )
    return _active


def send_ntfy(
    title: str,
    message: str,
    *,
    tags: str = "chart_with_upwards_trend",
    priority: str = "default",
    dedupe_key: str | None = None,
    config: NtfyConfig | None = None,
) -> bool:
    """POST to ntfy. Returns True if sent. Failures are logged, never raised."""
    cfg = config if config is not None else _active
    if cfg is None:
        if not _configured:
            cfg = ntfy_config_from_env_and_cfg()
        if cfg is None:
            return False

    key = dedupe_key or f"{title}|{message}"
    now = time.monotonic()
    prev = _last_sent.get(key, 0.0)
    if now - prev < _DEDUP_SEC:
        return False
    _last_sent[key] = now

    headers = {
        "Title": title,
        "Priority": priority,
        "Tags": tags,
        "Content-Type": "text/plain; charset=utf-8",
    }
    if cfg.token:
        headers["Authorization"] = f"Bearer {cfg.token}"

    data = message.encode("utf-8")
    req = urllib.request.Request(cfg.url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            resp.read()
        LOG.info(
            "ntfy sent: %s",
            title,
            extra={"event": "ntfy_sent", "title": title},
        )
        return True
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        LOG.warning(
            "ntfy send failed: %s",
            exc,
            extra={"event": "ntfy_failed", "error": str(exc), "title": title},
        )
        return False


def notify_min_hold(
    *,
    contract: str | None,
    side: str,
    expires_hkt: str,
    min_hold_hours: float,
) -> None:
    label = contract or "MHI"
    send_ntfy(
        f"Robs opened {side} {label}",
        f"Position opened ({side}).\n"
        f"Cut loss allowed after: {expires_hkt} HKT\n"
        f"(min hold {min_hold_hours:.0f}h HKEX trading)",
        tags="arrow_up,hourglass",
        priority="high",
        dedupe_key=f"min_hold|{label}|{expires_hkt}",
    )


def notify_reentry(
    *,
    contract: str | None,
    expires_hkt: str,
    reentry_minimum_hours: float,
    reentry_move_pts: float,
    reentry_trading_hours: float,
) -> None:
    label = contract or "MHI"
    send_ntfy(
        f"Robs flat {label}",
        f"Position closed (flat).\n"
        f"Re-entry (time path) after: {expires_hkt} HKT\n"
        f"Or earlier: {reentry_minimum_hours:.0f}h HKEX + {reentry_move_pts:.0f}pt move\n"
        f"(max wait {reentry_trading_hours:.0f}h HKEX)",
        tags="arrow_down,hourglass",
        priority="high",
        dedupe_key=f"reentry|{label}|{expires_hkt}",
    )
