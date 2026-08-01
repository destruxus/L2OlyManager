# L2 Grand Olympiad Manager (Discord bot)

A Discord bot for an alliance to track **Grand Olympiad** standings — your own
contestants and their per-class rivals — on a monthly cycle, with a weekly
Friday overview that tells you, per class, whether you're winning or need to
push. Runs on a Linux VPS.

---

## How it works (the short version)

- **Score entry is just chat.** In a class's `📊 points` thread you type
  `Name 180`. Known name → score updated (✅). New name → the bot asks *Member*
  or *Rival* with two buttons, then saves.
- **Opt-in class visibility.** Each class is a hidden, role-gated channel. A
  self-serve dropdown in `class-signup` lets each person pick which classes
  they follow, so nobody drowns in 33 channels.
- **Talk is separated from work.** Every class channel has a `📊 points` thread
  (scores only) and a `💬 discussion` thread (chatter). There's also a global
  `general-announcements` and `general-discussion`.
- **Monthly cycle.** Weekly ranking snapshots feed the standings; on the last
  day of the month the bot archives the Hero per class and opens a fresh cycle
  (points reset, history kept).

## Discord layout that `/setup` builds

```
Olympiad Hub            (category, visible to everyone)
  #general-announcements   read-only; weekly overview + Heroes land here
  #general-discussion      free chat
  #class-signup            the "pick your classes" dropdown

Grand Olympiad          (category, hidden by default)
  #duelist   → 📊 points   💬 discussion     (visible only with the Duelist role)
  #dreadnought → 📊 points 💬 discussion
  … one per class (33)
```

Each class also gets a **role** with the class's name; holding it reveals that
class's channel and its two threads. The signup dropdown grants/removes those
roles.

## Commands

| Command | Who | What |
|---|---|---|
| `/setup [force]` | admin | Build all channels, roles, threads, signup menu. `force:true` re-runs safely, adding only missing pieces. |
| *(just type)* `Name 180` | anyone | In a `📊 points` thread: record/update a score. Optional matches: `Name 180 9`. |
| `/standing [class]` | anyone | Ranking for a class (members vs rivals + verdicts). Omit `class` inside a points thread. |
| `/roster <class>` | anyone | Who's tracked in a class. |
| `/remove <class> <name>` | admin | Stop tracking a contestant (history kept). |
| `/rename <class> <old> <new>` | admin | Rename a contestant. |
| `/overview` | admin | Post the weekly overview now. |
| `/close-month` | admin | Archive Heroes and start a new cycle now. |

**Verdict colours:** 🟢 on track (ahead of top rival by more than the margin) ·
🟡 contested (within the margin) · 🔴 push (behind).

---

## Setup

### 1. Create the Discord application/bot
1. https://discord.com/developers/applications → **New Application**.
2. **Bot** tab → **Add Bot** → copy the **token**.
3. Still on the Bot tab, enable **Message Content Intent** (required — without
   it the bot can't read `Name 180`).
4. **OAuth2 → URL Generator**: scopes `bot` + `applications.commands`.
   Permissions: *Manage Roles, Manage Channels, Manage Threads, View Channels,
   Send Messages, Send Messages in Threads, Create Public Threads, Embed Links,
   Add Reactions, Read Message History*. Open the generated URL and invite the
   bot to your server.
5. In **Server Settings → Roles**, drag the bot's role **above** where the class
   roles will sit (near the top) so it can manage them.

> Tip: it's simplest to grant the bot **Administrator** on invite; then step 5
> is automatic and permission overwrites can never trip it up.

### 2. Configure
```bash
cp config.example.json config.json
```
Edit `config.json`: paste the `token`, set `guild_id` (right-click server →
Copy Server ID, with Developer Mode on), and adjust `timezone`,
`contested_margin`, `overview_hour`, `min_matches_for_hero` to taste.

### 3. Run locally to test
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python bot.py
```
In Discord, run `/setup` once. Then type a score in any `📊 points` thread.

### 4. Deploy on the VPS (systemd)
```bash
sudo useradd -r -m -d /opt/l2-olympiad-bot olympiad      # service user
sudo cp -r . /opt/l2-olympiad-bot                        # copy the project
cd /opt/l2-olympiad-bot
sudo -u olympiad python3 -m venv .venv
sudo -u olympiad .venv/bin/pip install -r requirements.txt
sudo cp l2-olympiad.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now l2-olympiad
journalctl -u l2-olympiad -f                             # watch logs
```

---

## Files

| File | Purpose |
|---|---|
| `bot.py` | Entry point: setup, signup menu, score listener, commands, scheduler. |
| `db.py` | SQLite schema + all queries (async). |
| `parser.py` | Turns a `Name 180` chat line into (name, points, matches). |
| `overview.py` | Standing embeds + the Friday verdict logic. |
| `classes.py` | The 33-class list + channel-name helper. |
| `config.example.json` | Template config; copy to `config.json`. |
| `l2-olympiad.service` | systemd unit for the VPS. |
| `requirements.txt` | Python dependencies. |

## Roadmap (built vs planned)

- ✅ **Phase 1–3** (this build): channel-based score entry, opt-in class
  visibility, member/rival tracking, `/standing`, weekly overview + verdicts,
  monthly Hero archive and cycle reset.
- ⏳ **Phase 4 — screenshot OCR assist**: post a ranking screenshot, the bot
  parses names + points, fuzzy-matches them to your roster, and shows a
  confirm-before-save prompt (OCR is never trusted blindly).

## Notes & limits

- The signup dropdown treats each menu as "your full selection" for the classes
  in it: selecting classes there sets your subscriptions, and un-picking one
  removes that role. (A toggle-button version is a possible later refinement.)
- Idle threads auto-archive; the bot un-archives a points thread when it posts
  the weekly overview, and uses the maximum 7-day auto-archive window.
- Olympiad points can be negative — `Name -30` is valid.
