"""Tests for structured logging setup."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import logging
from datetime import datetime

from robs.log_config import HK, StructuredFormatter, format_hk_log_ts, resolve_log_file, setup_logging


class LogConfigTests(unittest.TestCase):
    def test_resolve_relative_to_project_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = {"_project_root": tmp, "logging": {"file": "logs/mhimain.jsonl"}}
            path = resolve_log_file(cfg)
            self.assertEqual(path, Path(tmp) / "logs" / "mhimain.jsonl")

    def test_no_log_file_flag(self) -> None:
        cfg = {"_project_root": "/tmp", "logging": {"file": "logs/mhimain.jsonl"}}
        self.assertIsNone(resolve_log_file(cfg, no_log_file=True))

    def test_setup_creates_log_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = {
                "_project_root": tmp,
                "logging": {"file": "logs/test.jsonl", "stdout": False},
            }
            log_path = setup_logging(cfg)
            self.assertIsNotNone(log_path)
            assert log_path is not None

            logging.getLogger("robs.test").info("hello", extra={"event": "test"})
            for handler in logging.getLogger("robs").handlers:
                handler.flush()

            self.assertTrue(log_path.exists())
            text = log_path.read_text(encoding="utf-8")
            self.assertIn("hello", text)
            self.assertIn('"event": "test"', text)

    def test_poll_format_uses_hk_ts_without_data_time(self) -> None:
        record = logging.LogRecord(
            name="robs.mhimain",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="2026-06-25T22:31:20 MHI2606 ent 23074 long (+7), last 23099, pnl +25, cut 23054, tp 23114",
            args=(),
            exc_info=None,
        )
        record.event = "poll"
        text = StructuredFormatter("text").format(record)
        self.assertEqual(
            text,
            "2026-06-25T22:31:20 MHI2606 ent 23074 long (+7), last 23099, pnl +25, cut 23054, tp 23114",
        )
        self.assertNotIn("ts=", text)

        payload = StructuredFormatter("json").format(record)
        self.assertIn('"poll": "2026-06-25T22:31:20 MHI2606 ent 23074', payload)
        self.assertNotIn('"ts"', payload)
        self.assertNotIn('"message"', payload)

    def test_poll_format_detects_line_without_event(self) -> None:
        record = logging.LogRecord(
            name="robs.mhimain",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="2026-06-25T22:31:20 MHI2607 ent 23017 long (+9), last 23052, pnl +35, cut 22997, tp 23057",
            args=(),
            exc_info=None,
        )
        text = StructuredFormatter("text").format(record)
        self.assertEqual(
            text,
            "2026-06-25T22:31:20 MHI2607 ent 23017 long (+9), last 23052, pnl +35, cut 22997, tp 23057",
        )


    def test_non_poll_ts_format(self) -> None:
        record = logging.LogRecord(
            name="robs.mhimain",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="startup",
            args=(),
            exc_info=None,
        )
        record.created = datetime(2026, 6, 25, 23, 6, 34, 757643, tzinfo=HK).timestamp()
        record.event = "startup"
        text = StructuredFormatter("text").format(record)
        self.assertIn("ts=2026-06-25T23:06:34", text)
        self.assertNotIn("+08:00", text)
        self.assertNotIn(".757", text)

        payload = StructuredFormatter("json").format(record)
        self.assertIn('"ts": "2026-06-25T23:06:34"', payload)

    def test_format_hk_log_ts(self) -> None:
        when = datetime(2026, 6, 25, 23, 6, 34, 757643, tzinfo=HK)
        self.assertEqual(format_hk_log_ts(when), "2026-06-25T23:06:34")


if __name__ == "__main__":
    unittest.main()
