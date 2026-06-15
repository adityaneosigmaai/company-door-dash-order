"""Menu rotation logic — pure, testable helpers around the DB.

The rotating menu is a set of "weeks" (week_index 0..N-1). Each week defines,
per weekday, a veg restaurant and a non-veg restaurant plus their item options.
Which week is active on a given date is determined by counting whole weeks since
an anchor Monday, modulo the number of defined weeks. This makes the menu
auto-rotate forever without any per-day setup.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional

from . import db


def weeks_since_anchor(anchor_monday: date, target: date) -> int:
    """Whole weeks between the Monday of the anchor week and the Monday of the
    target's week. Robust to `anchor_monday` not actually being a Monday."""
    a_mon = anchor_monday - timedelta(days=anchor_monday.weekday())
    t_mon = target - timedelta(days=target.weekday())
    return (t_mon - a_mon).days // 7


def active_week_index(target: date) -> Optional[int]:
    """The rotating week index in effect for `target`, or None if no menu defined."""
    n = db.num_weeks()
    if n == 0:
        return None
    anchor_str = db.get_setting("anchor_monday") or ""
    if not anchor_str:
        # Not anchored yet: treat today's week as week 0.
        return target.isocalendar().week % n
    anchor = datetime.strptime(anchor_str, "%Y-%m-%d").date()
    return weeks_since_anchor(anchor, target) % n


def menu_for_date(target: date) -> Optional[dict]:
    """Returns {'week_index', 'veg': {...} | None, 'nonveg': {...} | None} for the
    given date, or None if no menu has been defined at all."""
    wk = active_week_index(target)
    if wk is None:
        return None
    weekday = target.weekday()
    return {
        "week_index": wk,
        "veg": db.get_menu_entry(wk, weekday, "veg"),
        "nonveg": db.get_menu_entry(wk, weekday, "nonveg"),
    }


def ensure_anchor(today: date) -> None:
    """Set the rotation anchor to this week's Monday if it has never been set.
    Called the first time a menu is created so week 0 == the week menus were set."""
    if not (db.get_setting("anchor_monday") or ""):
        monday = today - timedelta(days=today.weekday())
        db.set_setting("anchor_monday", monday.isoformat())


WEEKDAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def parse_weekday(token: str) -> Optional[int]:
    token = token.strip().lower()[:3]
    return WEEKDAY_NAMES.index(token) if token in WEEKDAY_NAMES else None
