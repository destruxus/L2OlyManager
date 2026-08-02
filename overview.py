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
        who = clan_emoji(r["clan"], r["is_member"])
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

    embed.set_footer(text="🐺 Wolfpack · ❓ ally · ⚔️ rival")
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


# Table columns: (data key, header, alignment, max width)
_OVERVIEW_COLS = [
    ("class",  "CLASS",      "l", 15),
    ("clan",   "CLAN",       "l", 8),
    ("cand",   "CAND.",      "l", 12),
    ("pts",    "PTS",        "r", 4),
    ("rival",  "BEST RIVAL", "l", 16),
    ("gap",    "GAP",        "r", 5),
    ("status", "STATUS",     "l", 8),
]


def _trunc(value, n: int) -> str:
    s = str(value)
    return s if len(s) <= n else s[: n - 1] + "…"


async def build_live_overview(db, month: str, margin: int):
    """Build the live overview as a monospace table: every class where we field
    candidates, each candidate, and how far ahead/behind they are versus the
    leading competitor. Sorted most-behind-first so problems sit at the top."""
    classes = await db.list_classes()

    # 1) Gather one record per candidate, grouped by class, with a status rank.
    #    rank: 0 behind, 1 no score, 2 close, 3 ahead/leading (lower = more urgent)
    class_groups = []
    for cl in classes:
        people = await db.list_contestants(cl["id"])
        candidates = [p for p in people if p["is_member"]]
        if not candidates:
            continue
        srows = await db.standings(cl["id"], month)
        pts_by_id = {r["id"]: r["points"] for r in srows}
        rivals = [r for r in srows if not r["is_member"]]
        top = rivals[0] if rivals else None

        recs = []
        for c in candidates:
            pts = pts_by_id.get(c["id"])
            if pts is None:
                rank, gap, status = 1, None, "NO SCORE"
            elif top is None:
                rank, gap, status = 3, None, "LEADING"
            else:
                gap = pts - top["points"]
                rank, status = (
                    (3, "AHEAD") if gap > margin
                    else (0, "BEHIND") if gap < -margin
                    else (2, "CLOSE")
                )
            recs.append({
                "clan": c["clan"] or "",
                "cand": c["name"],
                "pts": pts,
                "rival": f"{top['name']} {top['points']}" if top else "—",
                "gap": gap,
                "status": status,
                "rank": rank,
            })
        recs.sort(key=lambda r: (r["rank"], r["gap"] if r["gap"] is not None else 0))
        sort_key = (
            min(r["rank"] for r in recs),
            min((r["gap"] for r in recs if r["gap"] is not None), default=0),
        )
        class_groups.append((sort_key, cl["name"], recs))

    class_groups.sort(key=lambda g: g[0])

    title = f"Live Olympiad Overview — {month}"
    if not class_groups:
        return [discord.Embed(
            title=title, colour=0x3498DB,
            description="No candidates set yet — add them in #set-candidates.",
        )]

    # 2) Flatten to text cells (class name shown once per group).
    cells = []
    for _, cname, recs in class_groups:
        for i, r in enumerate(recs):
            gap = r["gap"]
            cells.append({
                "class": cname if i == 0 else "",
                "clan": r["clan"],
                "cand": r["cand"],
                "pts": "—" if r["pts"] is None else str(r["pts"]),
                "rival": r["rival"],
                "gap": "—" if gap is None else (f"+{gap}" if gap >= 0 else str(gap)),
                "status": r["status"],
            })

    # 3) Compute column widths and format rows.
    widths = {
        key: max(len(hdr), *(len(_trunc(row[key], mx)) for row in cells))
        for key, hdr, _a, mx in _OVERVIEW_COLS
    }

    def fmt(values):
        out = []
        for (key, _h, align, mx), val in zip(_OVERVIEW_COLS, values):
            v = _trunc(val, mx)
            out.append(v.rjust(widths[key]) if align == "r" else v.ljust(widths[key]))
        return "  ".join(out).rstrip()

    header = fmt([c[1] for c in _OVERVIEW_COLS])
    sep = "─" * len(header)
    body = [fmt([row[c[0]] for c in _OVERVIEW_COLS]) for row in cells]

    # 4) Pack into code-block embeds under the 4096-char description limit,
    #    repeating the header on each.
    def make_embed(lines, first):
        block = "```\n" + header + "\n" + sep + "\n" + "\n".join(lines) + "\n```"
        e = discord.Embed(description=block, colour=0x3498DB)
        if first:
            e.title = title
        return e

    embeds, cur = [], []
    base = len(header) + len(sep) + 12
    length = base
    for line in body:
        if cur and length + len(line) + 1 > 3800:
            embeds.append(make_embed(cur, not embeds))
            cur, length = [], base
        cur.append(line)
        length += len(line) + 1
    if cur:
        embeds.append(make_embed(cur, not embeds))

    embeds[-1].set_footer(
        text="🟢 AHEAD · 🟡 CLOSE · 🔴 BEHIND · ⚪ NO SCORE — updates live"
    )
    return embeds
