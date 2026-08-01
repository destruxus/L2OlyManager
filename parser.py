"""
Parse a score post typed into a class points thread.

Accepted shapes (case-insensitive, extra spaces tolerated):
    Xyzabc 180            -> name 'Xyzabc', points 180
    Xyzabc 180 9          -> name 'Xyzabc', points 180, matches 9
    Dark Knight -30       -> name 'Dark Knight', points -30 (spaces + negatives ok)

Rules
-----
* The LAST number on the line is points, UNLESS there are two trailing numbers,
  in which case it's '<points> <matches>'.
* Olympiad points can be negative, so a leading '-' on the points is allowed.
* Everything before the trailing number(s) is the character name.
* A line with no trailing number is ignored (returns None) — that's just chat
  that happened to land in the points thread.
"""

import re

# name (non-greedy) + points (optional sign) + optional matches (non-negative)
_PATTERN = re.compile(
    r"^\s*(?P<name>.+?)\s+(?P<points>-?\d+)(?:\s+(?P<matches>\d+))?\s*$"
)


def parse_score(content: str):
    """Return (name, points, matches|None) or None if the line isn't a score."""
    if not content:
        return None

    # Ignore anything that starts like a command or a bot mention.
    stripped = content.strip()
    if stripped.startswith(("/", "!", "<@")):
        return None

    match = _PATTERN.match(stripped)
    if not match:
        return None

    name = match.group("name").strip()
    points = int(match.group("points"))
    matches = int(match.group("matches")) if match.group("matches") else None

    if not name:
        return None

    return name, points, matches
