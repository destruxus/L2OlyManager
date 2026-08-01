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


async def build_live_overview(db, month: str, margin: int):
    """Build the live overview embeds: every class where we field candidates,
    each candidate, and how far ahead/behind they are versus the leading
    competitor (the top-ranked rival) in that class."""
    G, Y, R, W = "\U0001F7E2", "\U0001F7E1", "\U0001F534", "\U000026AA"
    classes = await db.list_classes()
    blocks = []
    for cl in classes:
        people = await db.list_contestants(cl["id"])
        candidates = [p for p in people if p["is_member"]]
        if not candidates:
            continue
        rows = await db.standings(cl["id"], month)          # sorted high → low
        pts_by_id = {r["id"]: r["points"] for r in rows}
        rivals = [r for r in rows if not r["is_member"]]
        top = (rivals[0]["name"], rivals[0]["points"]) if rivals else None

        lines = [f"**{cl['name']}**"]
        for c in candidates:
            pts = pts_by_id.get(c["id"])
            if pts is None:
                lines.append(f"{W} {c['name']} — no score yet")
            elif top is None:
                lines.append(f"{G} {c['name']} — {pts} (leading, no rival)")
            else:
                gap = pts - top[1]
                emoji = G if gap > margin else (R if gap < -margin else Y)
                sign = f"+{gap}" if gap >= 0 else str(gap)
                lines.append(
                    f"{emoji} {c['name']} — {pts} vs {top[0]} {top[1]} ({sign})"
                )
        blocks.append("\n".join(lines))

    title = f"Live Olympiad Overview — {month}"
    if not blocks:
        return [discord.Embed(
            title=title, colour=0x3498DB,
            description="No candidates set yet — add them in #set-candidates.",
        )]

    # Pack class blocks into embeds under the 4096-char description limit.
    chunks, cur = [], ""
    for b in blocks:
        if cur and len(cur) + len(b) + 2 > 3800:
            chunks.append(cur)
            cur = ""
        cur += ("\n\n" if cur else "") + b
    if cur:
        chunks.append(cur)

    embeds = []
    for i, ch in enumerate(chunks):
        e = discord.Embed(description=ch, colour=0x3498DB)
        if i == 0:
            e.title = title
        embeds.append(e)
    embeds[-1].set_footer(
        text="🟢 ahead · 🟡 close · 🔴 behind · ⚪ no score — updates live"
    )
    return embeds
