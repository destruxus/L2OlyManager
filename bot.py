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
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks

import db
from classes import CLASSES, channel_name
from overview import post_overview, build_standing_embed
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
# UI: Member / Rival prompt for a newly seen name
# ---------------------------------------------------------------------------
class NewContestantView(discord.ui.View):
    def __init__(self, name, class_id, points, matches, month):
        super().__init__(timeout=300)
        self.name = name
        self.class_id = class_id
        self.points = points
        self.matches = matches
        self.month = month

    async def _save(self, interaction: discord.Interaction, is_member: bool):
        cid = await db.add_contestant(self.name, self.class_id, is_member)
        await db.add_snapshot(cid, self.points, self.month, self.matches, source="chat")
        kind = "member \U0001F6E1️" if is_member else "rival ⚔️"
        await interaction.response.edit_message(
            content=f"Added **{self.name}** as {kind} with **{self.points}** points.",
            view=None,
        )
        self.stop()

    @discord.ui.button(label="Member", style=discord.ButtonStyle.success, emoji="\U0001F6E1️")
    async def member(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._save(interaction, True)

    @discord.ui.button(label="Rival", style=discord.ButtonStyle.danger, emoji="⚔️")
    async def rival(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._save(interaction, False)


# ---------------------------------------------------------------------------
# UI: class signup menu (persistent)
# ---------------------------------------------------------------------------
def alpha_chunks(class_rows, max_size: int = 25):
    """Sort classes A→Z and split into balanced groups of at most `max_size`,
    each tagged with its starting-letter range (e.g. 'A–H'). Splitting by
    alphabet — rather than list order — makes which dropdown holds what obvious."""
    rows = sorted(class_rows, key=lambda r: r["name"].lower())
    n = len(rows)
    num = max(1, (n + max_size - 1) // max_size)
    size = (n + num - 1) // num
    out = []
    for i in range(0, n, size):
        part = rows[i:i + size]
        a = part[0]["name"][0].upper()
        z = part[-1]["name"][0].upper()
        out.append((part, a if a == z else f"{a}–{z}"))
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
            "Now click Member or Rival.", ephemeral=True,
        )


class NewContestantGeneralView(discord.ui.View):
    """Prompt for a new name posted in the general points channel: pick a class
    from the dropdown(s), then press Member or Rival to save."""

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

        member = discord.ui.Button(
            label="Member", style=discord.ButtonStyle.success, emoji="\U0001F6E1️"
        )
        rival = discord.ui.Button(
            label="Rival", style=discord.ButtonStyle.danger, emoji="⚔️"
        )
        member.callback = self._member
        rival.callback = self._rival
        self.add_item(member)
        self.add_item(rival)

    async def _member(self, interaction):
        await self._save(interaction, True)

    async def _rival(self, interaction):
        await self._save(interaction, False)

    async def _save(self, interaction: discord.Interaction, is_member: bool):
        if self.selected_class_id is None:
            await interaction.response.send_message(
                "Pick a class from the menu first.", ephemeral=True
            )
            return
        cid = await db.add_contestant(self.name, self.selected_class_id, is_member)
        await db.add_snapshot(cid, self.points, self.month, self.matches, source="chat")
        kind = "member \U0001F6E1️" if is_member else "rival ⚔️"
        await interaction.response.edit_message(
            content=f"Added **{self.name}** as {kind} in "
                    f"**{self.selected_class_name}** — **{self.points}** points.",
            view=None,
        )
        self.stop()


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
        await db.add_contestant(n, cls["id"], is_member=True)
    await message.reply(
        f"Set {len(names)} candidate(s) for **{cls['name']}**: {', '.join(names)}"
    )


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------
@bot.event
async def on_ready():
    await db.init_db(DB_PATH)
    # Remember the special channels across restarts.
    global general_points_id, candidates_channel_id
    gp = await db.get_setting("points_channel_id")
    general_points_id = int(gp) if gp else None
    cc = await db.get_setting("candidates_channel_id")
    candidates_channel_id = int(cc) if cc else None
    # Re-register the persistent signup view so its dropdowns work after restart.
    rows = await db.list_classes()
    if rows and all(r["role_id"] for r in rows):
        bot.add_view(build_signup_view(rows))
    await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
    if not scheduler.is_running():
        scheduler.start()
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
    else:
        view = NewContestantView(name, cls["id"], points, matches, month)
        await message.reply(
            f"**{name}** isn't tracked in **{cls['name']}** yet — member or rival?",
            view=view,
        )


# ---------------------------------------------------------------------------
# Scheduler: daily at OVERVIEW_HOUR
# ---------------------------------------------------------------------------
@tasks.loop(time=time(hour=OVERVIEW_HOUR, tzinfo=TZ))
async def scheduler():
    today = now_local()
    month = await get_month()
    announce = await db.get_setting("announcements_channel_id")
    announce = int(announce) if announce else None

    # Friday overview (Monday=0 … Friday=4).
    if today.weekday() == 4:
        await post_overview(bot, db, month, MARGIN, announce)

    # Last day of the month -> archive Heroes and roll the cycle.
    if (today + timedelta(days=1)).month != today.month:
        await do_close_month(announce)


@scheduler.before_loop
async def _before_scheduler():
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

    async def ensure_text_channel(name, category, can_send_everyone):
        existing = discord.utils.get(guild.text_channels, name=name)
        if existing:
            return existing
        overwrites = {
            everyone: discord.PermissionOverwrite(
                view_channel=True, send_messages=can_send_everyone
            ),
            me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        }
        return await guild.create_text_channel(name, category=category, overwrites=overwrites)

    announcements = await ensure_text_channel("general-announcements", hub, False)
    discussion = await ensure_text_channel("general-discussion", hub, True)
    signup = await ensure_text_channel("class-signup", hub, False)
    points_ch = await ensure_text_channel("general-points", hub, True)

    await db.set_setting("announcements_channel_id", announcements.id)
    await db.set_setting("discussion_channel_id", discussion.id)
    await db.set_setting("signup_channel_id", signup.id)
    await db.set_setting("points_channel_id", points_ch.id)
    global general_points_id, candidates_channel_id
    general_points_id = points_ch.id

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

    created = 0
    repaired = 0
    for cname in CLASSES:
        class_id = await db.upsert_class(cname)
        row = await db.get_class_by_name(cname)
        if row["channel_id"]:
            # Already built. On a force run, repair admin visibility on it.
            if force:
                ch = guild.get_channel(row["channel_id"])
                if ch is not None:
                    await ch.set_permissions(
                        admin_role, view_channel=True, send_messages=True
                    )
                    repaired += 1
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
            channel_name(cname), category=hidden, overwrites=overwrites
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

    async def ensure_staff_channel(name):
        existing = discord.utils.get(guild.text_channels, name=name)
        if existing:
            await existing.set_permissions(everyone, view_channel=False)
            await existing.set_permissions(
                admin_role, view_channel=True, send_messages=True
            )
            return existing
        return await guild.create_text_channel(
            name, category=staff,
            overwrites={
                everyone: discord.PermissionOverwrite(view_channel=False),
                admin_role: discord.PermissionOverwrite(
                    view_channel=True, send_messages=True
                ),
                me: bot_over,
            },
        )

    await ensure_staff_channel("staff-discussion")
    candidates = await ensure_staff_channel("set-candidates")
    await db.set_setting("candidates_channel_id", candidates.id)
    candidates_channel_id = candidates.id

    if not await db.get_setting("candidates_intro_posted"):
        await candidates.send(
            "**Set candidates** (managers only).\n"
            "Post `ClassName: Name1, Name2, …` to register candidates for a class "
            "— comma-separate to add several at once. Example: "
            "`Duelist: Alice, Bob, Carol`. They're added as members of that class."
        )
        await db.set_setting("candidates_intro_posted", "1")

    # ---- Signup menu (posted once; use /postmenu to repost) ----
    if not await db.get_setting("signup_message_id"):
        rows = await db.list_classes()
        view = build_signup_view(rows)
        bot.add_view(view)
        msg = await signup.send(SIGNUP_TEXT, view=view)
        await db.set_setting("signup_message_id", msg.id)

    await db.set_setting("current_month", month_label())
    await db.set_setting("setup_done", "1")
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
    members = [p["name"] for p in people if p["is_member"]]
    rivals = [p["name"] for p in people if not p["is_member"]]
    embed = discord.Embed(title=f"{class_name} — Roster", colour=0x3498DB)
    embed.add_field(name="🛡️ Members", value="\n".join(members) or "—", inline=True)
    embed.add_field(name="⚔️ Rivals", value="\n".join(rivals) or "—", inline=True)
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
