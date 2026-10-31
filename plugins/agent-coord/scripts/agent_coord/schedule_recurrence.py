"""Validated interval and local-calendar recurrence without external dependencies."""
from __future__ import annotations

import calendar
from datetime import datetime, timedelta
import math
from zoneinfo import ZoneInfo

from .store import CoordinationError


def validate_recurrence(value):
    if value is None:
        return None
    if not isinstance(value, dict) or not isinstance(value.get("frequency"), str):
        raise CoordinationError("Choose a valid repeat schedule.")
    frequency = value["frequency"]
    if frequency == "interval":
        if set(value) != {"frequency", "minutes"} or type(value["minutes"]) is not int or not 1 <= value["minutes"] <= 525600:
            raise CoordinationError("Repeat intervals must be between 1 and 525600 whole minutes.")
    elif frequency not in {"daily", "weekdays", "weekly", "monthly"} or set(value) != {"frequency"}:
        raise CoordinationError("Choose a valid repeat schedule.")
    return dict(value)


def next_occurrence(anchor, after, timezone, recurrence):
    """Return the first occurrence strictly after ``after``, preserving the anchor.

    Calendar schedules retain local wall time. Nonexistent DST times are skipped;
    ambiguous times run once. Monthly schedules use the last day of short months.
    Interval schedules retain elapsed time, including across DST transitions.
    """
    if recurrence["frequency"] == "interval":
        seconds = recurrence["minutes"] * 60
        return anchor + max(0, math.floor((after - anchor) / seconds) + 1) * seconds
    zone = ZoneInfo(timezone)
    start = datetime.fromtimestamp(anchor, zone)
    current = datetime.fromtimestamp(max(anchor, after), zone)
    frequency = recurrence["frequency"]
    if frequency == "monthly":
        month = max(0, (current.year - start.year) * 12 + current.month - start.month)
    else:
        day = max(0, (current.date() - start.date()).days)
        if frequency == "weekly":
            day -= day % 7
    # Starting near 'after' bounds work even after years of downtime.
    for _ in range(370):
        if frequency == "monthly":
            year, month_index = divmod(start.year * 12 + start.month - 1 + month, 12)
            candidate = start.replace(year=year, month=month_index + 1,
                                      day=min(start.day, calendar.monthrange(year, month_index + 1)[1]))
            month += 1
        else:
            candidate = start + timedelta(days=day)
            day += 7 if frequency == "weekly" else 1
            if frequency == "weekdays" and candidate.weekday() >= 5:
                continue
        timestamp = candidate.timestamp()
        # A UTC round trip distinguishes a real wall time from a spring gap.
        restored = datetime.fromtimestamp(timestamp, zone)
        if timestamp > after and restored.replace(tzinfo=None) == candidate.replace(tzinfo=None):
            return timestamp
    raise CoordinationError("No upcoming occurrence could be calculated. Review this schedule.")
