"""Daily cron: open the poll, send a reminder, close & summarize.

Times and timezone come from settings, so `reload()` is called whenever an admin
changes them via `/lunch config`. On startup we also reconcile any poll that was
left open past its cutoff while the bot was down.
"""
from __future__ import annotations

from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from slack_sdk import WebClient

from . import db, poll, timeutil


class LunchScheduler:
    def __init__(self, client: WebClient):
        self.client = client
        self.sched = BackgroundScheduler(timezone=str(timeutil.tz()))

    def start(self) -> None:
        self.sched.start()
        self.reload()
        self.reconcile_on_startup()

    def reload(self) -> None:
        """Re-create the three daily jobs from current settings."""
        for job_id in ("open", "remind", "close", "arrived"):
            j = self.sched.get_job(job_id)
            if j:
                j.remove()
        self.sched.configure(timezone=str(timeutil.tz()))
        ph, pm = timeutil.parse_hhmm(db.get_setting("poll_time") or "10:00")
        rh, rm = timeutil.parse_hhmm(db.get_setting("reminder_time") or "10:30")
        ch, cm = timeutil.parse_hhmm(db.get_setting("cutoff_time") or "11:00")
        ah, am = timeutil.parse_hhmm(db.get_setting("arrival_time") or "12:00")
        tz = str(timeutil.tz())
        self.sched.add_job(self._open, CronTrigger(hour=ph, minute=pm, timezone=tz),
                           id="open", replace_existing=True)
        self.sched.add_job(self._remind, CronTrigger(hour=rh, minute=rm, timezone=tz),
                           id="remind", replace_existing=True)
        self.sched.add_job(self._close, CronTrigger(hour=ch, minute=cm, timezone=tz),
                           id="close", replace_existing=True)
        self.sched.add_job(self._arrived, CronTrigger(hour=ah, minute=am, timezone=tz),
                           id="arrived", replace_existing=True)

    # --- jobs --------------------------------------------------------------

    def _open(self) -> None:
        today = timeutil.today()
        ts = poll.open_poll(self.client, today)
        if ts is None and self._should_have_opened(today):
            self._alert_admin(f"⚠️ Couldn't open today's lunch poll ({today}). "
                              f"Check `/lunch setup` — likely no menu or no channel set.")

    def _remind(self) -> None:
        poll.send_reminder(self.client, timeutil.today().isoformat())

    def _close(self) -> None:
        poll.close_poll(self.client, timeutil.today().isoformat())

    def _arrived(self) -> None:
        poll.announce_arrival(self.client, timeutil.today().isoformat())

    # --- helpers -----------------------------------------------------------

    def _should_have_opened(self, today) -> bool:
        if (db.get_setting("skip_weekends") or "true").lower() == "true" and today.weekday() >= 5:
            return False
        return bool(db.get_setting("channel_id"))

    def _alert_admin(self, msg: str) -> None:
        admins = [a for a in (db.get_setting("admins") or "").split(",") if a]
        for uid in admins:
            try:
                self.client.chat_postMessage(channel=uid, text=msg)
            except Exception:
                pass

    def reconcile_on_startup(self) -> None:
        """If the bot was down past a poll's cutoff, close it now so it doesn't
        hang open forever."""
        ch, cm = timeutil.parse_hhmm(db.get_setting("cutoff_time") or "11:30")
        now = timeutil.now()
        for s in db.open_sessions():
            try:
                d = datetime.fromisoformat(s["date"]).date()
            except ValueError:
                continue
            past_today_cutoff = (d < now.date()) or (
                d == now.date() and (now.hour, now.minute) >= (ch, cm))
            if past_today_cutoff:
                poll.close_poll(self.client, s["date"])
