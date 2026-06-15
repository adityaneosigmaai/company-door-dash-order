"""`/lunch` slash-command router (admin control surface).

Usage is parsed from the raw text after the command. Admin-only commands are
gated by an allowlist stored in settings (`admins`). The first person to run any
command bootstraps themselves as admin so setup isn't a chicken-and-egg problem.
"""
from __future__ import annotations

import re
from datetime import date

from slack_bolt import Ack, Respond
from slack_sdk import WebClient

from . import db, menu, poll, timeutil

USER_RE = re.compile(r"<@([A-Z0-9]+)(?:\|[^>]+)?>")
CHANNEL_RE = re.compile(r"<#([A-Z0-9]+)(?:\|[^>]+)?>")

HELP = """*🍱 Lunch bot commands*
`/lunch setup` — interactive first-time setup checklist
`/lunch channel` — set _this_ channel as the poll channel
`/lunch member add @user [veg|nonveg|either]` — add a participant
`/lunch member remove @user` · `/lunch member list`
`/lunch member default @user veg|nonveg|either`
`/lunch menu set <week#> <mon..sun> <veg|nonveg> <Restaurant> :: item1, item2`
`/lunch menu show` · `/lunch menu clear`
`/lunch link veg <url>` · `/lunch link nonveg <url>` — paste DoorDash group-order links
`/lunch poll open|close|status` — run the poll manually
`/lunch arrived` — ping the channel now that food has arrived
`/lunch config [key] [value]` — view/change settings (poll_time, cutoff_time, arrival_time, reminder_time, timezone, skip_weekends, no_response_action)
`/lunch arrival HH:MM` — shortcut for the common delivery time
`/lunch admin add @user|list`"""


def _is_admin(user_id: str) -> bool:
    admins = (db.get_setting("admins") or "").split(",")
    admins = [a for a in admins if a]
    if not admins:  # bootstrap: first caller becomes admin
        db.set_setting("admins", user_id)
        return True
    return user_id in admins


def _first_user(text: str):
    m = USER_RE.search(text)
    return m.group(1) if m else None


def register(app, scheduler_reload) -> None:
    """Wire the /lunch command. `scheduler_reload` re-reads cron times when they change."""

    @app.command("/lunch")
    def handle(ack: Ack, command: dict, respond: Respond, client: WebClient):
        ack()
        text = (command.get("text") or "").strip()
        user = command["user_id"]
        parts = text.split()
        sub = parts[0].lower() if parts else "help"

        if sub in ("", "help"):
            return respond(HELP)

        if not _is_admin(user):
            return respond("⛔ Admin only. Ask an existing admin to add you with `/lunch admin add @you`.")

        try:
            _dispatch(sub, parts, text, command, respond, client, scheduler_reload)
        except Exception as e:  # surface errors to the admin instead of failing silently
            respond(f"⚠️ `{sub}` failed: {e}")


def _dispatch(sub, parts, text, command, respond, client, scheduler_reload):
    if sub == "setup":
        return respond(_setup_status())

    if sub == "channel":
        db.set_setting("channel_id", command["channel_id"])
        return respond(f"✅ Poll channel set to <#{command['channel_id']}>.")

    if sub == "admin":
        action = parts[1] if len(parts) > 1 else "list"
        if action == "list":
            admins = [a for a in (db.get_setting("admins") or "").split(",") if a]
            return respond("Admins: " + (", ".join(f"<@{a}>" for a in admins) or "—"))
        if action == "add":
            uid = _first_user(text)
            if not uid:
                return respond("Usage: `/lunch admin add @user`")
            admins = [a for a in (db.get_setting("admins") or "").split(",") if a]
            if uid not in admins:
                admins.append(uid)
            db.set_setting("admins", ",".join(admins))
            return respond(f"✅ <@{uid}> is now an admin.")

    if sub == "member":
        return _member(parts, text, respond)

    if sub == "menu":
        return _menu_cmd(parts, text, respond)

    if sub == "link":
        return _link(parts, respond, client)

    if sub == "arrival":
        if len(parts) < 2:
            return respond("Usage: `/lunch arrival 12:00`")
        db.set_setting("arrival_time", parts[1])
        scheduler_reload()  # the arrival ping is scheduled, so reschedule it
        return respond(f"✅ Arrival time set to {parts[1]} for both groups "
                       f"(channel gets pinged then).")

    if sub == "config":
        return _config(parts, respond, scheduler_reload)

    if sub == "poll":
        return _poll(parts, respond, client)

    if sub == "arrived":
        today = timeutil.today().isoformat()
        if poll.announce_arrival(client, today):
            return respond("📣 Pinged the channel that food's here.")
        return respond("Nothing to announce — no open/closed order with people in it today.")

    return respond(f"Unknown subcommand `{sub}`. Try `/lunch help`.")


def _setup_status() -> str:
    s = db.all_settings()
    checks = [
        ("Poll channel", bool(s.get("channel_id"))),
        ("At least one menu week", db.num_weeks() > 0),
        ("Active members", len(db.active_members()) > 0),
        ("Timezone", bool(s.get("timezone"))),
    ]
    lines = ["*Setup checklist*"]
    for name, ok in checks:
        lines.append(f"{'✅' if ok else '⬜'} {name}")
    lines.append("\nRun `/lunch help` for all commands. The poll opens daily at "
                 f"{s.get('poll_time')} and closes at {s.get('cutoff_time')} "
                 f"({s.get('timezone')}).")
    return "\n".join(lines)


def _member(parts, text, respond):
    action = parts[1] if len(parts) > 1 else "list"
    if action == "list":
        rows = db.active_members()
        if not rows:
            return respond("No members yet. Add with `/lunch member add @user veg|nonveg|either`.")
        out = "\n".join(
            f"• <@{r['user_id']}> — default: {r['default_group']}"
            + (f", no-reply: {r['no_response_action']}" if r['no_response_action'] else "")
            for r in rows)
        return respond("*Members*\n" + out)
    uid = _first_user(text)
    if not uid:
        return respond("Usage: `/lunch member add @user [veg|nonveg|either]`")
    if action == "add":
        grp = next((p for p in parts if p in ("veg", "nonveg", "either")), "either")
        db.upsert_member(uid, default_group=grp, active=True)
        return respond(f"✅ Added <@{uid}> (default {grp}).")
    if action == "remove":
        db.set_member_active(uid, False)
        return respond(f"✅ Removed <@{uid}> from the daily poll.")
    if action == "default":
        grp = next((p for p in parts if p in ("veg", "nonveg", "either")), None)
        if not grp:
            return respond("Usage: `/lunch member default @user veg|nonveg|either`")
        existing = db.get_member(uid)
        db.upsert_member(uid, default_group=grp,
                         no_response_action=existing["no_response_action"] if existing else None,
                         active=True)
        return respond(f"✅ <@{uid}> default group → {grp}.")
    return respond("Usage: `/lunch member add|remove|default|list ...`")


def _menu_cmd(parts, text, respond):
    action = parts[1] if len(parts) > 1 else "show"
    if action == "show":
        rows = db.all_menu()
        if not rows:
            return respond("No menu defined. Set one with "
                           "`/lunch menu set 0 mon veg Sweetgreen :: Harvest Bowl, Kale Caesar`.")
        wk = menu.active_week_index(timeutil.today())
        lines = [f"*Menu* (rotating over {db.num_weeks()} week(s); active week now: {wk})"]
        for r in rows:
            opts = ", ".join(__import__("json").loads(r["options"])) or "_no items_"
            lines.append(f"• W{r['week_index']} {menu.WEEKDAY_NAMES[r['weekday']]} "
                         f"{r['grp']}: *{r['restaurant']}* — {opts}")
        return respond("\n".join(lines))
    if action == "clear":
        with db.connect() as conn:
            conn.execute("DELETE FROM menu")
        db.set_setting("anchor_monday", "")
        return respond("✅ Menu cleared.")
    if action == "set":
        # /lunch menu set <week#> <weekday> <veg|nonveg> <Restaurant words> :: item, item
        body = text.split(None, 2)[2] if len(text.split(None, 2)) > 2 else ""
        m = re.match(r"\s*(\d+)\s+(\w+)\s+(veg|nonveg)\s+(.+)", body, re.IGNORECASE)
        if not m:
            return respond("Usage: `/lunch menu set <week#> <mon..sun> <veg|nonveg> "
                           "<Restaurant> :: item1, item2`")
        week = int(m.group(1))
        weekday = menu.parse_weekday(m.group(2))
        if weekday is None:
            return respond("Weekday must be one of mon tue wed thu fri sat sun.")
        grp = m.group(3).lower()
        rest_and_items = m.group(4)
        if "::" in rest_and_items:
            restaurant, items_raw = rest_and_items.split("::", 1)
            options = [i.strip() for i in items_raw.split(",") if i.strip()]
        else:
            restaurant, options = rest_and_items, []
        menu.ensure_anchor(timeutil.today())
        db.set_menu(week, weekday, grp, restaurant.strip(), options)
        return respond(f"✅ W{week} {menu.WEEKDAY_NAMES[weekday]} {grp}: "
                       f"*{restaurant.strip()}* ({len(options)} item(s)).")
    return respond("Usage: `/lunch menu set|show|clear ...`")


def _link(parts, respond, client):
    if len(parts) < 3 or parts[1] not in ("veg", "nonveg"):
        return respond("Usage: `/lunch link veg <url>` or `/lunch link nonveg <url>`")
    grp, url = parts[1], parts[2].strip("<>")  # Slack may auto-link the URL
    today = timeutil.today().isoformat()
    s = db.get_session(today)
    if not s:
        return respond("No poll open today yet. Open it with `/lunch poll open` first.")
    db.update_session(today, **{f"{grp}_url": url})
    poll.refresh_poll(client, today)
    return respond(f"✅ {grp} group-order link saved and added to today's poll.")


def _config(parts, respond, scheduler_reload):
    if len(parts) == 1:
        s = db.all_settings()
        hidden = {"admins"}
        return respond("*Config*\n" + "\n".join(
            f"• `{k}` = {v}" for k, v in sorted(s.items()) if k not in hidden))
    if len(parts) < 3:
        return respond("Usage: `/lunch config <key> <value>`")
    key, value = parts[1], " ".join(parts[2:])
    if key not in db.all_settings():
        return respond(f"Unknown setting `{key}`. Run `/lunch config` to list them.")
    db.set_setting(key, value)
    if key in ("poll_time", "reminder_time", "cutoff_time", "arrival_time", "timezone"):
        scheduler_reload()
    return respond(f"✅ `{key}` = {value}")


def _poll(parts, respond, client):
    action = parts[1] if len(parts) > 1 else "status"
    today = timeutil.today()
    today_str = today.isoformat()
    if action == "open":
        ts = poll.open_poll(client, today)
        if ts:
            return respond("✅ Poll opened.")
        # Diagnose why it didn't open.
        if not db.get_setting("channel_id"):
            return respond("⚠️ No channel set. Run `/lunch channel` in your lunch channel.")
        if menu.menu_for_date(today) is None or not any(
                [menu.menu_for_date(today)["veg"], menu.menu_for_date(today)["nonveg"]]):
            return respond("⚠️ No menu defined for today. Add one with `/lunch menu set ...`.")
        return respond("⚠️ Couldn't open (weekend skip, or poll already open).")
    if action == "close":
        poll.close_poll(client, today_str)
        return respond("✅ Poll closed and summary posted.")
    if action == "status":
        s = db.get_session(today_str)
        if not s:
            return respond("No poll for today yet.")
        t = poll.tally(today_str)
        return respond(f"Status: *{s['status']}* — veg {len(t['veg'])}, "
                       f"non-veg {len(t['nonveg'])}, out {len(t['out'])}. "
                       f"Non-responders: {len(poll.non_responders(today_str))}.")
    return respond("Usage: `/lunch poll open|close|status`")
