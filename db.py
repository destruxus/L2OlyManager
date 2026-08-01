"""
SQLite data layer for the L2 Grand Olympiad bot.

Everything the bot persists lives in one SQLite file (path from config).
This module owns the schema and every query; the rest of the bot never
writes raw SQL. All functions are async (aiosqlite) because discord.py runs
on an asyncio event loop.

Tables
------
settings      key/value store for guild + channel IDs and the current month.
classes       one row per Olympiad class, plus the Discord IDs /setup created
              (channel, points thread, discussion thread, class role).
contestants   a tracked character: name, which class, member-or-rival flag.
snapshots     a timestamped points reading for a contestant (this is what a
              'Name 180' post writes). Keeping snapshots gives weekly trends.
heroes        archived month-end winners, one row per class per month.
"""

import aiosqlite

# Single shared connection. discord.py is single-threaded asyncio, so awaits
# run sequentially and one connection is safe and simplest.
_db: aiosqlite.Connection | None = None


SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS classes (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    name                 TEXT UNIQUE NOT NULL,
    channel_id           INTEGER,
    points_thread_id     INTEGER,
    discussion_thread_id INTEGER,
    role_id              INTEGER
);

CREATE TABLE IF NOT EXISTS contestants (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL COLLATE NOCASE,
    class_id   INTEGER NOT NULL,
    is_member  INTEGER NOT NULL DEFAULT 0,   -- 1 = our alliance, 0 = rival
    active     INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (name, class_id),
    FOREIGN KEY (class_id) REFERENCES classes(id)
);

CREATE TABLE IF NOT EXISTS snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    contestant_id INTEGER NOT NULL,
    points        INTEGER NOT NULL,
    matches       INTEGER,
    month         TEXT NOT NULL,             -- 'YYYY-MM'
    source        TEXT NOT NULL DEFAULT 'command',
    recorded_at   TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (contestant_id) REFERENCES contestants(id)
);

CREATE TABLE IF NOT EXISTS heroes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    month         TEXT NOT NULL,
    class_id      INTEGER NOT NULL,
    contestant_id INTEGER NOT NULL,
    points        INTEGER NOT NULL,
    recorded_at   TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (class_id) REFERENCES classes(id),
    FOREIGN KEY (contestant_id) REFERENCES contestants(id)
);

CREATE INDEX IF NOT EXISTS idx_snapshots_contestant
    ON snapshots (contestant_id, month);
"""


async def init_db(path: str) -> None:
    """Open the database and create tables if they don't exist yet."""
    global _db
    _db = await aiosqlite.connect(path)
    _db.row_factory = aiosqlite.Row
    await _db.executescript(SCHEMA)
    await _db.commit()


async def close_db() -> None:
    if _db is not None:
        await _db.close()


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------
async def set_setting(key: str, value) -> None:
    await _db.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )
    await _db.commit()


async def get_setting(key: str, default=None):
    async with _db.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cur:
        row = await cur.fetchone()
    return row["value"] if row else default


# ---------------------------------------------------------------------------
# classes
# ---------------------------------------------------------------------------
async def upsert_class(name: str) -> int:
    """Ensure a class row exists; return its id."""
    await _db.execute(
        "INSERT INTO classes(name) VALUES(?) ON CONFLICT(name) DO NOTHING", (name,)
    )
    await _db.commit()
    async with _db.execute("SELECT id FROM classes WHERE name = ?", (name,)) as cur:
        row = await cur.fetchone()
    return row["id"]


async def set_class_discord_ids(
    class_id: int, channel_id: int, points_thread_id: int,
    discussion_thread_id: int, role_id: int,
) -> None:
    await _db.execute(
        "UPDATE classes SET channel_id=?, points_thread_id=?, "
        "discussion_thread_id=?, role_id=? WHERE id=?",
        (channel_id, points_thread_id, discussion_thread_id, role_id, class_id),
    )
    await _db.commit()


async def get_class_by_name(name: str) -> aiosqlite.Row | None:
    async with _db.execute("SELECT * FROM classes WHERE name = ?", (name,)) as cur:
        return await cur.fetchone()


async def get_class_by_points_thread(thread_id: int) -> aiosqlite.Row | None:
    async with _db.execute(
        "SELECT * FROM classes WHERE points_thread_id = ?", (thread_id,)
    ) as cur:
        return await cur.fetchone()


async def list_classes() -> list[aiosqlite.Row]:
    async with _db.execute("SELECT * FROM classes ORDER BY id") as cur:
        return await cur.fetchall()


# ---------------------------------------------------------------------------
# contestants
# ---------------------------------------------------------------------------
async def add_contestant(name: str, class_id: int, is_member: bool) -> int:
    await _db.execute(
        "INSERT INTO contestants(name, class_id, is_member) VALUES(?, ?, ?) "
        "ON CONFLICT(name, class_id) DO UPDATE SET active = 1",
        (name, class_id, 1 if is_member else 0),
    )
    await _db.commit()
    async with _db.execute(
        "SELECT id FROM contestants WHERE name = ? AND class_id = ?", (name, class_id)
    ) as cur:
        row = await cur.fetchone()
    return row["id"]


async def get_contestant(name: str, class_id: int) -> aiosqlite.Row | None:
    async with _db.execute(
        "SELECT * FROM contestants WHERE name = ? AND class_id = ? AND active = 1",
        (name, class_id),
    ) as cur:
        return await cur.fetchone()


async def rename_contestant(old_name: str, new_name: str, class_id: int) -> bool:
    cur = await _db.execute(
        "UPDATE contestants SET name = ? WHERE name = ? AND class_id = ?",
        (new_name, old_name, class_id),
    )
    await _db.commit()
    return cur.rowcount > 0


async def remove_contestant(name: str, class_id: int) -> bool:
    """Soft-delete: keep history, drop from standings."""
    cur = await _db.execute(
        "UPDATE contestants SET active = 0 WHERE name = ? AND class_id = ?",
        (name, class_id),
    )
    await _db.commit()
    return cur.rowcount > 0


async def list_contestants(class_id: int) -> list[aiosqlite.Row]:
    async with _db.execute(
        "SELECT * FROM contestants WHERE class_id = ? AND active = 1 "
        "ORDER BY is_member DESC, name",
        (class_id,),
    ) as cur:
        return await cur.fetchall()


async def find_contestants_by_name(name: str) -> list[aiosqlite.Row]:
    """Find a tracked contestant by name across ALL classes.

    Used by the general points channel, where the class isn't known from the
    channel. Normally returns 0 or 1 row; more than 1 only if the same name is
    tracked in several classes.
    """
    query = """
        SELECT c.id, c.name, c.is_member, c.class_id, cl.name AS class_name
        FROM contestants c
        JOIN classes cl ON cl.id = c.class_id
        WHERE c.name = ? AND c.active = 1
        ORDER BY cl.id
    """
    async with _db.execute(query, (name,)) as cur:
        return await cur.fetchall()


# ---------------------------------------------------------------------------
# snapshots (scores)
# ---------------------------------------------------------------------------
async def add_snapshot(
    contestant_id: int, points: int, month: str,
    matches: int | None = None, source: str = "command",
) -> None:
    await _db.execute(
        "INSERT INTO snapshots(contestant_id, points, matches, month, source) "
        "VALUES(?, ?, ?, ?, ?)",
        (contestant_id, points, matches, month, source),
    )
    await _db.commit()


async def standings(class_id: int, month: str) -> list[aiosqlite.Row]:
    """Latest points per active contestant in a class for the given month,
    highest first. Contestants with no reading this month are omitted."""
    query = """
        SELECT c.id, c.name, c.is_member, s.points, s.matches, s.recorded_at
        FROM contestants c
        JOIN snapshots s ON s.contestant_id = c.id
        WHERE c.class_id = ? AND c.active = 1 AND s.month = ?
          AND s.id = (
              SELECT id FROM snapshots s2
              WHERE s2.contestant_id = c.id AND s2.month = ?
              ORDER BY s2.recorded_at DESC, s2.id DESC LIMIT 1
          )
        ORDER BY s.points DESC, c.name
    """
    async with _db.execute(query, (class_id, month, month)) as cur:
        return await cur.fetchall()


# ---------------------------------------------------------------------------
# heroes (month-end archive)
# ---------------------------------------------------------------------------
async def record_hero(month: str, class_id: int, contestant_id: int, points: int) -> None:
    await _db.execute(
        "INSERT INTO heroes(month, class_id, contestant_id, points) VALUES(?, ?, ?, ?)",
        (month, class_id, contestant_id, points),
    )
    await _db.commit()


async def heroes_for_month(month: str) -> list[aiosqlite.Row]:
    query = """
        SELECT h.points, cl.name AS class_name, c.name AS contestant_name, c.is_member
        FROM heroes h
        JOIN classes cl ON cl.id = h.class_id
        JOIN contestants c ON c.id = h.contestant_id
        WHERE h.month = ?
        ORDER BY cl.id
    """
    async with _db.execute(query, (month,)) as cur:
        return await cur.fetchall()
