"""Ansible runs only the first handler matching a notified *name*.

Two same-named handlers in one role (darwin then linux) silently drop the
second: the Linux restart never fired (gr494656). OS-split handlers must have
distinct names and share a ``listen:`` topic, which fires every match.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import yaml

ROLES = Path(__file__).resolve().parent.parent / "deploy" / "roles"


def _handlers(role: Path) -> list[dict]:
    out: list[dict] = []
    for f in sorted((role / "handlers").glob("*.yml")):
        out += [h for h in (yaml.safe_load(f.read_text(encoding="utf-8")) or []) if h]
    return out


def _as_list(v: object) -> list[str]:
    if v is None:
        return []
    return [v] if isinstance(v, str) else list(v)  # type: ignore[call-overload]


def test_no_duplicate_handler_names_within_a_role() -> None:
    dupes = []
    for role in sorted(ROLES.iterdir()):
        names = Counter(h["name"] for h in _handlers(role) if "name" in h)
        dupes += [f"{role.name}: {n!r} x{c}" for n, c in names.items() if c > 1]
    assert not dupes, "same-named handlers (only the first runs): " + "; ".join(dupes)


def _notifies(node: object) -> list[str]:
    if isinstance(node, dict):
        out = _as_list(node.get("notify")) if "notify" in node else []
        return out + [n for v in node.values() for n in _notifies(v)]
    if isinstance(node, list):
        return [n for v in node for n in _notifies(v)]
    return []


def test_notify_resolves_in_the_split_handler_roles() -> None:
    # Scoped to the roles fixed for gr494656; other roles notify handlers
    # defined in sibling roles, which a per-role check cannot see.
    missing = []
    for name in ("node_exporter", "monitoring"):
        role = ROLES / name
        known: set[str] = set()
        for h in _handlers(role):
            known.add(h.get("name", ""))
            known.update(_as_list(h.get("listen")))
        for f in sorted((role / "tasks").glob("*.yml")):
            doc = yaml.safe_load(f.read_text(encoding="utf-8"))
            missing += [
                f"{name}/{f.name}: {n}" for n in _notifies(doc) if n not in known
            ]
    assert not missing, "notify targets with no handler: " + "; ".join(missing)
