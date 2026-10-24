"""Ratchet: migration SQL must not use ``ADD CONSTRAINT IF NOT EXISTS``.

Postgres has no such syntax — ``ADD COLUMN IF NOT EXISTS`` exists, but
``ADD CONSTRAINT`` has no ``IF NOT EXISTS`` form, so a migration written
that way only fails at apply time, on prod (gr514196). The repo idiom
for redefining a constraint is ``DROP CONSTRAINT IF EXISTS <name>``
followed by ``ADD CONSTRAINT <name> ...`` (migrations 0201, 0224).
"""

from __future__ import annotations

import re
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent.parent / "src" / "precis" / "migrations"

_BAD = re.compile(r"\bADD\s+CONSTRAINT\s+IF\s+NOT\s+EXISTS\b", re.IGNORECASE)
_PREFIX_RE = re.compile(r"^\d{4}_")


def test_no_add_constraint_if_not_exists() -> None:
    offenders = [
        p.name
        for p in sorted(MIGRATIONS_DIR.glob("*.sql"))
        if _PREFIX_RE.match(p.name) and _BAD.search(p.read_text(encoding="utf-8"))
    ]
    assert not offenders, (
        "`ADD CONSTRAINT IF NOT EXISTS` is not valid Postgres. Use "
        "`ALTER TABLE t DROP CONSTRAINT IF EXISTS c;` then `ALTER TABLE t "
        f"ADD CONSTRAINT c ...` instead (see 0201/0224): {offenders}"
    )
