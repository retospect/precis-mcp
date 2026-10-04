"""02-postgres.yml: each role carries its own tag (no play-level tags), so
``--tags pgbouncer`` runs the pgbouncer role without the postgres role."""

from __future__ import annotations

from pathlib import Path

import yaml

_PLAYBOOK = (
    Path(__file__).resolve().parent.parent / "deploy" / "playbooks" / "02-postgres.yml"
)


def test_roles_carry_their_own_tags() -> None:
    plays = yaml.safe_load(_PLAYBOOK.read_text(encoding="utf-8"))
    assert len(plays) == 1
    play = plays[0]
    assert "tags" not in play
    assert [(r["role"], r["tags"]) for r in play["roles"]] == [
        ("postgres", ["postgres"]),
        ("pgbouncer", ["pgbouncer"]),
    ]
