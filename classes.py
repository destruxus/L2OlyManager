"""
The Grand Olympiad class list.

These are the awakened/3rd-profession classes taken from the in-game
Grand Olympiad ranking screen. Order here is only used for display and for
building channels/threads at /setup time; the database keys off the name.

If a class is missing (the ranking screen can cut one off at the bottom),
add it to this list and re-run /setup with force=True to create the missing
channel, role and threads. Existing data is preserved.
"""

# Kept in the same left/right reading order as the ranking screenshot.
CLASSES = [
    "Duelist",
    "Dreadnought",
    "Phoenix Knight",
    "Hell Knight",
    "Sagittarius",
    "Adventurer",
    "Archmage",
    "Soultaker",
    "Arcana Lord",
    "Cardinal",
    "Hierophant",
    "Eva's Templar",
    "Sword Muse",
    "Wind Rider",
    "Moonlight Sentinel",
    "Mystic Muse",
    "Elemental Master",
    "Eva's Saint",
    "Shilien Templar",
    "Spectral Dancer",
    "Ghost Hunter",
    "Ghost Sentinel",
    "Spectral Master",
    "Storm Screamer",
    "Titan",
    "Shilien Saint",
    "Dominator",
    "Grand Khavatari",
    "Fortune Seeker",
    "Doomcryer",
    "Doombringer",
    "Maestro",
    "Trickster",
    "Soul Hound",
]


def channel_name(class_name: str) -> str:
    """Turn a class name into a Discord-safe channel name.

    Discord channel names must be lowercase, no spaces, no apostrophes.
    'Eva's Templar' -> 'evas-templar'
    """
    safe = class_name.lower().replace("'", "").replace(" ", "-")
    return safe


# ---------------------------------------------------------------------------
# Affiliations (which clan a contestant belongs to).
#
# 'friendly' = on our alliance's side, so they count as one of "our" candidates
# in the overview. Anything not friendly is competition (a rival we must beat).
# To add a real clan later, just append an entry here — the buttons, overview
# and rosters all read from this list, so nothing else needs changing.
# ---------------------------------------------------------------------------
OUR_CLAN = "Wolfpack"

AFFILIATIONS = [
    {"label": "Wolfpack", "emoji": "🐺", "friendly": True},
    {"label": "Unknown",  "emoji": "❓", "friendly": True},   # allied clan, name TBD
    {"label": "Rival",    "emoji": "⚔️", "friendly": False},
]

_CLAN_EMOJI = {a["label"]: a["emoji"] for a in AFFILIATIONS}
_CLAN_FRIENDLY = {a["label"]: a["friendly"] for a in AFFILIATIONS}


def clan_emoji(clan, is_member=None) -> str:
    """Emoji for a clan label, with a fallback for legacy rows that only have
    the old is_member flag."""
    if clan and clan in _CLAN_EMOJI:
        return _CLAN_EMOJI[clan]
    if is_member is None:
        return "❓"
    return "🐺" if is_member else "⚔️"


def is_friendly(clan) -> bool:
    """Whether a clan counts as on our side (defaults to friendly if unknown)."""
    return _CLAN_FRIENDLY.get(clan, True)
