"""
L2 Grand Olympiad Discord bot — entry point.

What it does
------------
* /setup builds the whole Discord structure once: a public "Olympiad Hub"
  (announcements, discussion, class-signup) and a hidden "Grand Olympiad"
  category holding one role-gated channel per class, each with a 📊 points
  thread and a 💬 discussion thread. It also posts a self-serve signup menu
  so members only see the classes they opt into.
* In any 📊 points thread, typing "Name 180" records a score. New names get a
  Member / Rival button prompt; known names are just updated (✅ reaction).
* /standing, /roster show current state; /remove, /rename, /overview,
  /close-month are admin tools.
* A daily scheduler posts the Friday overview and, on the last day of the
  month, archives Heroes and opens a fresh monthly cycle.

Run with:  python bot.py   (reads config.json next to this file)
"""

import json
import os
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks

import db
from classes import CLASSES, channel_name, AFFILIATIONS, clan_emoji, OUR_CLAN
from overview import (
    post_overview, build_standing_embed, build_live_overview, post_class_boards,
)
from parser import parse_score

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(HERE, "config.json"), encoding="utf-8") as f:
    CONFIG = json.load(f)

TZ = ZoneInfo(CONFIG.get("timezone", "UTC"))
GUILD_ID = int(CONFIG["guild_id"])
ADMIN_ROLE = CONFIG.get("admin_role_name", "Olympiad Manager")
MARGIN = int(CONFIG.get("contested_margin", 30))
MIN_MATCHES = int(CONFIG.get("min_matches_for_hero", 0))
OVERVIEW_HOUR = int(CONFIG.get("overview_hour", 18))
DB_PATH = os.path.join(HERE, CONFIG.get("database_path", "olympiad.db"))

HIDDEN_CATEGORY = "Grand Olympiad"
HUB_CATEGORY = "Olympiad Hub"
STAFF_CATEGORY = "Olympiad Staff"  # manager-only, hidden from everyone else

intents = discord.Intents.default()
intents.message_content = True  # REQUIRED to read "Name 180" posts.
bot = commands.Bot(command_prefix="!", intents=intents)

# Channel ids loaded from settings on startup and set during /setup.
general_points_id: int | None = None   # shared "general-points" channel
candidates_channel_id: int | None = None  # manager-only "set-candidates" channel
overview_channel_id: int | None = None    # read-only "overview" channel
overview_message_id: int | None = None    # the single live-edited overview message


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def now_local() -> datetime:
    return datetime.now(TZ)


def month_label(dt: datetime | None = None) -> str:
    return (dt or now_local()).strftime("%Y-%m")


async def get_month() -> str:
    """The currently open competition month ('YYYY-MM')."""
    m = await db.get_setting("current_month")
    if not m:
        m = month_label()
        await db.set_setting("current_month", m)
    return m


def is_admin(interaction: discord.Interaction) -> bool:
    if interaction.user.guild_permissions.administrator:
        return True
    return any(r.name == ADMIN_ROLE for r in getattr(interaction.user, "roles", []))


async def class_autocomplete(interaction: discord.Interaction, current: str):
    current = current.lower()
    return [
        app_commands.Choice(name=c, value=c)
        for c in CLASSES
        if current in c.lower()
    ][:25]


# ---------------------------------------------------------------------------
# UI: affiliation prompt for a newly seen name (Wolfpack / Unknown / Rival …)
# ---------------------------------------------------------------------------
def _affiliation_button(aff):
    """Build a button for one affiliation (friendly ones green/grey, rivals red)."""
    if not aff["friendly"]:
        style = discord.ButtonStyle.danger
    elif aff["label"] == OUR_CLAN:
        style = discord.ButtonStyle.success
    else:
        style = discord.ButtonStyle.secondary
    return discord.ui.Button(label=aff["label"], emoji=aff["emoji"], style=style)


class NewContestantView(discord.ui.View):
    """Prompt for a new name in a class points thread: one button per clan."""

    def __init__(self, name, class_id, points, matches, month):
        super().__init__(timeout=300)
        self.name = name
        self.class_id = class_id
        self.points = points
        self.matches = matches
        self.month = month
        for aff in AFFILIATIONS:
            btn = _affiliation_button(aff)
            btn.callback = self._make_cb(aff["label"], aff["friendly"])
            self.add_item(btn)

    def _make_cb(self, clan, friendly):
        async def cb(interaction):
            await self._save(interaction, clan, friendly)
        return cb

    async def _save(self, interaction: discord.Interaction, clan: str, friendly: bool):
        cid = await db.add_contestant(self.name, self.class_id, friendly, clan)
        await db.add_snapshot(cid, self.points, self.month, self.matches, source="chat")
        await interaction.response.edit_message(
            content=f"Added **{self.name}** as {clan_emoji(clan)} {clan} — "
                    f"**{self.points}** points.",
            view=None,
        )
        self.stop()
        await after_change(self.class_id)


# ---------------------------------------------------------------------------
# UI: class signup menu (persistent)
# ---------------------------------------------------------------------------
def alpha_chunks(class_rows, max_size: int = 25):
    """Sort classes A→Z and split into balanced groups of at most `max_size`,
    each tagged with its starting-letter range (e.g. 'A–H'). Splits are nudged
    to a letter boundary so the same starting letter never appears in two
    dropdowns — the labels stay unambiguous."""
    rows = sorted(class_rows, key=lambda r: r["name"].lower())
    n = len(rows)
    num = max(1, (n + max_size - 1) // max_size)
    target = (n + num - 1) // num

    def letter(r):
        return r["name"][0].lower()

    out, i = [], 0
    while i < n:
        end = min(i + target, n)
        # Don't cut in the middle of a starting-letter group (unless we'd
        # exceed max_size).
        while end < n and letter(rows[end]) == letter(rows[end - 1]) and (end - i) < max_size:
            end += 1
        part = rows[i:end]
        a = part[0]["name"][0].upper()
        z = part[-1]["name"][0].upper()
        out.append((part, a if a == z else f"{a}–{z}"))
        i = end
    return out


class SignupSelect(discord.ui.Select):
    """One dropdown covering one alphabetical group of classes. Selecting sets
    your subscriptions for the classes in THIS dropdown (unselected ones here
    get removed)."""

    def __init__(self, chunk_index: int, class_rows, letters: str):
        options = [
            discord.SelectOption(label=row["name"], value=str(row["role_id"]))
            for row in class_rows
        ]
        super().__init__(
            placeholder=f"Classes {letters} — pick the ones you want",
            min_values=0,
            max_values=len(options),
            options=options,
            custom_id=f"signup:{chunk_index}",
        )
        self._role_ids = [int(o.value) for o in options]

    async def callback(self, interaction: discord.Interaction):
        member = interaction.user
        chosen = {int(v) for v in self.values}
        to_add, to_remove = [], []
        for rid in self._role_ids:
            role = interaction.guild.get_role(rid)
            if role is None:
                continue
            if rid in chosen and role not in member.roles:
                to_add.append(role)
            elif rid not in chosen and role in member.roles:
                to_remove.append(role)
        if to_add:
            await member.add_roles(*to_add, reason="Olympiad class signup")
        if to_remove:
            await member.remove_roles(*to_remove, reason="Olympiad class signup")
        await interaction.response.send_message(
            f"Updated your classes: +{len(to_add)} / −{len(to_remove)}.", ephemeral=True
        )


def build_signup_view(class_rows) -> discord.ui.View:
    """A persistent view holding one dropdown per alphabetical group."""
    view = discord.ui.View(timeout=None)
    for i, (chunk, letters) in enumerate(alpha_chunks(class_rows)):
        view.add_item(SignupSelect(i, chunk, letters))
    return view


SIGNUP_TEXT = (
    "**Follow your Olympiad classes**\n"
    "Use the menu below to choose which classes you want visible. You'll only "
    "see the channels for the classes you pick here — everything else stays hidden. "
    "Re-open the menu any time to change your selection (pick every class you want; "
    "un-picking one removes it)."
)


async def ensure_signup_menu(channel):
    """Make sure the signup menu message is actually present in `channel`.
    Re-registers the handler if it exists; reposts it if it was deleted."""
    rows = await db.list_classes()
    view = build_signup_view(rows)
    mid = await db.get_setting("signup_message_id")
    if mid:
        try:
            await channel.fetch_message(int(mid))
            bot.add_view(view)      # still there — just rebind the handler
            return
        except discord.NotFound:
            pass                    # was deleted — repost below
        except discord.HTTPException:
            return
    bot.add_view(view)
    msg = await channel.send(SIGNUP_TEXT, view=view)
    await db.set_setting("signup_message_id", msg.id)


# ---------------------------------------------------------------------------
# UI: general points channel — class is NOT known from the channel, so a new
# name needs both a class AND member/rival before it can be saved.
# ---------------------------------------------------------------------------
class ClassPickSelect(discord.ui.Select):
    """One alphabetical group of classes; remembers the pick on its parent view."""

    def __init__(self, class_rows, letters: str):
        options = [
            discord.SelectOption(label=r["name"], value=str(r["id"]))
            for r in class_rows
        ]
        super().__init__(
            placeholder=f"Class ({letters})…", min_values=0, max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        self.view.selected_class_id = int(self.values[0])
        self.view.selected_class_name = discord.utils.get(
            self.options, value=self.values[0]
        ).label
        await interaction.response.send_message(
            f"Class set to **{self.view.selected_class_name}**. "
            "Now choose the clan.", ephemeral=True,
        )


class NewContestantGeneralView(discord.ui.View):
    """Prompt for a new name posted in the general points channel: pick a class
    from the dropdown(s), then choose the clan to save."""

    def __init__(self, name, points, matches, month, class_rows):
        super().__init__(timeout=300)
        self.name = name
        self.points = points
        self.matches = matches
        self.month = month
        self.selected_class_id = None
        self.selected_class_name = None

        for chunk, letters in alpha_chunks(class_rows):
            self.add_item(ClassPickSelect(chunk, letters))

        for aff in AFFILIATIONS:
            btn = _affiliation_button(aff)
            btn.callback = self._make_cb(aff["label"], aff["friendly"])
            self.add_item(btn)

    def _make_cb(self, clan, friendly):
        async def cb(interaction):
            await self._save(interaction, clan, friendly)
        return cb

    async def _save(self, interaction: discord.Interaction, clan: str, friendly: bool):
        if self.selected_class_id is None:
            await interaction.response.send_message(
                "Pick a class from the menu first.", ephemeral=True
            )
            return
        cid = await db.add_contestant(
            self.name, self.selected_class_id, friendly, clan
        )
        await db.add_snapshot(cid, self.points, self.month, self.matches, source="chat")
        await interaction.response.edit_message(
            content=f"Added **{self.name}** as {clan_emoji(clan)} {clan} in "
                    f"**{self.selected_class_name}** — **{self.points}** points.",
            view=None,
        )
        self.stop()
        await after_change(self.selected_class_id)


class ClassUpdateSelect(discord.ui.Select):
    """Disambiguation dropdown when a name exists in more than one class."""

    def __init__(self, rows):
        options = [
            discord.SelectOption(label=r["class_name"], value=str(r["id"]))
            for r in rows
        ]
        super().__init__(
            placeholder="Which class to update?", min_values=1, max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        v = self.view
        cid = int(self.values[0])
        cname = discord.utils.get(self.options, value=self.values[0]).label
        await db.add_snapshot(cid, v.points, v.month, v.matches, source="chat")
        await interaction.response.edit_message(
            content=f"Updated **{v.name}** in **{cname}** → **{v.points}**.", view=None
        )
        v.stop()
        await after_change(cid)


class PickClassUpdateView(discord.ui.View):
    def __init__(self, name, points, matches, month, rows):
        super().__init__(timeout=300)
        self.name = name
        self.points = points
        self.matches = matches
        self.month = month
        self.add_item(ClassUpdateSelect(rows))


async def handle_general_points(message: discord.Message):
    """Score posted in the general points channel (class unknown)."""
    parsed = parse_score(message.content)
    if parsed is None:
        return
    name, points, matches = parsed
    month = await get_month()
    found = await db.find_contestants_by_name(name)

    if len(found) == 1:
        await db.add_snapshot(found[0]["id"], points, month, matches, source="chat")
        try:
            await message.add_reaction("✅")
        except discord.HTTPException:
            pass
        await after_change(found[0]["class_id"])
    elif len(found) == 0:
        class_rows = await db.list_classes()
        view = NewContestantGeneralView(name, points, matches, month, class_rows)
        await message.reply(
            f"**{name}** is new — pick the class, then Member or Rival:", view=view
        )
    else:
        view = PickClassUpdateView(name, points, matches, month, found)
        await message.reply(
            f"**{name}** is tracked in multiple classes — which one to update?",
            view=view,
        )


def _norm(s: str) -> str:
    """Normalise a class name for tolerant matching (case/space/punctuation)."""
    return "".join(ch for ch in s.lower() if ch.isalnum())


async def resolve_class(text: str):
    """Match free-typed text to a class row, ignoring case/spaces/apostrophes."""
    target = _norm(text)
    for row in await db.list_classes():
        if _norm(row["name"]) == target:
            return row
    return None


async def handle_set_candidates(message: discord.Message):
    """Manager posts 'ClassName: Name1, Name2, …' to register candidates
    (members) for a class in one go."""
    content = message.content.strip()
    if ":" not in content:
        return  # not a candidate line — ignore staff chatter
    cls_part, names_part = content.split(":", 1)
    cls = await resolve_class(cls_part.strip())
    if cls is None:
        await message.reply(
            f"Unknown class: **{cls_part.strip()}**. "
            "Format: `Duelist: Name1, Name2`."
        )
        return
    names = [n.strip() for n in names_part.split(",") if n.strip()]
    if not names:
        await message.reply("Give at least one name, e.g. `Duelist: Name1, Name2`.")
        return
    for n in names:
        await db.add_contestant(n, cls["id"], is_member=True, clan=OUR_CLAN)
        await db.mark_candidate(n, cls["id"], True)
    await message.reply(
        f"👑 Hero candidate(s) for **{cls['name']}**: {', '.join(names)}"
    )
    await after_change(cls["id"])


async def after_change(class_id: int):
    """Refresh both live views after any score/candidate change in a class."""
    await refresh_overview()
    await update_class_board(class_id)


async def update_class_board(class_id: int):
    """Edit (or post) the always-on top-10 board in the class's own
    'class standing' thread, under that class's channel."""
    cl = await db.get_class_by_id(class_id)
    if cl is None or not cl["standings_thread_id"]:
        return
    thread = bot.get_channel(cl["standings_thread_id"])
    if thread is None:
        return
    try:
        if getattr(thread, "archived", False):
            await thread.edit(archived=False)
    except discord.HTTPException:
        pass
    month = await get_month()
    rows = await db.standings(class_id, month)
    embed = build_standing_embed(
        cl["name"], rows, MARGIN, month, title=f"{cl['name']} — Top 10", limit=10
    )
    mid = cl["standings_message_id"]
    if mid:
        try:
            await thread.get_partial_message(mid).edit(embed=embed)
            return
        except discord.NotFound:
            pass  # board was deleted — repost below
        except discord.HTTPException:
            return
    try:
        msg = await thread.send(embed=embed)
    except discord.HTTPException:
        return
    await db.set_class_standings_message(class_id, msg.id)


async def refresh_all_class_boards():
    """Ensure every class has an up-to-date board (used at /setup and startup)."""
    for cl in await db.list_classes():
        await update_class_board(cl["id"])


async def refresh_overview():
    """Rebuild and edit the single live overview message. Called after every
    score/candidate change so the overview channel stays current."""
    global overview_message_id
    if not overview_channel_id:
        return
    channel = bot.get_channel(overview_channel_id)
    if channel is None:
        return
    embeds = await build_live_overview(db, await get_month(), MARGIN)
    if overview_message_id:
        try:
            await channel.get_partial_message(overview_message_id).edit(embeds=embeds)
            return
        except discord.NotFound:
            pass  # message was deleted — fall through and post a fresh one
        except discord.HTTPException:
            return
    try:
        msg = await channel.send(embeds=embeds)
    except discord.HTTPException:
        return
    overview_message_id = msg.id
    await db.set_setting("overview_message_id", msg.id)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------
@bot.event
async def on_ready():
    await db.init_db(DB_PATH)
    # Remember the special channels across restarts.
    global general_points_id, candidates_channel_id
    global overview_channel_id, overview_message_id
    gp = await db.get_setting("points_channel_id")
    general_points_id = int(gp) if gp else None
    cc = await db.get_setting("candidates_channel_id")
    candidates_channel_id = int(cc) if cc else None
    ov = await db.get_setting("overview_channel_id")
    overview_channel_id = int(ov) if ov else None
    om = await db.get_setting("overview_message_id")
    overview_message_id = int(om) if om else None
    # Re-register the persistent signup view so its dropdowns work after restart.
    rows = await db.list_classes()
    if rows and all(r["role_id"] for r in rows):
        bot.add_view(build_signup_view(rows))
    await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
    for loop in (scheduler, friday_boards, saturday_boards):
        if not loop.is_running():
            loop.start()
    await refresh_overview()  # keep the live overview current after a restart
    await refresh_all_class_boards()
    print(f"Logged in as {bot.user} — ready.")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or message.guild is None:
        return
    # Manager-only candidate channel (hidden, so only staff can post here).
    if candidates_channel_id and message.channel.id == candidates_channel_id:
        await handle_set_candidates(message)
        return
    # General points channel: class is not implied, so unknown names are asked
    # for both class and member/rival.
    if general_points_id and message.channel.id == general_points_id:
        await handle_general_points(message)
        return
    # Otherwise only act inside a class's points thread (class implied).
    cls = await db.get_class_by_points_thread(message.channel.id)
    if cls is None:
        return
    parsed = parse_score(message.content)
    if parsed is None:
        return

    name, points, matches = parsed
    month = await get_month()
    existing = await db.get_contestant(name, cls["id"])
    if existing:
        await db.add_snapshot(existing["id"], points, month, matches, source="chat")
        try:
            await message.add_reaction("✅")
        except discord.HTTPException:
            pass
        await after_change(cls["id"])
    else:
        view = NewContestantView(name, cls["id"], points, matches, month)
        await message.reply(
            f"**{name}** isn't tracked in **{cls['name']}** yet — member or rival?",
            view=view,
        )


# ---------------------------------------------------------------------------
# Schedulers
# ---------------------------------------------------------------------------
# Daily housekeeping at OVERVIEW_HOUR (local): archive Heroes at month end.
@tasks.loop(time=time(hour=OVERVIEW_HOUR, tzinfo=TZ))
async def scheduler():
    today = now_local()
    announce = await db.get_setting("announcements_channel_id")
    announce = int(announce) if announce else None
    if (today + timedelta(days=1)).month != today.month:
        await do_close_month(announce)


@scheduler.before_loop
async def _before_scheduler():
    await bot.wait_until_ready()


# Friday 16:00 UTC — "Pre-Olympiad" top-10 board in every class points thread.
@tasks.loop(time=time(hour=16, minute=0, tzinfo=timezone.utc))
async def friday_boards():
    if datetime.now(timezone.utc).weekday() != 4:      # 4 = Friday
        return
    await post_class_boards(bot, db, await get_month(), MARGIN, "Pre-Olympiad")


@friday_boards.before_loop
async def _before_friday():
    await bot.wait_until_ready()


# Saturday 21:00 UTC — "Weekend results" top-10 board in every class points thread.
@tasks.loop(time=time(hour=21, minute=0, tzinfo=timezone.utc))
async def saturday_boards():
    if datetime.now(timezone.utc).weekday() != 5:      # 5 = Saturday
        return
    await post_class_boards(bot, db, await get_month(), MARGIN, "Weekend results")


@saturday_boards.before_loop
async def _before_saturday():
    await bot.wait_until_ready()


async def do_close_month(announce_channel_id: int | None):
    """Archive the #1 eligible contestant per class as Hero, then open next month."""
    month = await get_month()
    classes = await db.list_classes()
    winners = []
    for cl in classes:
        rows = await db.standings(cl["id"], month)
        eligible = [
            r for r in rows
            if MIN_MATCHES == 0 or (r["matches"] is not None and r["matches"] >= MIN_MATCHES)
        ]
        if not eligible:
            continue
        top = eligible[0]  # standings() is sorted high -> low
        await db.record_hero(month, cl["id"], top["id"], top["points"])
        flag = "\U0001F6E1️ (ours!)" if top["is_member"] else "⚔️"
        winners.append(f"**{cl['name']}** — {top['name']} {flag} ({top['points']})")

    # Open the next month.
    first_next = (now_local().replace(day=1) + timedelta(days=32)).replace(day=1)
    await db.set_setting("current_month", month_label(first_next))

    if announce_channel_id:
        channel = bot.get_channel(announce_channel_id)
        if channel is not None:
            embed = discord.Embed(
                title=f"\U0001F3C6 Heroes — {month}",
                description="\n".join(winners) or "No eligible contestants this month.",
                colour=0xF1C40F,
            )
            embed.set_footer(text="A new Olympiad month has begun. Points reset.")
            try:
                await channel.send(embed=embed)
            except discord.HTTPException:
                pass


# ---------------------------------------------------------------------------
# /setup
# ---------------------------------------------------------------------------
@bot.tree.command(guild=discord.Object(id=GUILD_ID),
                  description="Build the Olympiad channels, roles and threads (admin).")
@app_commands.describe(force="Create only pieces that are missing (safe to re-run).")
async def setup(interaction: discord.Interaction, force: bool = False):
    if not is_admin(interaction):
        await interaction.response.send_message("Admins only.", ephemeral=True)
        return
    if await db.get_setting("setup_done") == "1" and not force:
        await interaction.response.send_message(
            "Already set up. Re-run with `force:true` to add missing pieces.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(thinking=True, ephemeral=True)
    guild = interaction.guild
    everyone = guild.default_role
    me = guild.me

    # Admin role (create + grant to invoker if it doesn't exist yet).
    admin_role = discord.utils.get(guild.roles, name=ADMIN_ROLE)
    if admin_role is None:
        admin_role = await guild.create_role(name=ADMIN_ROLE, reason="Olympiad admin role")
        await interaction.user.add_roles(admin_role)

    # ---- Public hub (visible to everyone) ----
    hub = discord.utils.get(guild.categories, name=HUB_CATEGORY)
    if hub is None:
        hub = await guild.create_category(HUB_CATEGORY)

    async def ensure_text_channel(name, category, can_send_everyone, topic=None):
        existing = discord.utils.get(guild.text_channels, name=name)
        if existing:
            if topic and existing.topic != topic:
                await existing.edit(topic=topic)
            return existing
        overwrites = {
            everyone: discord.PermissionOverwrite(
                view_channel=True, send_messages=can_send_everyone
            ),
            me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        }
        return await guild.create_text_channel(
            name, category=category, overwrites=overwrites, topic=topic
        )

    announcements = await ensure_text_channel(
        "general-announcements", hub, False,
        "📢 Weekly Olympiad overview and monthly Hero announcements. Posts by the bot only.",
    )
    discussion = await ensure_text_channel(
        "general-discussion", hub, True,
        "💬 Open chat about the Grand Olympiad for everyone.",
    )
    signup = await ensure_text_channel(
        "class-signup", hub, False,
        "🎭 Pick which class channels you want to see — grab a class from the menu to reveal it.",
    )
    points_ch = await ensure_text_channel(
        "general-points", hub, True,
        "📊 Log scores here from any class: type `Name 180` (or `Name 180 9` for matches). "
        "New names are asked for their class and clan.",
    )
    overview_ch = await ensure_text_channel(
        "overview", hub, False,
        "🏆 Live standings: our candidates vs the leading rival in each class. Updates automatically.",
    )
    await db.set_setting("announcements_channel_id", announcements.id)
    await db.set_setting("discussion_channel_id", discussion.id)
    await db.set_setting("signup_channel_id", signup.id)
    await db.set_setting("points_channel_id", points_ch.id)
    await db.set_setting("overview_channel_id", overview_ch.id)
    global general_points_id, candidates_channel_id, overview_channel_id
    general_points_id = points_ch.id
    overview_channel_id = overview_ch.id

    if not await db.get_setting("points_intro_posted"):
        await points_ch.send(
            "**General points** — anyone can log scores here.\n"
            "Post `Name 180` (or `Name 180 9` to include matches). If the name is "
            "new, I'll ask which class it is and whether they're a member or rival. "
            "Known names are updated automatically."
        )
        await db.set_setting("points_intro_posted", "1")

    # ---- Hidden category for class channels ----
    # @everyone sees nothing here; the admin role sees every class; each class
    # channel additionally grants its own class role. So the default is exactly:
    # general channels visible to all, class channels hidden unless you hold the
    # class role — and admins see them all.
    admin_over = discord.PermissionOverwrite(view_channel=True, send_messages=True)
    bot_over = discord.PermissionOverwrite(view_channel=True, send_messages=True)

    hidden = discord.utils.get(guild.categories, name=HIDDEN_CATEGORY)
    if hidden is None:
        hidden = await guild.create_category(
            HIDDEN_CATEGORY,
            overwrites={
                everyone: discord.PermissionOverwrite(view_channel=False),
                admin_role: admin_over,
                me: bot_over,
            },
        )
    else:
        # Repair an existing category so admins can see the whole thing.
        await hidden.set_permissions(admin_role, view_channel=True, send_messages=True)

    def class_topic(cname):
        return (f"⚔️ {cname} Olympiad — 📋 class standing (live top 10), "
                f"📊 points (log scores), 💬 discussion. Visible via #class-signup.")

    async def ensure_standings_thread(class_row):
        """Create the '📋 class standing' thread under a class channel if missing."""
        if class_row["standings_thread_id"]:
            return
        channel = guild.get_channel(class_row["channel_id"])
        if channel is None:
            return
        th = await channel.create_thread(
            name="\U0001F4CB class standing", type=discord.ChannelType.public_thread,
            auto_archive_duration=10080,
        )
        await db.set_class_standings_thread(class_row["id"], th.id)

    created = 0
    repaired = 0
    for cname in CLASSES:
        class_id = await db.upsert_class(cname)
        row = await db.get_class_by_name(cname)
        if row["channel_id"]:
            # Already built. On a force run, repair admin/topic and add the
            # class-standing thread if it doesn't exist yet.
            if force:
                ch = guild.get_channel(row["channel_id"])
                if ch is not None:
                    await ch.set_permissions(
                        admin_role, view_channel=True, send_messages=True
                    )
                    if ch.topic != class_topic(cname):
                        await ch.edit(topic=class_topic(cname))
                    repaired += 1
                await ensure_standings_thread(row)
            continue

        # Class role = the opt-in visibility key for this class.
        role = discord.utils.get(guild.roles, name=cname) or await guild.create_role(
            name=cname, mentionable=False, reason="Olympiad class role"
        )
        overwrites = {
            everyone: discord.PermissionOverwrite(view_channel=False),
            role: discord.PermissionOverwrite(view_channel=True, send_messages=True),
            admin_role: admin_over,
            me: bot_over,
        }
        channel = await guild.create_text_channel(
            channel_name(cname), category=hidden, overwrites=overwrites,
            topic=class_topic(cname),
        )
        standing = await channel.create_thread(
            name="\U0001F4CB class standing", type=discord.ChannelType.public_thread,
            auto_archive_duration=10080,
        )
        points = await channel.create_thread(
            name="\U0001F4CA points", type=discord.ChannelType.public_thread,
            auto_archive_duration=10080,
        )
        talk = await channel.create_thread(
            name="\U0001F4AC discussion", type=discord.ChannelType.public_thread,
            auto_archive_duration=10080,
        )
        await db.set_class_discord_ids(class_id, channel.id, points.id, talk.id, role.id)
        await db.set_class_standings_thread(class_id, standing.id)
        created += 1

    # ---- Manager-only staff area (hidden from everyone but the admin role) ----
    staff = discord.utils.get(guild.categories, name=STAFF_CATEGORY)
    if staff is None:
        staff = await guild.create_category(
            STAFF_CATEGORY,
            overwrites={
                everyone: discord.PermissionOverwrite(view_channel=False),
                admin_role: admin_over,
                me: bot_over,
            },
        )
    else:
        await staff.set_permissions(admin_role, view_channel=True, send_messages=True)

    async def ensure_staff_channel(name, topic=None):
        existing = discord.utils.get(guild.text_channels, name=name)
        if existing:
            await existing.set_permissions(everyone, view_channel=False)
            await existing.set_permissions(
                admin_role, view_channel=True, send_messages=True
            )
            if topic and existing.topic != topic:
                await existing.edit(topic=topic)
            return existing
        return await guild.create_text_channel(
            name, category=staff, topic=topic,
            overwrites={
                everyone: discord.PermissionOverwrite(view_channel=False),
                admin_role: discord.PermissionOverwrite(
                    view_channel=True, send_messages=True
                ),
                me: bot_over,
            },
        )

    await ensure_staff_channel(
        "staff-discussion",
        "🔒 Olympiad managers only — coordination and planning.",
    )
    candidates = await ensure_staff_channel(
        "set-candidates",
        "👑 Managers: mark the Hero pick per class with `ClassName: Name1, Name2` "
        "(comma-separate for co-candidates).",
    )
    await db.set_setting("candidates_channel_id", candidates.id)
    candidates_channel_id = candidates.id

    if not await db.get_setting("candidates_intro_posted"):
        await candidates.send(
            "**Set Hero candidates** (managers only).\n"
            "A candidate is the person we're backing to win **Hero** in a class — "
            "not every clan player. Post `ClassName: Name1, Name2, …` to mark them "
            "(comma-separate for co-candidates). Example: `Duelist: Alice`. "
            "They appear crowned 👑 in #overview."
        )
        await db.set_setting("candidates_intro_posted", "1")

    # ---- Signup menu: verify it's present, repost if it was deleted ----
    await ensure_signup_menu(signup)

    await db.set_setting("current_month", month_label())
    await db.set_setting("setup_done", "1")
    await refresh_overview()          # post the initial live overview
    await refresh_all_class_boards()  # post the per-class top-10 boards
    await interaction.followup.send(
        f"Setup complete. Built {created} class channels; "
        f"repaired {repaired} for admin access.",
        ephemeral=True,
    )


# ---------------------------------------------------------------------------
# /postmenu  (admin) — (re)post the class signup menu
# ---------------------------------------------------------------------------
@bot.tree.command(name="postmenu", guild=discord.Object(id=GUILD_ID),
                  description="(Re)post the class signup menu in class-signup (admin).")
async def postmenu(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("Admins only.", ephemeral=True)
        return
    signup_id = await db.get_setting("signup_channel_id")
    if not signup_id:
        await interaction.response.send_message("Run /setup first.", ephemeral=True)
        return
    channel = bot.get_channel(int(signup_id))
    if channel is None:
        await interaction.response.send_message(
            "Can't find the class-signup channel.", ephemeral=True
        )
        return
    rows = await db.list_classes()
    view = build_signup_view(rows)
    bot.add_view(view)
    msg = await channel.send(SIGNUP_TEXT, view=view)
    await db.set_setting("signup_message_id", msg.id)
    await interaction.response.send_message("Signup menu posted.", ephemeral=True)


# ---------------------------------------------------------------------------
# /standing
# ---------------------------------------------------------------------------
@bot.tree.command(guild=discord.Object(id=GUILD_ID),
                  description="Show current standings for a class.")
@app_commands.describe(class_name="Leave blank inside a class points thread to auto-detect.")
@app_commands.autocomplete(class_name=class_autocomplete)
async def standing(interaction: discord.Interaction, class_name: str | None = None):
    if class_name is None:
        cls = await db.get_class_by_points_thread(interaction.channel_id)
        if cls is None:
            await interaction.response.send_message(
                "Name a class, or use this in a class points thread.", ephemeral=True
            )
            return
    else:
        cls = await db.get_class_by_name(class_name)
        if cls is None:
            await interaction.response.send_message("Unknown class.", ephemeral=True)
            return

    month = await get_month()
    rows = await db.standings(cls["id"], month)
    await interaction.response.send_message(
        embed=build_standing_embed(cls["name"], rows, MARGIN, month)
    )


# ---------------------------------------------------------------------------
# /roster
# ---------------------------------------------------------------------------
@bot.tree.command(guild=discord.Object(id=GUILD_ID),
                  description="List the tracked contestants in a class.")
@app_commands.autocomplete(class_name=class_autocomplete)
async def roster(interaction: discord.Interaction, class_name: str):
    cls = await db.get_class_by_name(class_name)
    if cls is None:
        await interaction.response.send_message("Unknown class.", ephemeral=True)
        return
    people = await db.list_contestants(cls["id"])
    if not people:
        await interaction.response.send_message(
            f"No contestants tracked in **{class_name}** yet.", ephemeral=True
        )
        return
    ours = [f"{clan_emoji(p['clan'], p['is_member'])} {p['name']}"
            for p in people if p["is_member"]]
    rivals = [f"{clan_emoji(p['clan'], p['is_member'])} {p['name']}"
              for p in people if not p["is_member"]]
    embed = discord.Embed(title=f"{class_name} — Roster", colour=0x3498DB)
    embed.add_field(name="Our side", value="\n".join(ours) or "—", inline=True)
    embed.add_field(name="Rivals", value="\n".join(rivals) or "—", inline=True)
    await interaction.response.send_message(embed=embed)


# ---------------------------------------------------------------------------
# /remove  and  /rename  (admin)
# ---------------------------------------------------------------------------
@bot.tree.command(guild=discord.Object(id=GUILD_ID),
                  description="Stop tracking a contestant (admin).")
@app_commands.autocomplete(class_name=class_autocomplete)
async def remove(interaction: discord.Interaction, class_name: str, name: str):
    if not is_admin(interaction):
        await interaction.response.send_message("Admins only.", ephemeral=True)
        return
    cls = await db.get_class_by_name(class_name)
    if cls is None:
        await interaction.response.send_message("Unknown class.", ephemeral=True)
        return
    ok = await db.remove_contestant(name, cls["id"])
    msg = f"Removed **{name}** from **{class_name}**." if ok else "No such contestant."
    await interaction.response.send_message(msg, ephemeral=True)


@bot.tree.command(guild=discord.Object(id=GUILD_ID),
                  description="Rename a contestant (admin).")
@app_commands.autocomplete(class_name=class_autocomplete)
async def rename(interaction: discord.Interaction, class_name: str, old_name: str, new_name: str):
    if not is_admin(interaction):
        await interaction.response.send_message("Admins only.", ephemeral=True)
        return
    cls = await db.get_class_by_name(class_name)
    if cls is None:
        await interaction.response.send_message("Unknown class.", ephemeral=True)
        return
    ok = await db.rename_contestant(old_name, new_name, cls["id"])
    msg = f"Renamed to **{new_name}**." if ok else "No such contestant."
    await interaction.response.send_message(msg, ephemeral=True)


# ---------------------------------------------------------------------------
# /overview  and  /close-month  (admin)
# ---------------------------------------------------------------------------
@bot.tree.command(guild=discord.Object(id=GUILD_ID),
                  description="Post the weekly overview now (admin).")
async def overview(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("Admins only.", ephemeral=True)
        return
    await interaction.response.defer(thinking=True, ephemeral=True)
    month = await get_month()
    announce = await db.get_setting("announcements_channel_id")
    await post_overview(bot, db, month, MARGIN, int(announce) if announce else None)
    await interaction.followup.send("Overview posted.", ephemeral=True)


@bot.tree.command(name="close-month", guild=discord.Object(id=GUILD_ID),
                  description="Archive this month's Heroes and start a new cycle (admin).")
async def close_month(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("Admins only.", ephemeral=True)
        return
    await interaction.response.defer(thinking=True, ephemeral=True)
    announce = await db.get_setting("announcements_channel_id")
    await do_close_month(int(announce) if announce else None)
    await interaction.followup.send("Month closed. Heroes archived, new cycle open.", ephemeral=True)


if __name__ == "__main__":
    bot.run(CONFIG["token"])
