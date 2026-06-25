"""Tests for structured logging setup."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from robs.log_config import resolve_log_file, setup_logging


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

            import logging

            logging.getLogger("robs.test").info("hello", extra={"event": "test"})
            for handler in logging.getLogger("robs").handlers:
                handler.flush()

            self.assertTrue(log_path.exists())
            text = log_path.read_text(encoding="utf-8")
            self.assertIn("hello", text)
            self.assertIn('"event": "test"', text)


if __name__ == "__main__":
    unittest.main()
