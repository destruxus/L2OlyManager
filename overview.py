"""
Standings display + verdict logic, and the Friday overview.

'Verdict' answers the question you actually care about each week: for every
alliance member in a class, are we winning the class or do we need to push?
It compares a member's points against the strongest rival in the same class.
"""

import discord

from classes import clan_emoji

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


def build_standing_embed(class_name: str, rows, margin: int, month: str,
                         title: str | None = None, limit: int | None = None) -> discord.Embed:
    """Build the ranking embed for one class. `limit` caps the displayed rows
    (e.g. top 10); the verdict is still computed from everyone."""
    embed = discord.Embed(
        title=title or f"{class_name} — Standings ({month})",
        colour=_worst_colour(rows, margin),
    )

    if not rows:
        embed.description = "No scores recorded yet this month."
        return embed

    rivals = [r for r in rows if not r["is_member"]]
    top_rival = max((r["points"] for r in rivals), default=None)

    # Ranking block (already sorted high -> low by the DB query).
    shown = rows[:limit] if limit else rows
    lines = []
    for i, r in enumerate(shown):
        medal = MEDALS.get(i, f"`{i + 1:>2}`")
        if r["is_candidate"]:
            name = f"\U0001F451 **{r['name']}**"          # 👑 + bold for our pick
        else:
            name = f"{clan_emoji(r['clan'], r['is_member'])} {r['name']}"
        matches = f"  ·  {r['matches']} matches" if r["matches"] is not None else ""
        lines.append(f"{medal} {name} — **{r['points']}**{matches}")
    embed.description = "\n".join(lines)

    # Per-member verdict block.
    members = [r for r in rows if r["is_member"]]
    if members:
        verdicts = []
        for m in members:
            emoji, label = verdict(m["points"], top_rival, margin)
            verdicts.append(f"{emoji} **{m['name']}** — {label}")
        embed.add_field(name="Our contestants", value="\n".join(verdicts), inline=False)

    embed.set_footer(text="🐺 Wolfpack · ❓ ally · ⚔️ rival")
    return embed


async def post_class_boards(bot, db, month: str, margin: int, heading: str, limit: int = 10):
    """Post a top-`limit` board into each class's points thread — used for the
    scheduled 'Pre-Olympiad' (Fri) and 'Weekend results' (Sat) posts."""
    for cl in await db.list_classes():
        rows = await db.standings(cl["id"], month)
        if not rows:
            continue
        thread = bot.get_channel(cl["points_thread_id"]) if cl["points_thread_id"] else None
        if thread is None:
            continue
        embed = build_standing_embed(
            cl["name"], rows, margin, month,
            title=f"{heading} — {cl['name']} ({month})", limit=limit,
        )
        try:
            if getattr(thread, "archived", False):
                await thread.edit(archived=False)
            await thread.send(embed=embed)
        except discord.HTTPException:
            pass


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
    """Build the live overview — one row per class that has any score.

    Each row: class, then the current #1. If that #1 is our Hero candidate we
    show the closest chaser (how far we're ahead). If it's someone else we show
    our candidate and how far behind they are. Sorted worst-first."""
    G, Y, R, W = "\U0001F7E2", "\U0001F7E1", "\U0001F534", "\U000026AA"
    classes = await db.list_classes()

    def person(p):
        if p["is_candidate"]:
            return f"\U0001F451 **{p['name']}**"           # 👑 + bold
        return f"{clan_emoji(p['clan'], p['is_member'])} {p['name']}"

    rows = []
    for cl in classes:
        srows = await db.standings(cl["id"], month)         # scored, high → low
        if not srows:
            continue                                        # only classes with a score
        leader = srows[0]
        scored_cands = [r for r in srows if r["is_candidate"]]
        cname = cl["name"]

        if leader["is_candidate"]:
            # Our candidate is #1 — show the closest chaser.
            chaser = srows[1] if len(srows) > 1 else None
            if chaser:
                gap = leader["points"] - chaser["points"]
                status = G if gap > margin else Y
                line = (f"{status} **{cname}** — {person(leader)} {leader['points']}  ·  "
                        f"2nd {person(chaser)} {chaser['points']} (+{gap})")
                sort_val = gap
            else:
                line = (f"{G} **{cname}** — {person(leader)} {leader['points']}  ·  "
                        f"solo (leading)")
                sort_val = 10 ** 6
        elif scored_cands:
            # Someone else leads; our candidate has a score — show the gap.
            cand = scored_cands[0]
            gap = cand["points"] - leader["points"]         # negative
            status = R if gap < -margin else Y
            line = (f"{status} **{cname}** — {person(leader)} {leader['points']}  ·  "
                    f"us {person(cand)} {cand['points']} ({gap})")
            sort_val = gap
        else:
            # Someone else leads and we have no scoring candidate here.
            people = await db.list_contestants(cl["id"])
            cand0 = next((p for p in people if p["is_candidate"]), None)
            tail = (f"us \U0001F451 **{cand0['name']}** (no score)"
                    if cand0 else "no candidate")
            line = f"{W} **{cname}** — {person(leader)} {leader['points']}  ·  {tail}"
            sort_val = 10 ** 7
        rows.append((sort_val, line))

    rows.sort(key=lambda x: x[0])

    title = f"Live Olympiad Overview — {month}"
    if not rows:
        return [discord.Embed(
            title=title, colour=0x3498DB,
            description="No scores recorded yet this month.",
        )]

    lines = [r[1] for r in rows]
    embeds, cur, length = [], [], 0
    for ln in lines:
        if cur and length + len(ln) + 1 > 3800:
            e = discord.Embed(description="\n".join(cur), colour=0x3498DB)
            if not embeds:
                e.title = title
            embeds.append(e)
            cur, length = [], 0
        cur.append(ln)
        length += len(ln) + 1
    e = discord.Embed(description="\n".join(cur), colour=0x3498DB)
    if not embeds:
        e.title = title
    embeds.append(e)

    embeds[-1].set_footer(
        text="👑 our candidate · 🟢 leading · 🟡 close · 🔴 behind · ⚪ no candidate — updates live"
    )
    return embeds
