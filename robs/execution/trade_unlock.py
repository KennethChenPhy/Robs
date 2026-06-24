"""Live trade authorization for Futu OpenD (REAL accounts only)."""

from __future__ import annotations

import getpass
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from futu import RET_OK

from robs.config import trd_env_name

LOG = logging.getLogger("robs.trade_unlock")


def _is_real(cfg: dict[str, Any]) -> bool:
    return trd_env_name(cfg) == "REAL"


def trade_unlock_valid_days(cfg: dict[str, Any]) -> float:
    return float(cfg.get("mhimain", {}).get("trade_unlock_valid_days", 30))


def warn_short_trade_unlock_window(cfg: dict[str, Any]) -> None:
    if not _is_real(cfg):
        return
    days = trade_unlock_valid_days(cfg)
    if days < 3:
        LOG.warning(
            f"trade_unlock_valid_days={days:g} is below 3 — auth expires quickly",
            extra={
                "event": "trade_unlock_warn",
                "trade_unlock_valid_days": days,
            },
        )


def prompt_startup_password(
    cfg: dict[str, Any],
    *,
    cli_password: str | None = None,
) -> str:
    """REAL script restart: always re-authorize (prompt on TTY)."""
    if cli_password:
        return cli_password

    mhi = cfg.get("mhimain", {})
    from_cfg = str(mhi.get("trade_password", "") or "").strip()
    from_env = str(os.environ.get("FUTU_TRADE_PASSWORD", "") or "").strip()

    if sys.stdin.isatty():
        if from_cfg or from_env:
            LOG.info(
                "REAL mode: re-authorize trade (config/env ignored on interactive restart)",
                extra={"event": "trade_unlock_prompt"},
            )
        pwd = getpass.getpass("Futu trade password: ")
        if not pwd:
            raise SystemExit("Trade password required for REAL trading.")
        return pwd

    if from_env:
        return from_env
    if from_cfg:
        return from_cfg

    raise SystemExit(
        "REAL trading requires trade re-authorization on each start. "
        "Run interactively, set FUTU_TRADE_PASSWORD, or add mhimain.trade_password for systemd."
    )


def resolve_trade_password(cfg: dict[str, Any], *, cli_password: str | None = None) -> str | None:
    if not _is_real(cfg):
        return None
    return prompt_startup_password(cfg, cli_password=cli_password)


@dataclass
class TradeUnlockSession:
    """REAL: authorize on each script start; lock orders after valid_days without re-auth."""

    valid_days: float = 30.0
    password: str = ""
    authorized_at: datetime | None = None
    locked: bool = False
    _opend_healthy: bool = field(default=True, repr=False)
    _expiry_close_announced: bool = field(default=False, repr=False)
    _broker_unlocked_for_close: bool = field(default=False, repr=False)

    def is_expired(self, now: datetime | None = None) -> bool:
        if self.authorized_at is None:
            return True
        now = now or datetime.now(timezone.utc)
        started = self.authorized_at
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        return (now - started) > timedelta(days=self.valid_days)

    def authorize(self, trade: Any, password: str) -> bool:
        self.password = password
        ret, data = trade._ctx.unlock_trade(password)
        if ret == RET_OK:
            self.authorized_at = datetime.now(timezone.utc)
            self.locked = False
            self._expiry_close_announced = False
            self._broker_unlocked_for_close = False
            LOG.info(
                "trade authorized",
                extra={"event": "trade_unlock", "ok": True, "detail": str(data) if data else None},
            )
            return True

        self.locked = True
        LOG.error(
            "trade authorization failed",
            extra={"event": "trade_unlock", "ok": False, "detail": str(data)},
        )
        return False

    def ensure_broker_unlocked(self, trade: Any) -> bool:
        """Unlock on broker for an order; does not extend the session auth window."""
        if not self.password:
            return False
        if self._broker_unlocked_for_close:
            return True
        ret, _data = trade._ctx.unlock_trade(self.password)
        if ret == RET_OK:
            self._broker_unlocked_for_close = True
        return ret == RET_OK

    def note_close_order_failed(self) -> None:
        """Allow another broker unlock attempt before retrying expiry close."""
        self._broker_unlocked_for_close = False

    def ensure_authorized(
        self,
        cfg: dict[str, Any],
        trade: Any,
        *,
        position_contracts: int = 0,
    ) -> bool:
        """Before orders: OK if within window; if expired and flat, lock and prompt."""
        if not self.is_expired() and not self.locked:
            return True

        if self.is_expired() and position_contracts != 0:
            return False

        return self._lock_and_reauthorize(cfg, trade)

    def announce_expiry_close(self) -> None:
        if self._expiry_close_announced:
            return
        self._expiry_close_announced = True
        LOG.warning(
            "trade authorization expired — closing open position before trade lock",
            extra={
                "event": "auth_expiry",
                "valid_days": self.valid_days,
            },
        )

    def _lock_and_reauthorize(self, cfg: dict[str, Any], trade: Any) -> bool:
        self.locked = True
        if not sys.stdin.isatty():
            LOG.error(
                "trade locked: authorization expired — restart with fresh password",
                extra={
                    "event": "trade_locked",
                    "valid_days": self.valid_days,
                    "tty": False,
                },
            )
            return False

        LOG.error(
            "trade locked: re-authorization required",
            extra={
                "event": "trade_locked",
                "valid_days": self.valid_days,
                "tty": True,
            },
        )
        pwd = getpass.getpass("Futu trade password: ")
        if not pwd:
            LOG.warning(
                "trade remains locked — password required",
                extra={"event": "trade_locked", "reason": "empty_password"},
            )
            return False
        return self.authorize(trade, pwd)

    def mark_opend_unhealthy(self) -> bool:
        if self._opend_healthy:
            self._opend_healthy = False
            return True
        return False

    def on_opend_recovered(self, trade: Any) -> bool:
        """OpenD back online — no unlock API call; session auth window still applies."""
        self._opend_healthy = True
        return not self.locked and not self.is_expired()

    def invalidate(self) -> None:
        """Order rejected for locked trade — require re-authorization."""
        self.locked = True


def maybe_create_unlock_session(
    cfg: dict[str, Any],
    trade: Any,
    *,
    cli_password: str | None = None,
) -> TradeUnlockSession | None:
    if not _is_real(cfg):
        return None

    pwd = prompt_startup_password(cfg, cli_password=cli_password)
    valid_days = trade_unlock_valid_days(cfg)
    session = TradeUnlockSession(valid_days=valid_days)
    if not session.authorize(trade, pwd):
        raise SystemExit("Cannot start REAL trader without trade authorization.")
    LOG.info(
        f"trade authorization valid for {valid_days:.0f} days",
        extra={"event": "trade_unlock", "valid_days": valid_days},
    )
    return session
