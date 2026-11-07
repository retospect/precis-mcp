"""Paper-compatible inclusive block selectors shared with presentation reads."""

from __future__ import annotations

import re

from precis.errors import BadInput
from precis.utils import handle_registry

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
    # A handle pasted where an ord belongs (gr572709): the render
    # headlines each block ``pc<id> ~<ord>``, so ``pa<N>~pc<M>`` is the
    # natural misassembly — name the call that works. The handle is a
    # complete id; it needs no ref prefix.
    norm = handle_registry.normalize(sel)
    if handle_registry.is_well_formed(norm):
        raise BadInput(
            f"chunk selector {sel!r} is a handle, not a block ord: {raw!r}",
            next=f"a handle is a complete id — get(id='{norm}')",
        )
    raise BadInput(
        f"unparseable chunk selector after ~: {sel!r}",
        next="use '~N' for a single block or '~N..M' for a range",
    )
