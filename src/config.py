"""Environment + runtime configuration.

All Slack tokens come from the environment. Operational settings (poll time,
cutoff, arrival time, channel, timezone, etc.) live in the DB `settings` table
so they can be changed at runtime via `/lunch config` without a redeploy.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _require(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        raise RuntimeError(
            f"Missing required env var {name}. Copy .env.example to .env and fill it in."
        )
    return val


@dataclass(frozen=True)
class SlackConfig:
    bot_token: str       # xoxb-...
    app_token: str       # xapp-...  (Socket Mode)

    @classmethod
    def from_env(cls) -> "SlackConfig":
        return cls(
            bot_token=_require("SLACK_BOT_TOKEN"),
            app_token=_require("SLACK_APP_TOKEN"),
        )


# Default operational settings, seeded into the DB on first run. Each can be
# overridden live with `/lunch config <key> <value>`.
DEFAULT_SETTINGS = {
    "timezone": os.environ.get("TZ", "America/Los_Angeles"),
    "channel_id": "",            # set via `/lunch channel` (the channel the poll posts to)
    "poll_time": "10:00",        # when the daily poll opens (HH:MM, local tz)
    "reminder_time": "10:30",    # when non-responders get pinged
    "cutoff_time": "11:00",      # when the poll closes and the summary posts
    "arrival_time": "12:00",     # the common delivery time everyone orders for
    "skip_weekends": "true",     # don't run Sat/Sun
    "no_response_action": "out", # global default for non-responders: out | last
    "orderer": "",               # user who places the order; pinged when everyone's responded
    "anchor_monday": "",         # ISO date of week 0 for rotation; auto-set on first menu
}

DB_PATH = os.environ.get("LUNCHBOT_DB", os.path.join(os.path.dirname(__file__), "..", "lunchbot.db"))
