# 🍱 Lunch Bot — daily DoorDash group orders in Slack

A Slack bot that runs your daily company lunch: it posts one poll, lets everyone
pick **🥗 Veg** or **🍗 Non-veg** (or opt out), rotates the restaurant menu every
week, nudges stragglers, and posts a single consolidated order for each group —
all timed to one **arrival time** so the company eats together.

## ⚠️ Important: what this bot can and can't do

**DoorDash has no public API for placing consumer orders.** There is no supported
way for any bot to create a cart or hit "place order" on your company account.
DoorDash's public APIs (Drive) are for merchant delivery logistics only.

So this bot does **everything around the order** and leaves the actual checkout to
a human:

| The bot does | A human does |
|---|---|
| Posts the daily poll, splits veg / non-veg | Creates the two DoorDash **Group Orders** in the DoorDash app |
| Collects choices, handles swaps & opt-outs | Pastes the two share links into Slack (`/lunch link …`) |
| Reminds non-responders, applies defaults | Each person adds their item to their group's cart |
| Tallies an itemized summary per group | An admin reviews and **places** each group order |
| Shows one arrival time for both groups | Sets both delivery times to that arrival time |

This is the only honest design — anything claiming to "auto-order from DoorDash"
relies on scraping/automating the logged-in site, which breaks constantly and
violates DoorDash's terms. We don't do that.

---

## Setup (≈10 minutes)

### 1. Create the Slack app
1. Go to <https://api.slack.com/apps> → **Create New App** → **From a manifest**.
2. Pick your workspace, paste the contents of [`slack-app-manifest.yaml`](slack-app-manifest.yaml), create.
3. **Basic Information → App-Level Tokens** → *Generate Token* with scope
   `connections:write`. Copy the `xapp-…` token → `SLACK_APP_TOKEN`.
4. **Install to Workspace**. Copy the **Bot User OAuth Token** (`xoxb-…`) →
   `SLACK_BOT_TOKEN`.
5. Invite the bot to your lunch channel: `/invite @lunchbot`.

### 2. Run it
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # paste your two tokens
python app.py
```
Socket Mode means **no public URL / ngrok** is needed — it works from a laptop or
any always-on host (a tiny VM, a `systemd` service, etc.).

### 3. Configure (in Slack)
Run these once. The **first person to use `/lunch` becomes the admin.**
```
/lunch channel                 # in your lunch channel — sets where the poll posts
/lunch member add @alice veg
/lunch member add @bob nonveg
/lunch member add @carol either
/lunch menu set 0 mon veg     Sweetgreen :: Harvest Bowl, Kale Caesar, Guacamole Greens
/lunch menu set 0 mon nonveg  Chipotle   :: Chicken Bowl, Steak Burrito, Carnitas Tacos
/lunch menu set 0 tue veg     Cava       :: Greens & Grains, Falafel Pita
/lunch menu set 0 tue nonveg  Halal Guys :: Chicken over Rice, Gyro Platter
# …repeat for each weekday. Add week 1, week 2… to rotate.
/lunch arrival 12:30
/lunch setup                   # shows a checklist of what's done
```

That's it. From then on the bot runs itself.

---

## Daily flow

1. **10:00** (`poll_time`) — bot posts the poll to your channel:
   veg/non-veg/out buttons, item pickers, and two "🛒 cart" buttons.
2. People click. Clicking a different group **swaps**; an item picker attaches
   their dish. The roster updates live.
3. **Admin** creates the two DoorDash Group Orders and pastes the links:
   `/lunch link veg https://… ` and `/lunch link nonveg https://…`. The cart
   buttons go live so everyone adds their own item.
4. **10:30** (`reminder_time`) — non-responders get pinged in-thread.
5. **11:00** (`cutoff_time`) — poll closes, defaults applied, and a consolidated
   **itemized summary** posts per group with the arrival time. The admin places
   the orders, setting both delivery times to the arrival time (**12:00**).
6. **12:00** (`arrival_time`) — the bot pings the channel that food's here,
   tagging everyone in each group. (No DoorDash API = no real delivery
   detection; this fires on schedule. Use `/lunch arrived` to ping early/late.)

---

## Command reference

| Command | What it does |
|---|---|
| `/lunch help` | List all commands |
| `/lunch setup` | First-time setup checklist |
| `/lunch channel` | Set the current channel as the poll channel |
| `/lunch member add @u [veg\|nonveg\|either]` | Add a participant |
| `/lunch member remove @u` / `list` | Manage members |
| `/lunch member default @u veg\|nonveg\|either` | Set someone's default group |
| `/lunch menu set <wk#> <mon..sun> <veg\|nonveg> <Restaurant> :: a, b, c` | Define a rotating menu slot |
| `/lunch menu show` / `clear` | View / reset the menu |
| `/lunch link veg\|nonveg <url>` | Attach today's DoorDash group-order link |
| `/lunch poll open\|close\|status` | Run the poll manually |
| `/lunch arrived` | Ping the channel that food has arrived |
| `/lunch arrival HH:MM` | Set the shared delivery time |
| `/lunch config [key] [value]` | View/change settings |
| `/lunch admin add @u\|list` | Manage admins |

**Settings** (`/lunch config`): `poll_time`, `reminder_time`, `cutoff_time`,
`arrival_time`, `timezone`, `skip_weekends` (true/false), `no_response_action`
(`out` or `last`).

---

## Menu rotation

The menu rotates by week. You define week `0`, `1`, `2`… Each week holds a
restaurant + items per weekday per group. The bot auto-picks the active week by
counting whole weeks since the first menu you created, modulo the number of weeks
you defined — so a 2-week menu alternates forever with zero upkeep. Define one
week and it simply repeats.

---

## Edge cases handled

- **No response** → per-person or global default: marked `out`, or repeat their
  `last` real choice (`no_response_action`).
- **Swaps** any time before cutoff; clicking re-renders the live roster.
- **Opt back in** after opting out — just click a group again.
- **Non-member clicks** (guest/new hire) → enrolled for that day only.
- **Weekends / holidays** → `skip_weekends`; the session is marked `skipped`.
- **No menu set for today** or **no channel** → poll won't open; admins get a DM.
- **Group-order link not posted yet** → cart shows "⏳ link pending"; goes live
  the moment an admin pastes it.
- **Empty group** (nobody veg) → that group is simply absent from the summary.
- **Bot restart** → all state is in SQLite; an open poll left past its cutoff is
  closed and summarized on startup (`reconcile_on_startup`).
- **Duplicate opens** (rerun/restart) → reuses the existing poll, never double-posts.
- **Closed poll clicks** → ephemeral "poll is closed" notice, no state change.

---

## Project layout
```
app.py                 entrypoint: Bolt (Socket Mode) + scheduler
src/config.py          env + default settings
src/db.py              SQLite layer (settings, members, menu, sessions, responses)
src/menu.py            weekly rotation logic
src/timeutil.py        timezone helpers
src/blocks.py          Block Kit builders (poll + summary)
src/poll.py            poll lifecycle: open / refresh / remind / close / tally
src/commands.py        /lunch admin command router
src/actions.py         button + select handlers
src/scheduler.py       daily cron jobs + startup reconcile
tests/test_logic.py    rotation, tally, no-response defaults
slack-app-manifest.yaml  one-paste Slack app config
```

## Tests
```bash
pip install pytest && pytest -q
```

## Deploying always-on
Any host that can run Python works. A minimal `systemd` unit:
```ini
[Unit]
Description=Lunch Bot
After=network-online.target

[Service]
WorkingDirectory=/opt/lunchbot
EnvironmentFile=/opt/lunchbot/.env
ExecStart=/opt/lunchbot/.venv/bin/python app.py
Restart=always

[Install]
WantedBy=multi-user.target
```
SQLite lives next to the app (`lunchbot.db`); back it up if you care about history.
