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
