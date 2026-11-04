"""Paper-compatible inclusive block selectors shared with presentation reads."""

from __future__ import annotations

import re

from precis.errors import BadInput

_RANGE_RE = re.compile(r"^(\d+)(?:\.\.|-)(\d+)$")
# Negative single ords address synthetic cards; ranges stay nonnegative.
_CHUNK_RE = re.compile(r"^(-?\d+)$")


def parse_block_selector(sel: str, *, raw: str) -> tuple[int, int]:
    """Parse the part after ``~``, without an optional view path."""
    rng = _RANGE_RE.match(sel)
    if rng:
        lo, hi = int(rng.group(1)), int(rng.group(2))
        if lo > hi:
            raise BadInput(
                f"empty chunk range: {raw!r}",
                next="ranges run lo..hi inclusive (e.g. '~3..7')",
            )
        return lo, hi
    single = _CHUNK_RE.match(sel)
    if single:
        n = int(single.group(1))
        return n, n
    raise BadInput(
        f"unparseable chunk selector after ~: {sel!r}",
        next="use '~N' for a single block or '~N..M' for a range",
    )
