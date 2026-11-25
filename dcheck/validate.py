"""Username normalization and validation for Discord's unique usernames."""

from __future__ import annotations

MIN_LENGTH = 2
MAX_LENGTH = 32
ALLOWED_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_.")


def normalize(name: str) -> str:
    """Strip surrounding whitespace, strip one leading "@", and lowercase."""
    name = name.strip()
    if name.startswith("@"):
        name = name[1:]
    return name.lower()


def validate(name: str) -> str | None:
    """Return a human-readable reason if ``name`` is invalid, else None.

    ``name`` is expected to already be normalized (see :func:`normalize`).
    Rules: length 2-32; only a-z, 0-9, "_" and "."; no "..".
    """
    if len(name) < MIN_LENGTH:
        return f"too short (must be {MIN_LENGTH}-{MAX_LENGTH} characters)"
    if len(name) > MAX_LENGTH:
        return f"too long (must be {MIN_LENGTH}-{MAX_LENGTH} characters)"
    bad = sorted({ch for ch in name if ch not in ALLOWED_CHARS})
    if bad:
        shown = ", ".join(repr(ch) for ch in bad)
        return f"invalid characters: {shown} (only a-z, 0-9, _ and . allowed)"
    if ".." in name:
        return "cannot contain two consecutive periods (..)"
    return None
