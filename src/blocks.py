"""Slack Block Kit builders for the daily poll and summary.

Action IDs encode the choice so a single handler can route clicks:
  choose_veg / choose_nonveg / choose_out  -> set/swap a person's group
The item picker is a static_select per group, shown once a person joins a group.
"""
from __future__ import annotations

from typing import Optional


def _link_or_pending(label: str, url: Optional[str]) -> dict:
    """A button that opens the group-order link, or a disabled-looking note if the
    admin hasn't dropped the link yet."""
    if url:
        return {
            "type": "button",
            "text": {"type": "plain_text", "text": f"🛒 {label} cart"},
            "url": url,
            "action_id": f"open_{label.lower()}_cart",  # url buttons still need a unique id
        }
    return {
        "type": "button",
        "text": {"type": "plain_text", "text": f"⏳ {label} link pending"},
        "action_id": f"pending_{label.lower()}",
        "value": "pending",
    }


def poll_blocks(*, date_str: str, arrival_time: str,
                veg_restaurant: Optional[str], nonveg_restaurant: Optional[str],
                veg_url: Optional[str], nonveg_url: Optional[str],
                tally: dict) -> list[dict]:
    """Build the interactive poll message. `tally` comes from poll.tally().

    People pick a group with one tap, then click their group's 🛒 DoorDash link
    to add their own dish in DoorDash — no dish selection happens in Slack."""
    veg_count = len(tally["veg"])
    nonveg_count = len(tally["nonveg"])
    out_count = len(tally["out"])

    veg_line = (f"🥗 *Veg* — {veg_restaurant}" if veg_restaurant
                else "🥗 *Veg* — _no restaurant set_")
    nonveg_line = (f"🍗 *Non-veg* — {nonveg_restaurant}" if nonveg_restaurant
                   else "🍗 *Non-veg* — _no restaurant set_")

    blocks: list[dict] = [
        {"type": "header",
         "text": {"type": "plain_text", "text": f"🍱 Lunch order — {date_str}"}},
        {"type": "section",
         "text": {"type": "mrkdwn",
                  "text": f"Pick your group, then tap your 🛒 cart link to add your "
                          f"order in DoorDash. All food arrives at *{arrival_time}* so we "
                          f"eat together. Tap again to swap, or 🙅 to opt out."}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"{veg_line}\n{nonveg_line}"}},
        {"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": f"🥗 Veg ({veg_count})"},
             "style": "primary", "action_id": "choose_veg", "value": "veg"},
            {"type": "button", "text": {"type": "plain_text", "text": f"🍗 Non-veg ({nonveg_count})"},
             "style": "primary", "action_id": "choose_nonveg", "value": "nonveg"},
            {"type": "button", "text": {"type": "plain_text", "text": f"🙅 Out ({out_count})"},
             "action_id": "choose_out", "value": "out"},
        ]},
    ]

    # DoorDash group-order links — this is where each person adds their dish.
    blocks.append({"type": "actions", "elements": [
        _link_or_pending("Veg", veg_url),
        _link_or_pending("Non-veg", nonveg_url),
    ]})

    # Live roster.
    blocks.append({"type": "context", "elements": [
        {"type": "mrkdwn", "text": _roster_line(tally)}
    ]})
    return blocks


def _roster_line(tally: dict) -> str:
    def names(rows):
        return ", ".join(_fmt(r) for r in rows) or "—"
    return (f"*Veg:* {names(tally['veg'])}   |   "
            f"*Non-veg:* {names(tally['nonveg'])}   |   "
            f"*Out:* {names(tally['out'])}")


def _fmt(r) -> str:
    mention = f"<@{r['user_id']}>"
    if r["auto"]:
        mention += " ·auto"
    return mention


def summary_blocks(*, date_str: str, arrival_time: str, tally: dict,
                   veg_restaurant: Optional[str], nonveg_restaurant: Optional[str],
                   veg_url: Optional[str], nonveg_url: Optional[str]) -> list[dict]:
    """The headcount summary posted at cutoff. Actual dishes live in the DoorDash
    group cart — this just confirms who's in each group + links the carts."""
    def group_section(label, emoji, rows, restaurant, url) -> str:
        if not rows:
            return f"{emoji} *{label}* — nobody today."
        who = ", ".join(f"<@{r['user_id']}>" for r in rows)
        lines = [f"{emoji} *{label}* — {restaurant or '?'} · *{len(rows)}* eating",
                 f"   {who}"]
        if url:
            lines.append(f"   🛒 <{url}|Open {label} group cart>")
        return "\n".join(lines)

    blocks = [
        {"type": "header",
         "text": {"type": "plain_text", "text": f"✅ Lunch is in — {date_str}"}},
        {"type": "section",
         "text": {"type": "mrkdwn",
                  "text": f"Poll closed. *Arrival: {arrival_time}* for both groups.\n\n"
                          + group_section("Veg", "🥗", tally["veg"], veg_restaurant, veg_url)}},
        {"type": "section",
         "text": {"type": "mrkdwn",
                  "text": group_section("Non-veg", "🍗", tally["nonveg"], nonveg_restaurant, nonveg_url)}},
    ]
    if tally["out"]:
        blocks.append({"type": "context", "elements": [
            {"type": "mrkdwn",
             "text": "🙅 Out: " + ", ".join(f"<@{r['user_id']}>" for r in tally["out"])}
        ]})
    blocks.append({"type": "context", "elements": [
        {"type": "mrkdwn", "text": "Set both delivery times to the arrival time above so everything lands together."}
    ]})
    return blocks
