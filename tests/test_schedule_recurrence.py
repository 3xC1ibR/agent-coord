from datetime import datetime
from pathlib import Path
import sys
import unittest
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))
from agent_coord.schedule_recurrence import next_occurrence, validate_recurrence
from agent_coord.store import CoordinationError


class RecurrenceTests(unittest.TestCase):
    zone = "America/Chicago"

    def instant(self, value):
        return datetime.fromisoformat(value).replace(tzinfo=ZoneInfo(self.zone)).timestamp()

    def next(self, frequency, anchor, after=None):
        start = self.instant(anchor)
        return datetime.fromtimestamp(next_occurrence(start, self.instant(after) if after else start,
                                      self.zone, {"frequency": frequency}), ZoneInfo(self.zone)).isoformat()

    def test_validation_rejects_extra_fields_and_nonintegral_intervals(self):
        for value in [False, "daily", {}, {"frequency": []}, {"frequency": "yearly"},
                      {"frequency": "daily", "minutes": 10}, {"frequency": "interval"},
                      *({"frequency": "interval", "minutes": minutes} for minutes in [True, 0, -1, 1.5, "60", 525601])]:
            with self.subTest(value=value), self.assertRaises(CoordinationError):
                validate_recurrence(value)
        self.assertIsNone(validate_recurrence(None))
        self.assertEqual(validate_recurrence({"frequency": "interval", "minutes": 1})["minutes"], 1)

    def test_daily_preserves_local_time_across_both_dst_transitions(self):
        self.assertEqual(self.next("daily", "2027-03-13T09:00"), "2027-03-14T09:00:00-05:00")
        self.assertEqual(self.next("daily", "2026-10-31T09:00"), "2026-11-01T09:00:00-06:00")

    def test_spring_gap_is_skipped_and_fall_fold_occurs_once(self):
        self.assertEqual(self.next("daily", "2027-03-13T02:30"), "2027-03-15T02:30:00-05:00")
        self.assertEqual(self.next("daily", "2026-10-31T01:30", "2026-11-01T01:30"), "2026-11-02T01:30:00-06:00")

    def test_weekdays_skip_weekends_and_weekly_retains_weekday(self):
        self.assertEqual(self.next("weekdays", "2026-10-30T09:00"), "2026-11-02T09:00:00-06:00")
        self.assertEqual(self.next("weekly", "2026-10-30T09:00", "2026-11-02T12:00"), "2026-11-06T09:00:00-06:00")

    def test_monthly_clamps_short_months_without_drifting_original_day(self):
        self.assertEqual(self.next("monthly", "2027-01-31T09:00"), "2027-02-28T09:00:00-06:00")
        self.assertEqual(self.next("monthly", "2027-01-31T09:00", "2027-02-28T09:00"), "2027-03-31T09:00:00-05:00")
        self.assertEqual(self.next("monthly", "2028-01-31T09:00"), "2028-02-29T09:00:00-06:00")

    def test_interval_retains_phase_and_skips_years_of_missed_occurrences(self):
        anchor = self.instant("2026-10-31T09:00")
        rule = {"frequency": "interval", "minutes": 60}
        after = self.instant("2036-11-01T11:45")
        following = next_occurrence(anchor, after, self.zone, rule)
        self.assertGreater(following, after)
        self.assertLessEqual(following - after, 3600)
        self.assertEqual((following - anchor) % 3600, 0)
        self.assertEqual(next_occurrence(anchor, anchor + 3600, self.zone, rule), anchor + 7200)

    def test_calendar_skips_years_of_downtime(self):
        self.assertEqual(self.next("daily", "2026-10-31T09:00", "2036-10-31T12:00"), "2036-11-01T09:00:00-05:00")
