"""Generate every short username (2c / 3c / 4c) for a given character set."""
from __future__ import annotations

import math
import random
import string
from typing import Iterator

CHARSETS = {
    "letters": string.ascii_lowercase,
    "digits": string.digits,
    "alnum": string.ascii_lowercase + string.digits,
    "all": string.ascii_lowercase + string.digits + "_.",  # everything Discord allows
}


def total(length: int, charset: str) -> int:
    """Exact number of names ``combos`` yields (names containing '..' are excluded)."""
    chars = CHARSETS[charset]
    if "." not in chars or length < 1:
        return len(chars) ** length
    other = len(chars) - 1
    no_dot, dot = other, 1  # counts of valid strings ending in a non-period / a period
    for _ in range(length - 1):
        no_dot, dot = (no_dot + dot) * other, no_dot
    return no_dot + dot


def in_space(name: str, lengths: "list[int]", charset: str) -> bool:
    """True if ``combos`` for one of ``lengths`` would yield ``name``."""
    chars = CHARSETS[charset]
    return len(name) in lengths and ".." not in name and all(c in chars for c in name)


def _name_at(index: int, length: int, chars: str) -> str:
    base = len(chars)
    out = []
    for _ in range(length):
        index, r = divmod(index, base)
        out.append(chars[r])
    return "".join(reversed(out))


def combos(length: int, charset: str = "alnum", *, shuffle: bool = False,
           seed: int | None = None) -> Iterator[str]:
    """Yield every ``length``-character name over ``charset``, lazily.

    With ``shuffle`` the order is a pseudo-random permutation (an affine map over the
    index space), so a partial run samples the whole space instead of only "aa..".
    Uses O(1) memory even for millions of names. Names with ".." are skipped.
    """
    chars = CHARSETS[charset]
    n = len(chars) ** length
    if shuffle:
        rng = random.Random(seed)
        a = rng.randrange(1, n) if n > 1 else 1
        while math.gcd(a, n) != 1:
            a = rng.randrange(1, n)
        b = rng.randrange(n)
        indices = ((a * i + b) % n for i in range(n))
    else:
        indices = iter(range(n))
    for i in indices:
        name = _name_at(i, length, chars)
        if ".." not in name:
            yield name
