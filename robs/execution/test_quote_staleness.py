"""Tests for quote data_time staleness."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from robs.execution.quote_staleness import (
    QuoteFreshness,
    assess_quote_freshness,
    is_new_entry,
    parse_quote_data_time,
    quote_data_age_sec,
    stale_threshold_sec,
)
from robs.strategy.rules import Action

HK = ZoneInfo("Asia/Hong_Kong")


class QuoteStalenessTests(unittest.TestCase):
    def test_parse_time_only_today(self) -> None:
        now = datetime(2026, 6, 24, 16, 30, 0, tzinfo=HK)
        parsed = parse_quote_data_time("16:28:13.300", now=now)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.hour, 16)
        self.assertEqual(parsed.minute, 28)

    def test_parse_time_only_rolls_to_previous_day_near_midnight(self) -> None:
        now = datetime(2026, 6, 24, 0, 2, 0, tzinfo=HK)
        parsed = parse_quote_data_time("23:58:00", now=now)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.day, 23)

    def test_data_age_seconds(self) -> None:
        now = datetime(2026, 6, 24, 16, 30, 0, tzinfo=HK)
        age = quote_data_age_sec("16:29:00", now=now)
        self.assertIsNotNone(age)
        self.assertAlmostEqual(age, 60.0, delta=1.0)

    def test_assess_data_stale_on_success(self) -> None:
        now = datetime(2026, 6, 24, 16, 30, 0, tzinfo=HK)
        fresh = assess_quote_freshness(
            {"risk": {"stale_poll_multiplier": 10.0}},
            1.0,
            last_successful_poll_at=now.astimezone(timezone.utc),
            data_time="16:20:00",
            now=now.astimezone(timezone.utc),
        )
        self.assertTrue(fresh.data_stale)
        self.assertFalse(fresh.poll_stale)
        self.assertTrue(fresh.block_entries)

    def test_assess_poll_stale_on_fail(self) -> None:
        last = datetime.now(timezone.utc) - timedelta(seconds=35)
        fresh = assess_quote_freshness(
            {"risk": {"stale_poll_multiplier": 10.0}},
            1.0,
            last_successful_poll_at=last,
            data_time=None,
            poll_failed=True,
        )
        self.assertTrue(fresh.poll_stale)
        self.assertFalse(fresh.data_stale)
        self.assertTrue(fresh.block_entries)

    def test_fail_before_first_success_not_stale(self) -> None:
        fresh = assess_quote_freshness(
            {"risk": {"stale_poll_multiplier": 10.0}},
            1.0,
            last_successful_poll_at=None,
            data_time=None,
            poll_failed=True,
        )
        self.assertFalse(fresh.poll_stale)
        self.assertFalse(fresh.block_entries)

    def test_assess_poll_stale_on_success_after_gap(self) -> None:
        hk_now = datetime(2026, 6, 24, 16, 30, 0, tzinfo=HK)
        prev = hk_now.astimezone(timezone.utc) - timedelta(seconds=35)
        now = hk_now.astimezone(timezone.utc)
        fresh = assess_quote_freshness(
            {"risk": {"stale_poll_multiplier": 10.0}},
            1.0,
            last_successful_poll_at=prev,
            data_time="16:29:59",
            now=now,
        )
        self.assertTrue(fresh.poll_stale)
        self.assertFalse(fresh.data_stale)
        self.assertTrue(fresh.block_entries)

    def test_missing_data_time_is_stale(self) -> None:
        now = datetime.now(timezone.utc)
        fresh = assess_quote_freshness(
            {"risk": {"stale_poll_multiplier": 10.0}},
            1.0,
            last_successful_poll_at=now - timedelta(seconds=1),
            data_time="",
            now=now,
        )
        self.assertTrue(fresh.data_stale)
        self.assertIsNone(fresh.data_age_sec)

    def test_first_success_not_poll_stale(self) -> None:
        now = datetime.now(timezone.utc)
        fresh = assess_quote_freshness(
            {"risk": {"stale_poll_multiplier": 10.0}},
            1.0,
            last_successful_poll_at=None,
            data_time="16:28:13.300",
            now=now,
        )
        self.assertFalse(fresh.poll_stale)

    def test_is_new_entry_only_when_flat(self) -> None:
        self.assertTrue(is_new_entry(0, Action.BUY))
        self.assertFalse(is_new_entry(1, Action.SELL))
        self.assertFalse(is_new_entry(0, Action.FLAT))

    def test_threshold_sec(self) -> None:
        self.assertEqual(stale_threshold_sec({"risk": {"stale_poll_multiplier": 30}}, 1.0), 30.0)

    def test_status_tag_combined(self) -> None:
        fresh = QuoteFreshness(poll_stale=True, data_stale=True)
        self.assertEqual(fresh.status_tag, " [STALE_POLL+DATA]")


if __name__ == "__main__":
    unittest.main()
