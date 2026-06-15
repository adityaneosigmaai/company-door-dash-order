"""SQLite persistence layer.

Deliberately thin (no ORM) — this is a small internal tool. All state lives in
one file so the bot survives restarts: scheduler reloads, open polls resume, and
menus/members persist.

Tables
------
settings   key/value operational config (see config.DEFAULT_SETTINGS)
members    people who participate, their default group + no-response behaviour
menu       rotating weekly menu: (week_index, weekday, group) -> restaurant+options
sessions   one row per day's poll: links, message ts, status, arrival time
responses  one row per (date, user): their group choice + item + in/out status
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator, Optional

from . import config

_lock = threading.Lock()


SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS members (
    user_id           TEXT PRIMARY KEY,
    display_name      TEXT,
    default_group     TEXT NOT NULL DEFAULT 'either'  -- veg | nonveg | either
        CHECK (default_group IN ('veg','nonveg','either')),
    no_response_action TEXT                            -- NULL = use global default
        CHECK (no_response_action IS NULL OR no_response_action IN ('out','last')),
    active            INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS menu (
    week_index INTEGER NOT NULL,   -- which rotating week (0..N-1)
    weekday    INTEGER NOT NULL,   -- 0=Mon .. 6=Sun
    grp        TEXT NOT NULL CHECK (grp IN ('veg','nonveg')),
    restaurant TEXT NOT NULL,
    options    TEXT NOT NULL DEFAULT '[]',  -- JSON list of item strings
    PRIMARY KEY (week_index, weekday, grp)
);

CREATE TABLE IF NOT EXISTS sessions (
    date            TEXT PRIMARY KEY,  -- YYYY-MM-DD (local tz)
    channel_id      TEXT,
    message_ts      TEXT,
    status          TEXT NOT NULL DEFAULT 'open'  -- open | closed | skipped
        CHECK (status IN ('open','closed','skipped')),
    arrival_time    TEXT,
    veg_restaurant  TEXT,
    nonveg_restaurant TEXT,
    veg_url         TEXT,
    nonveg_url      TEXT,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS responses (
    date       TEXT NOT NULL,
    user_id    TEXT NOT NULL,
    grp        TEXT CHECK (grp IS NULL OR grp IN ('veg','nonveg')),
    item       TEXT,
    status     TEXT NOT NULL DEFAULT 'in'  -- in | out
        CHECK (status IN ('in','out')),
    auto       INTEGER NOT NULL DEFAULT 0,  -- 1 if applied by the no-response default
    updated_at TEXT NOT NULL,
    PRIMARY KEY (date, user_id)
);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """Serialized connection. The lock keeps writes safe across the Bolt thread
    pool and the scheduler thread without needing WAL gymnastics."""
    with _lock:
        conn = sqlite3.connect(config.DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        # Seed defaults only for keys that don't exist yet.
        for key, value in config.DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (key, value)
            )


# ---- settings -------------------------------------------------------------

def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    with connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def all_settings() -> dict[str, str]:
    with connect() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    return {r["key"]: r["value"] for r in rows}


# ---- members --------------------------------------------------------------

def upsert_member(user_id: str, display_name: str = "", default_group: str = "either",
                  no_response_action: Optional[str] = None, active: bool = True) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO members(user_id, display_name, default_group, no_response_action, active) "
            "VALUES(?,?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "  display_name=excluded.display_name, "
            "  default_group=excluded.default_group, "
            "  no_response_action=excluded.no_response_action, "
            "  active=excluded.active",
            (user_id, display_name, default_group, no_response_action, int(active)),
        )


def set_member_active(user_id: str, active: bool) -> None:
    with connect() as conn:
        conn.execute("UPDATE members SET active=? WHERE user_id=?", (int(active), user_id))


def active_members() -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute("SELECT * FROM members WHERE active=1").fetchall()


def get_member(user_id: str) -> Optional[sqlite3.Row]:
    with connect() as conn:
        return conn.execute("SELECT * FROM members WHERE user_id=?", (user_id,)).fetchone()


# ---- menu -----------------------------------------------------------------

def set_menu(week_index: int, weekday: int, grp: str, restaurant: str, options: list[str]) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO menu(week_index, weekday, grp, restaurant, options) VALUES(?,?,?,?,?) "
            "ON CONFLICT(week_index, weekday, grp) DO UPDATE SET "
            "  restaurant=excluded.restaurant, options=excluded.options",
            (week_index, weekday, grp, restaurant, json.dumps(options)),
        )


def get_menu_entry(week_index: int, weekday: int, grp: str) -> Optional[dict]:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM menu WHERE week_index=? AND weekday=? AND grp=?",
            (week_index, weekday, grp),
        ).fetchone()
    if not row:
        return None
    return {"restaurant": row["restaurant"], "options": json.loads(row["options"])}


def all_menu() -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM menu ORDER BY week_index, weekday, grp"
        ).fetchall()


def num_weeks() -> int:
    """How many distinct rotating weeks have been defined (>=1)."""
    with connect() as conn:
        row = conn.execute("SELECT MAX(week_index) AS m FROM menu").fetchone()
    return (row["m"] + 1) if row and row["m"] is not None else 0


# ---- sessions -------------------------------------------------------------

def create_session(date: str, channel_id: str, arrival_time: str,
                   veg_restaurant: str, nonveg_restaurant: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO sessions(date, channel_id, status, arrival_time, "
            "  veg_restaurant, nonveg_restaurant, created_at) "
            "VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(date) DO UPDATE SET "
            "  channel_id=excluded.channel_id, arrival_time=excluded.arrival_time, "
            "  veg_restaurant=excluded.veg_restaurant, "
            "  nonveg_restaurant=excluded.nonveg_restaurant",
            (date, channel_id, "open", arrival_time, veg_restaurant, nonveg_restaurant, _now_iso()),
        )


def get_session(date: str) -> Optional[sqlite3.Row]:
    with connect() as conn:
        return conn.execute("SELECT * FROM sessions WHERE date=?", (date,)).fetchone()


def update_session(date: str, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE sessions SET {cols} WHERE date=?", (*fields.values(), date))


def open_sessions() -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute("SELECT * FROM sessions WHERE status='open'").fetchall()


# ---- responses ------------------------------------------------------------

def set_response(date: str, user_id: str, grp: Optional[str], item: Optional[str],
                 status: str = "in", auto: bool = False) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO responses(date, user_id, grp, item, status, auto, updated_at) "
            "VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(date, user_id) DO UPDATE SET "
            "  grp=excluded.grp, item=excluded.item, status=excluded.status, "
            "  auto=excluded.auto, updated_at=excluded.updated_at",
            (date, user_id, grp, item, status, int(auto), _now_iso()),
        )


def get_response(date: str, user_id: str) -> Optional[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM responses WHERE date=? AND user_id=?", (date, user_id)
        ).fetchone()


def responses_for(date: str) -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute("SELECT * FROM responses WHERE date=?", (date,)).fetchall()


def last_response_before(user_id: str, date: str) -> Optional[sqlite3.Row]:
    """Most recent non-auto 'in' response a user made before `date` — used for the
    'last' no-response default."""
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM responses WHERE user_id=? AND date<? AND status='in' AND auto=0 "
            "ORDER BY date DESC LIMIT 1",
            (user_id, date),
        ).fetchone()
