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

intents = discord.Intents.default()
intents.message_content = True  # REQUIRED to read "Name 180" posts.
bot = commands.Bot(command_prefix="!", intents=intents)


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
class SignupSelect(discord.ui.Select):
    """One dropdown covering up to 25 classes. Selecting sets your subscriptions
    for the classes in THIS dropdown (unselected ones here get removed)."""

    def __init__(self, chunk_index: int, class_rows):
        options = [
            discord.SelectOption(label=row["name"], value=str(row["role_id"]))
            for row in class_rows
        ]
        super().__init__(
            placeholder="Pick the classes you want to follow…",
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
    """A persistent view holding one dropdown per 25 classes."""
    view = discord.ui.View(timeout=None)
    chunks = [class_rows[i:i + 25] for i in range(0, len(class_rows), 25)]
    for i, chunk in enumerate(chunks):
        view.add_item(SignupSelect(i, chunk))
    return view


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------
@bot.event
async def on_ready():
    await db.init_db(DB_PATH)
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
    # Only act inside a class's points thread.
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

    await db.set_setting("announcements_channel_id", announcements.id)
    await db.set_setting("discussion_channel_id", discussion.id)
    await db.set_setting("signup_channel_id", signup.id)

    # ---- Hidden category for class channels ----
    hidden = discord.utils.get(guild.categories, name=HIDDEN_CATEGORY)
    if hidden is None:
        hidden = await guild.create_category(
            HIDDEN_CATEGORY,
            overwrites={
                everyone: discord.PermissionOverwrite(view_channel=False),
                me: discord.PermissionOverwrite(view_channel=True),
            },
        )

    created = 0
    for cname in CLASSES:
        class_id = await db.upsert_class(cname)
        row = await db.get_class_by_name(cname)
        if row["channel_id"] and force:
            continue  # already built; skip on a resume run
        if row["channel_id"]:
            continue

        # Class role (opt-in visibility key).
        role = discord.utils.get(guild.roles, name=cname) or await guild.create_role(
            name=cname, mentionable=False, reason="Olympiad class role"
        )
        # Role-gated channel.
        overwrites = {
            everyone: discord.PermissionOverwrite(view_channel=False),
            role: discord.PermissionOverwrite(view_channel=True, send_messages=True),
            me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
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

    # ---- Signup menu ----
    rows = await db.list_classes()
    view = build_signup_view(rows)
    bot.add_view(view)
    await signup.send(
        "**Follow your Olympiad classes**\nPick the classes you want to see. "
        "You'll only see channels for the classes you select here.",
        view=view,
    )

    await db.set_setting("current_month", month_label())
    await db.set_setting("setup_done", "1")
    await interaction.followup.send(
        f"Setup complete. Built {created} class channels (+ roles & threads).",
        ephemeral=True,
    )


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
