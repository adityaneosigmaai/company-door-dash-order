"""Timezone helpers. The configured `timezone` setting governs 'today', poll
times, and the displayed arrival time."""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from . import db


def tz() -> ZoneInfo:
    return ZoneInfo(db.get_setting("timezone") or "America/Los_Angeles")


def now() -> datetime:
    return datetime.now(tz())


def today() -> date:
    return now().date()


def parse_hhmm(value: str) -> tuple[int, int]:
    h, m = value.strip().split(":")
    return int(h), int(m)
