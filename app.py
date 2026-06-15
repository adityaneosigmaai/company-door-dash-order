"""Entrypoint: wire Slack Bolt (Socket Mode) + scheduler, then run forever.

    python app.py

Requires SLACK_BOT_TOKEN (xoxb-) and SLACK_APP_TOKEN (xapp-) in the environment
(see .env.example). Socket Mode means no public URL / ngrok is needed.
"""
from __future__ import annotations

import logging
import os

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from src import actions, commands, db
from src.config import SlackConfig
from src.scheduler import LunchScheduler

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("lunchbot")


def main() -> None:
    # Load .env if python-dotenv is available (optional convenience).
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    cfg = SlackConfig.from_env()
    db.init_db()

    app = App(token=cfg.bot_token)
    scheduler = LunchScheduler(app.client)

    commands.register(app, scheduler_reload=scheduler.reload)
    actions.register(app)

    scheduler.start()
    log.info("Lunch bot starting (Socket Mode). Poll channel: %s",
             db.get_setting("channel_id") or "<not set — run /lunch channel>")

    SocketModeHandler(app, cfg.app_token).start()


if __name__ == "__main__":
    main()
