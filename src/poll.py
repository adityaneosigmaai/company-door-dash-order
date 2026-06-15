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
    """The two restaurants for the date (or None each if unset)."""
    m = menu.menu_for_date(target)
    veg = m["veg"] if m else None
    nonveg = m["nonveg"] if m else None
    return (
        veg["restaurant"] if veg else None,
        nonveg["restaurant"] if nonveg else None,
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

    veg_r, nonveg_r = _menu_bits(target)
    if not veg_r and not nonveg_r:
        return None  # nothing on the menu — caller alerts admin

    arrival = db.get_setting("arrival_time") or "12:00"
    db.create_session(date_str, channel, arrival, veg_r or "", nonveg_r or "")

    resp = client.chat_postMessage(
        channel=channel,
        text=f"Lunch order for {date_str} — pick your group!",  # fallback for notifications
        blocks=blocks.poll_blocks(
            date_str=date_str, arrival_time=arrival,
            veg_restaurant=veg_r, nonveg_restaurant=nonveg_r,
            veg_url=None, nonveg_url=None,
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
    client.chat_update(
        channel=s["channel_id"], ts=s["message_ts"],
        text=f"Lunch order for {date_str}",
        blocks=blocks.poll_blocks(
            date_str=date_str, arrival_time=s["arrival_time"],
            veg_restaurant=s["veg_restaurant"], nonveg_restaurant=s["nonveg_restaurant"],
            veg_url=s["veg_url"], nonveg_url=s["nonveg_url"],
            tally=tally(date_str),
        ),
    )


# ---- reminders ------------------------------------------------------------

def non_responders(date_str: str) -> list[str]:
    responded = {r["user_id"] for r in db.responses_for(date_str)}
    return [m["user_id"] for m in db.active_members() if m["user_id"] not in responded]


def all_responded(date_str: str) -> bool:
    """True once every active member has a response for the day (in or out)."""
    members = db.active_members()
    if not members:
        return False
    responded = {r["user_id"] for r in db.responses_for(date_str)}
    return all(m["user_id"] in responded for m in members)


def _orderer_mention() -> str:
    """Who to tag to place the order — the configured orderer, else all admins."""
    orderer = db.get_setting("orderer") or ""
    if orderer:
        return f"<@{orderer}>"
    admins = [a for a in (db.get_setting("admins") or "").split(",") if a]
    return " ".join(f"<@{a}>" for a in admins) or "@here"


def maybe_notify_orderer(client: WebClient, date_str: str) -> bool:
    """Ping the orderer that everyone's responded so they can place the order now.
    Fires at most once per day (orderer_notified flag). Returns True if it pinged."""
    s = db.get_session(date_str)
    if not s or s["status"] == "skipped" or s["orderer_notified"]:
        return False
    if not all_responded(date_str):
        return False

    t = tally(date_str)
    tag = _orderer_mention()
    if not t["veg"] and not t["nonveg"]:
        text = f"📣 {tag} — everyone's responded and *everyone's out today*. No lunch order needed. 🎉"
    else:
        lines = [f"📣 {tag} — *everyone's responded, you're clear to place the orders!*"]
        if t["veg"]:
            link = f" — <{s['veg_url']}|cart>" if s["veg_url"] else " — _link pending_"
            lines.append(f"🥗 *{s['veg_restaurant'] or 'Veg'}*: {len(t['veg'])} eating{link}")
        if t["nonveg"]:
            link = f" — <{s['nonveg_url']}|cart>" if s["nonveg_url"] else " — _link pending_"
            lines.append(f"🍗 *{s['nonveg_restaurant'] or 'Non-veg'}*: {len(t['nonveg'])} eating{link}")
        lines.append(f"Set both delivery times to *{s['arrival_time']}* so it all lands together.")
        text = "\n".join(lines)

    client.chat_postMessage(
        channel=s["channel_id"], thread_ts=s["message_ts"] or None,
        reply_broadcast=bool(s["message_ts"]), text=text,
    )
    db.update_session(date_str, orderer_notified=1)
    return True


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


def announce_arrival(client: WebClient, date_str: str) -> bool:
    """Ping the channel that food has arrived, tagging everyone in each group.

    Returns False (and posts nothing) if there's no real order to announce — no
    session, a skipped day, or nobody in. The bot can't detect actual delivery
    (no DoorDash API), so this fires on the scheduled arrival time or on demand
    via `/lunch arrived`. Idempotent-ish: safe to call again, it just re-pings.
    """
    s = db.get_session(date_str)
    if not s or s["status"] == "skipped" or not s["channel_id"]:
        return False
    t = tally(date_str)
    if not t["veg"] and not t["nonveg"]:
        return False

    lines = [f"🍱 *Lunch has arrived — come grab it!* (it's {s['arrival_time']})"]
    if t["veg"]:
        who = " ".join(f"<@{r['user_id']}>" for r in t["veg"])
        lines.append(f"🥗 *{s['veg_restaurant'] or 'Veg'}*: {who}")
    if t["nonveg"]:
        who = " ".join(f"<@{r['user_id']}>" for r in t["nonveg"])
        lines.append(f"🍗 *{s['nonveg_restaurant'] or 'Non-veg'}*: {who}")
    lines.append("Both orders are in — see you in the kitchen 🎉")

    client.chat_postMessage(
        channel=s["channel_id"],
        thread_ts=s["message_ts"] or None,
        reply_broadcast=bool(s["message_ts"]),  # show in-channel even though threaded
        text="\n".join(lines),
    )
    return True


def close_poll(client: WebClient, date_str: str) -> None:
    """Apply defaults, post the consolidated summary, mark closed, and freeze the poll."""
    s = db.get_session(date_str)
    if not s or s["status"] != "open":
        return
    apply_no_response_defaults(date_str)
    # Everyone now has a response — make sure the orderer got their go-ahead ping
    # (no-op if they were already pinged when the last person responded early).
    maybe_notify_orderer(client, date_str)
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
