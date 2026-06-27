"""Guard: migration numeric prefixes must be unique going forward.

The runner (:mod:`precis.store.migrate`) keys the ledger on the filename
*stem*, so two files sharing a numeric prefix (e.g. ``0037_a.sql`` +
``0037_b.sql``) don't collide functionally — but they break the
monotonic "each migration N follows N-1" mental model and are a
reliable tell that two parallel worktree branches both grabbed the next
number. This test fails the next time that happens.

Two collisions predate this guard and are grandfathered in
``_KNOWN_COLLISIONS`` (renaming them is unsafe: the stem is the ledger
key, so a rename would orphan the applied row on every existing DB and
re-run the migration). New duplicates are not allowed — pick the next
free number.
"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent.parent / "src" / "precis" / "migrations"

# Numeric prefixes that already shipped with more than one file. Frozen
# history — do NOT add to this set to silence a new collision; renumber
# the new migration instead.
_KNOWN_COLLISIONS: frozenset[str] = frozenset({"0037", "0039"})

_PREFIX_RE = re.compile(r"^(\d{4})_")


def _prefix_map() -> dict[str, list[str]]:
    by_prefix: dict[str, list[str]] = defaultdict(list)
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        m = _PREFIX_RE.match(path.name)
        assert m is not None, f"migration {path.name!r} lacks a NNNN_ prefix"
        by_prefix[m.group(1)].append(path.name)
    return by_prefix


def test_migration_prefixes_are_unique_going_forward() -> None:
    new_collisions = {
        prefix: names
        for prefix, names in _prefix_map().items()
        if len(names) > 1 and prefix not in _KNOWN_COLLISIONS
    }
    assert not new_collisions, (
        "Duplicate migration numbers detected. Each migration must take "
        "the next free NNNN prefix — pick a higher number rather than "
        f"reusing one. Offending prefixes: {new_collisions}"
    )


def test_known_collisions_still_present() -> None:
    """Keep the grandfather list honest.

    If a grandfathered collision is ever cleaned up (one of the pair
    renamed/removed), drop it from ``_KNOWN_COLLISIONS`` so the set
    doesn't rot into a stale exception that hides a real future clash.
    """
    by_prefix = _prefix_map()
    stale = {
        prefix for prefix in _KNOWN_COLLISIONS if len(by_prefix.get(prefix, [])) <= 1
    }
    assert not stale, (
        "These prefixes are in _KNOWN_COLLISIONS but no longer collide; "
        f"remove them from the allowlist: {stale}"
    )
