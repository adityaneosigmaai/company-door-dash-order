"""Poll lifecycle: open, refresh, remind, close.

A "session" is one day's poll. Opening posts the interactive message; clicks
mutate `responses`; closing applies no-response defaults, posts a summary, and
disables further changes. Everything is idempotent so the scheduler can re-run
safely after a restart.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from slack_sdk import WebClient

from . import blocks, db, menu
from . import timeutil


# ---- tally ----------------------------------------------------------------

def tally(date_str: str) -> dict:
    """Group today's responses into {'veg': [...], 'nonveg': [...], 'out': [...]}."""
    out = {"veg": [], "nonveg": [], "out": []}
    for r in db.responses_for(date_str):
        if r["status"] == "out":
            out["out"].append(r)
        elif r["grp"] in ("veg", "nonveg"):
            out[r["grp"]].append(r)
    return out


def _menu_bits(target: date):
    m = menu.menu_for_date(target)
    veg = m["veg"] if m else None
    nonveg = m["nonveg"] if m else None
    return (
        veg["restaurant"] if veg else None,
        nonveg["restaurant"] if nonveg else None,
        veg["options"] if veg else [],
        nonveg["options"] if nonveg else [],
    )


# ---- open -----------------------------------------------------------------

def open_poll(client: WebClient, target: date) -> Optional[str]:
    """Post the daily poll. Returns the message ts, or None if it couldn't open.

    Edge cases handled:
      - no channel configured        -> abort, caller should alert
      - weekend + skip_weekends       -> mark session skipped
      - no menu defined for today     -> abort (caller alerts admin)
      - duplicate open (rerun/restart)-> reuse the existing open session
    """
    date_str = target.isoformat()
    channel = db.get_setting("channel_id") or ""
    if not channel:
        return None

    if (db.get_setting("skip_weekends") or "true").lower() == "true" and target.weekday() >= 5:
        db.create_session(date_str, channel, db.get_setting("arrival_time") or "", "", "")
        db.update_session(date_str, status="skipped")
        return None

    existing = db.get_session(date_str)
    if existing and existing["status"] != "skipped" and existing["message_ts"]:
        return existing["message_ts"]  # already open — don't double-post

    veg_r, nonveg_r, veg_o, nonveg_o = _menu_bits(target)
    if not veg_r and not nonveg_r:
        return None  # nothing on the menu — caller alerts admin

    arrival = db.get_setting("arrival_time") or "12:30"
    db.create_session(date_str, channel, arrival, veg_r or "", nonveg_r or "")

    resp = client.chat_postMessage(
        channel=channel,
        text=f"Lunch order for {date_str} — pick your group!",  # fallback for notifications
        blocks=blocks.poll_blocks(
            date_str=date_str, arrival_time=arrival,
            veg_restaurant=veg_r, nonveg_restaurant=nonveg_r,
            veg_url=None, nonveg_url=None,
            veg_options=veg_o, nonveg_options=nonveg_o,
            tally=tally(date_str),
        ),
    )
    db.update_session(date_str, message_ts=resp["ts"], channel_id=resp["channel"])
    return resp["ts"]


def refresh_poll(client: WebClient, date_str: str) -> None:
    """Re-render the open poll in place (after a click or link update)."""
    s = db.get_session(date_str)
    if not s or s["status"] != "open" or not s["message_ts"]:
        return
    target = date.fromisoformat(date_str)
    _, _, veg_o, nonveg_o = _menu_bits(target)
    client.chat_update(
        channel=s["channel_id"], ts=s["message_ts"],
        text=f"Lunch order for {date_str}",
        blocks=blocks.poll_blocks(
            date_str=date_str, arrival_time=s["arrival_time"],
            veg_restaurant=s["veg_restaurant"], nonveg_restaurant=s["nonveg_restaurant"],
            veg_url=s["veg_url"], nonveg_url=s["nonveg_url"],
            veg_options=veg_o, nonveg_options=nonveg_o,
            tally=tally(date_str),
        ),
    )


# ---- reminders ------------------------------------------------------------

def non_responders(date_str: str) -> list[str]:
    responded = {r["user_id"] for r in db.responses_for(date_str)}
    return [m["user_id"] for m in db.active_members() if m["user_id"] not in responded]


def send_reminder(client: WebClient, date_str: str) -> None:
    s = db.get_session(date_str)
    if not s or s["status"] != "open":
        return
    missing = non_responders(date_str)
    if not missing:
        return
    cutoff = db.get_setting("cutoff_time") or "11:30"
    mentions = " ".join(f"<@{u}>" for u in missing)
    client.chat_postMessage(
        channel=s["channel_id"], thread_ts=s["message_ts"],
        text=f"⏰ {mentions} — lunch poll closes at {cutoff}. Pick a group or you'll be marked per your default.",
    )


# ---- close ----------------------------------------------------------------

def apply_no_response_defaults(date_str: str) -> None:
    """Fill in everyone who never responded, per their (or the global) policy."""
    global_action = db.get_setting("no_response_action") or "out"
    responded = {r["user_id"] for r in db.responses_for(date_str)}
    for m in db.active_members():
        uid = m["user_id"]
        if uid in responded:
            continue
        action = m["no_response_action"] or global_action
        if action == "last":
            last = db.last_response_before(uid, date_str)
            if last and last["grp"]:
                db.set_response(date_str, uid, last["grp"], last["item"], status="in", auto=True)
                continue
            # fall through to 'out' if no usable history
        # 'out' (or 'last' with no history): respect a stored default group if not 'either'.
        if action == "out":
            db.set_response(date_str, uid, None, None, status="out", auto=True)
        else:
            grp = m["default_group"] if m["default_group"] in ("veg", "nonveg") else None
            db.set_response(date_str, uid, grp, None,
                            status="in" if grp else "out", auto=True)


def close_poll(client: WebClient, date_str: str) -> None:
    """Apply defaults, post the consolidated summary, mark closed, and freeze the poll."""
    s = db.get_session(date_str)
    if not s or s["status"] != "open":
        return
    apply_no_response_defaults(date_str)
    t = tally(date_str)

    client.chat_postMessage(
        channel=s["channel_id"], thread_ts=s["message_ts"],
        text=f"Lunch order closed for {date_str}.",
        blocks=blocks.summary_blocks(
            date_str=date_str, arrival_time=s["arrival_time"], tally=t,
            veg_restaurant=s["veg_restaurant"], nonveg_restaurant=s["nonveg_restaurant"],
            veg_url=s["veg_url"], nonveg_url=s["nonveg_url"],
        ),
    )
    db.update_session(date_str, status="closed")

    # Replace the live poll with a frozen note so nobody clicks a closed poll.
    if s["message_ts"]:
        client.chat_update(
            channel=s["channel_id"], ts=s["message_ts"],
            text=f"Lunch poll for {date_str} is closed.",
            blocks=[{"type": "section", "text": {"type": "mrkdwn",
                     "text": f"🔒 *Lunch poll for {date_str} is closed.* "
                             f"See the summary in thread."}}],
        )
