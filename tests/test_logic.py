"""Unit tests for the logic that has no Slack dependency: menu rotation, tally,
and no-response defaults. Run with: pytest -q

Each test uses a fresh temp DB via the LUNCHBOT_DB env var + importlib reload so
the config picks up the path before db connects.
"""
import importlib
import os
from datetime import date

import pytest


@pytest.fixture()
def mod(tmp_path):
    os.environ["LUNCHBOT_DB"] = str(tmp_path / "test.db")
    import src.config as config
    importlib.reload(config)
    import src.db as db
    importlib.reload(db)
    import src.menu as menu
    importlib.reload(menu)
    import src.poll as poll
    importlib.reload(poll)
    db.init_db()
    return {"db": db, "menu": menu, "poll": poll}


def test_rotation_cycles_over_defined_weeks(mod):
    db, menu = mod["db"], mod["menu"]
    # Two rotating weeks defined.
    db.set_menu(0, 0, "veg", "Sweetgreen", ["Harvest Bowl"])
    db.set_menu(1, 0, "veg", "Cava", ["Greens Bowl"])
    db.set_setting("anchor_monday", "2026-06-01")  # a Monday
    assert db.num_weeks() == 2
    # Week of anchor -> index 0; next week -> 1; week after -> back to 0.
    assert menu.active_week_index(date(2026, 6, 1)) == 0
    assert menu.active_week_index(date(2026, 6, 8)) == 1
    assert menu.active_week_index(date(2026, 6, 15)) == 0


def test_weeks_since_anchor_handles_nonmonday_anchor(mod):
    menu = mod["menu"]
    # Anchor given as a Wednesday should still align to its Monday.
    assert menu.weeks_since_anchor(date(2026, 6, 3), date(2026, 6, 10)) == 1


def test_menu_for_date_returns_both_groups(mod):
    db, menu = mod["db"], mod["menu"]
    db.set_menu(0, 2, "veg", "Cava", ["Bowl"])      # Wed veg
    db.set_menu(0, 2, "nonveg", "Chipotle", ["Burrito"])
    db.set_setting("anchor_monday", "2026-06-15")
    m = menu.menu_for_date(date(2026, 6, 17))        # Wed
    assert m["veg"]["restaurant"] == "Cava"
    assert m["nonveg"]["restaurant"] == "Chipotle"
    # A day with no menu entries returns None for those groups but a dict overall.
    m2 = menu.menu_for_date(date(2026, 6, 18))       # Thu — nothing set
    assert m2["veg"] is None and m2["nonveg"] is None


def test_tally_groups_responses(mod):
    db, poll = mod["db"], mod["poll"]
    d = "2026-06-15"
    db.set_response(d, "U1", "veg", "Bowl", "in")
    db.set_response(d, "U2", "nonveg", "Burrito", "in")
    db.set_response(d, "U3", None, None, "out")
    t = poll.tally(d)
    assert [r["user_id"] for r in t["veg"]] == ["U1"]
    assert [r["user_id"] for r in t["nonveg"]] == ["U2"]
    assert [r["user_id"] for r in t["out"]] == ["U3"]


def test_no_response_default_out(mod):
    db, poll = mod["db"], mod["poll"]
    db.upsert_member("U1", default_group="veg")
    db.upsert_member("U2", default_group="nonveg")
    db.set_setting("no_response_action", "out")
    d = "2026-06-15"
    db.set_response(d, "U1", "veg", "Bowl", "in")  # U1 responded; U2 did not
    poll.apply_no_response_defaults(d)
    r2 = db.get_response(d, "U2")
    assert r2["status"] == "out" and r2["auto"] == 1
    # Responder is untouched.
    assert db.get_response(d, "U1")["auto"] == 0


def test_no_response_default_last_uses_history(mod):
    db, poll = mod["db"], mod["poll"]
    db.upsert_member("U1", default_group="either", no_response_action="last")
    db.set_response("2026-06-12", "U1", "nonveg", "Burrito", "in")  # prior real choice
    poll.apply_no_response_defaults("2026-06-15")
    r = db.get_response("2026-06-15", "U1")
    assert r["status"] == "in" and r["grp"] == "nonveg" and r["item"] == "Burrito"
    assert r["auto"] == 1


def test_last_falls_back_to_out_without_history(mod):
    db, poll = mod["db"], mod["poll"]
    db.upsert_member("U1", default_group="either", no_response_action="last")
    poll.apply_no_response_defaults("2026-06-15")  # no prior responses at all
    r = db.get_response("2026-06-15", "U1")
    assert r["status"] == "out"


def test_inactive_member_excluded_from_defaults(mod):
    db, poll = mod["db"], mod["poll"]
    db.upsert_member("U1", default_group="veg")
    db.set_member_active("U1", False)
    poll.apply_no_response_defaults("2026-06-15")
    assert db.get_response("2026-06-15", "U1") is None
