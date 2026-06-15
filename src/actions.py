"""Button + select handlers for the live poll.

A click sets or swaps the clicker's group; the item selects attach a chosen item.
All mutations target *today's* open session and re-render the poll in place. If a
non-member clicks, they're auto-enrolled for the day (so guests/new hires aren't
locked out) but not added as a permanent member.
"""
from __future__ import annotations

from slack_bolt import Ack
from slack_sdk import WebClient

from . import db, poll, timeutil


def _today_open() -> str | None:
    today = timeutil.today().isoformat()
    s = db.get_session(today)
    if s and s["status"] == "open":
        return today
    return None


def register(app) -> None:

    def _choose(group: str | None, status: str):
        def handler(ack: Ack, body: dict, client: WebClient):
            ack()
            date_str = _today_open()
            user = body["user"]["id"]
            if not date_str:
                client.chat_postEphemeral(
                    channel=body["channel"]["id"], user=user,
                    text="That poll is closed — talk to an admin for late changes.")
                return
            # Dishes are chosen in DoorDash via the cart link, not in Slack, so
            # we only track the group/status here.
            db.set_response(date_str, user, group, None, status=status, auto=False)
            poll.refresh_poll(client, date_str)
            # If that was the last person, tell the orderer they can order now.
            poll.maybe_notify_orderer(client, date_str)
        return handler

    app.action("choose_veg")(_choose("veg", "in"))
    app.action("choose_nonveg")(_choose("nonveg", "in"))
    app.action("choose_out")(_choose(None, "out"))

    # URL buttons (open cart) and the "pending link" placeholder need acks so
    # Slack doesn't show a warning, but require no state change.
    @app.action("pending_veg")
    @app.action("pending_nonveg")
    def _pending(ack: Ack, body: dict, client: WebClient):
        ack()
        client.chat_postEphemeral(
            channel=body["channel"]["id"], user=body["user"]["id"],
            text="⏳ The group-order link isn't posted yet — an admin will drop it shortly.")

    @app.action("open_veg_cart")
    @app.action("open_nonveg_cart")
    def _open_cart(ack: Ack):
        ack()  # URL buttons open the link client-side; just acknowledge.
