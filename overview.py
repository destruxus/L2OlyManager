"""
Standings display + verdict logic, and the Friday overview.

'Verdict' answers the question you actually care about each week: for every
alliance member in a class, are we winning the class or do we need to push?
It compares a member's points against the strongest rival in the same class.
"""

import discord

# Colours for the standing embeds.
GREEN = 0x2ECC71
YELLOW = 0xF1C40F
RED = 0xE74C3C
GREY = 0x95A5A6

MEDALS = {0: "\U0001F947", 1: "\U0001F948", 2: "\U0001F949"}  # 🥇🥈🥉


def verdict(member_points: int, top_rival_points: int | None, margin: int):
    """Return (emoji, short_label) for a member vs the class's top rival."""
    if top_rival_points is None:
        # No rival tracked in this class yet.
        return "\U0001F7E2", "Leading (no rival tracked)"

    gap = member_points - top_rival_points
    if gap > margin:
        return "\U0001F7E2", f"On track (+{gap} ahead)"
    if gap < -margin:
        return "\U0001F534", f"Push ({gap} behind)"
    # Within the margin either way = too close to call.
    sign = f"+{gap}" if gap >= 0 else str(gap)
    return "\U0001F7E1", f"Contested ({sign})"


def _worst_colour(rows, margin: int) -> int:
    """Pick the embed colour from the most urgent member verdict in the class."""
    members = [r for r in rows if r["is_member"]]
    if not members:
        return GREY
    rivals = [r for r in rows if not r["is_member"]]
    top_rival = max((r["points"] for r in rivals), default=None)

    colours = set()
    for m in members:
        emoji, _ = verdict(m["points"], top_rival, margin)
        colours.add(emoji)
    if "\U0001F534" in colours:
        return RED
    if "\U0001F7E1" in colours:
        return YELLOW
    return GREEN


def build_standing_embed(class_name: str, rows, margin: int, month: str) -> discord.Embed:
    """Build the ranking embed for one class."""
    embed = discord.Embed(
        title=f"{class_name} — Standings ({month})",
        colour=_worst_colour(rows, margin),
    )

    if not rows:
        embed.description = "No scores recorded yet this month."
        return embed

    rivals = [r for r in rows if not r["is_member"]]
    top_rival = max((r["points"] for r in rivals), default=None)

    # Ranking block (already sorted high -> low by the DB query).
    lines = []
    for i, r in enumerate(rows):
        medal = MEDALS.get(i, f"`{i + 1:>2}`")
        tag = "**" if r["is_member"] else ""
        who = "\U0001F6E1️" if r["is_member"] else "⚔️"  # 🛡️ / ⚔️
        matches = f"  ·  {r['matches']} matches" if r["matches"] is not None else ""
        lines.append(f"{medal} {who} {tag}{r['name']}{tag} — **{r['points']}**{matches}")
    embed.description = "\n".join(lines)

    # Per-member verdict block.
    members = [r for r in rows if r["is_member"]]
    if members:
        verdicts = []
        for m in members:
            emoji, label = verdict(m["points"], top_rival, margin)
            verdicts.append(f"{emoji} **{m['name']}** — {label}")
        embed.add_field(name="Our contestants", value="\n".join(verdicts), inline=False)

    embed.set_footer(text="🛡️ member  ·  ⚔️ rival")
    return embed


async def post_overview(bot, db, month: str, margin: int, announce_channel_id: int | None):
    """Post the standings into every class points thread that has scores,
    and a rollup into the announcements channel. Called on Fridays and by /overview."""
    classes = await db.list_classes()
    summary_lines = []

    for cl in classes:
        rows = await db.standings(cl["id"], month)
        if not rows:
            continue

        thread = bot.get_channel(cl["points_thread_id"]) if cl["points_thread_id"] else None
        if thread is not None:
            # Un-archive if Discord put the thread to sleep between weeks.
            try:
                if getattr(thread, "archived", False):
                    await thread.edit(archived=False)
                await thread.send(embed=build_standing_embed(cl["name"], rows, margin, month))
            except discord.HTTPException:
                pass

        # One summary line per class with members.
        members = [r for r in rows if r["is_member"]]
        if members:
            rivals = [r for r in rows if not r["is_member"]]
            top_rival = max((r["points"] for r in rivals), default=None)
            worst = min(
                members,
                key=lambda m: m["points"] - (top_rival if top_rival is not None else -10**9),
            )
            emoji, label = verdict(worst["points"], top_rival, margin)
            summary_lines.append(f"{emoji} **{cl['name']}** — {label}")

    if announce_channel_id:
        channel = bot.get_channel(announce_channel_id)
        if channel is not None:
            embed = discord.Embed(
                title=f"Grand Olympiad — Weekly Overview ({month})",
                description="\n".join(summary_lines) or "No contestants scored yet.",
                colour=0x3498DB,
            )
            embed.set_footer(text="🟢 on track · 🟡 contested · 🔴 push harder")
            try:
                await channel.send(embed=embed)
            except discord.HTTPException:
                pass
