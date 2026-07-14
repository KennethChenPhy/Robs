"""Tests for ntfy notification helper."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from robs.execution import ntfy_notify as ntfy


class NtfyConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        ntfy._active = None
        ntfy._configured = False
        ntfy._last_sent.clear()
        self._env_keys = (
            "NTFY_TOPIC",
            "NTFY_SERVER",
            "NTFY_TOKEN",
            "WATCHDOG_NTFY_TOPIC",
            "WATCHDOG_NTFY_SERVER",
            "WATCHDOG_NTFY_TOKEN",
            "ROBS_WATCHDOG_ENV",
        )
        self._saved = {k: os.environ.get(k) for k in self._env_keys}
        for k in self._env_keys:
            os.environ.pop(k, None)

    def tearDown(self) -> None:
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        ntfy._active = None
        ntfy._configured = False
        ntfy._last_sent.clear()

    def test_disabled_without_topic(self) -> None:
        self.assertIsNone(ntfy.ntfy_config_from_env_and_cfg({}))

    def test_watchdog_topic(self) -> None:
        os.environ["WATCHDOG_NTFY_TOPIC"] = "robs-mhimain-test"
        cfg = ntfy.ntfy_config_from_env_and_cfg({})
        self.assertIsNotNone(cfg)
        assert cfg is not None
        self.assertEqual(cfg.topic, "robs-mhimain-test")
        self.assertEqual(cfg.server, "https://ntfy.sh")

    def test_yaml_disable(self) -> None:
        os.environ["WATCHDOG_NTFY_TOPIC"] = "robs-mhimain-test"
        cfg = ntfy.ntfy_config_from_env_and_cfg({"ntfy": {"enabled": False}})
        self.assertIsNone(cfg)

    def test_load_env_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "watchdog.env"
            path.write_text(
                "WATCHDOG_NTFY_TOPIC=from-file\nWATCHDOG_NTFY_SERVER=https://example.com\n",
                encoding="utf-8",
            )
            os.environ["ROBS_WATCHDOG_ENV"] = str(path)
            ntfy.load_env_file()
            self.assertEqual(os.environ.get("WATCHDOG_NTFY_TOPIC"), "from-file")
            cfg = ntfy.ntfy_config_from_env_and_cfg({})
            assert cfg is not None
            self.assertEqual(cfg.server, "https://example.com")


class NtfySendTests(unittest.TestCase):
    def setUp(self) -> None:
        ntfy._last_sent.clear()
        self.cfg = ntfy.NtfyConfig(
            enabled=True, server="https://ntfy.sh", topic="robs-test"
        )

    def tearDown(self) -> None:
        ntfy._last_sent.clear()

    @patch("robs.execution.ntfy_notify.urllib.request.urlopen")
    def test_send_posts(self, mock_urlopen: MagicMock) -> None:
        resp = MagicMock()
        resp.read.return_value = b"ok"
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = None
        mock_urlopen.return_value = resp

        ok = ntfy.send_ntfy("Title", "Body", config=self.cfg, dedupe_key="once")
        self.assertTrue(ok)
        mock_urlopen.assert_called_once()
        req = mock_urlopen.call_args[0][0]
        self.assertEqual(req.full_url, "https://ntfy.sh/robs-test")
        self.assertEqual(req.get_method(), "POST")

        ok2 = ntfy.send_ntfy("Title", "Body", config=self.cfg, dedupe_key="once")
        self.assertFalse(ok2)
        self.assertEqual(mock_urlopen.call_count, 1)

    @patch("robs.execution.ntfy_notify.send_ntfy")
    def test_notify_helpers(self, mock_send: MagicMock) -> None:
        ntfy.notify_min_hold(
            contract="HK.MHI2607",
            side="short",
            expires_hkt="2026-07-15T19:31:00",
            min_hold_hours=24,
        )
        ntfy.notify_reentry(
            contract="HK.MHI2607",
            expires_hkt="2026-07-16T01:07:00",
            reentry_minimum_hours=4,
            reentry_move_pts=420,
            reentry_trading_hours=34,
        )
        self.assertEqual(mock_send.call_count, 2)


if __name__ == "__main__":
    unittest.main()
