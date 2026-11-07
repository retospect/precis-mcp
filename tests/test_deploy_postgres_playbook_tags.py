"""02-postgres.yml: each role carries its own tag (no play-level tags), so
``--tags pgbouncer`` runs the pgbouncer role without the postgres role.

A second, task-level tag — ``roles`` — selects only the slice of the two
roles that syncs logins, passwords, grants and the pgbouncer userlist:
``02-postgres.yml --tags roles`` is what ``scripts/deploy`` runs before the
migrate-first play when the overlay changed or the db-own owner login is
broken (gr583700, r241). The tag must never reach a task that touches
``postgresql.conf`` / ``pg_hba.conf`` or that can notify ``restart
postgresql`` (directly, or through a handler chain): the whole point of the
tag is that the sync can never restart Postgres. The pgbouncer *reload*
(SIGHUP re-reads the userlist without dropping pooled connections) is the
only handler the tagged set may fire.
"""

from __future__ import annotations

from pathlib import Path

import yaml

_PLAYBOOK = (
    Path(__file__).resolve().parent.parent / "deploy" / "playbooks" / "02-postgres.yml"
)
_ROLES = Path(__file__).resolve().parent.parent / "deploy" / "roles"


def test_roles_carry_their_own_tags() -> None:
    plays = yaml.safe_load(_PLAYBOOK.read_text(encoding="utf-8"))
    assert len(plays) == 1
    play = plays[0]
    assert "tags" not in play
    assert [(r["role"], r["tags"]) for r in play["roles"]] == [
        ("postgres", ["postgres"]),
        ("pgbouncer", ["pgbouncer"]),
    ]


def _tasks(role: str) -> list[dict]:
    return yaml.safe_load(
        (_ROLES / role / "tasks" / "main.yml").read_text(encoding="utf-8")
    )


def _handlers(role: str) -> list[dict]:
    out: list[dict] = []
    for f in sorted((_ROLES / role / "handlers").glob("*.yml")):
        out += [h for h in (yaml.safe_load(f.read_text(encoding="utf-8")) or []) if h]
    return out


def _as_list(v: str | list[str] | tuple[str, ...] | None) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    return list(v)


def _roles_tagged(task: dict) -> bool:
    return "roles" in _as_list(task.get("tags"))


def _notifies(node: object) -> list[str]:
    if isinstance(node, dict):
        out = _as_list(node.get("notify")) if "notify" in node else []
        return out + [n for v in node.values() for n in _notifies(v)]
    if isinstance(node, list):
        return [n for v in node for n in _notifies(v)]
    return []


def _handler_index() -> dict[str, list[dict]]:
    """Handler name AND listen topic -> handlers, over every role (handler
    names resolve play-wide, not just inside the notifying role)."""
    index: dict[str, list[dict]] = {}
    for role_dir in sorted(_ROLES.iterdir()):
        if not (role_dir / "handlers").is_dir():
            continue
        for h in _handlers(role_dir.name):
            for key in [h.get("name", "")] + _as_list(h.get("listen")):
                if key:
                    index.setdefault(key, []).append(h)
    return index


def _reachable_handlers(notify_names: list[str]) -> set[str]:
    """Handlers a notify reaches, following handler->handler notify chains."""
    index = _handler_index()
    reached: set[str] = set()
    queue = list(notify_names)
    while queue:
        for handler in index.get(queue.pop(), []):
            key = handler.get("name") or repr(handler)
            if key in reached:
                continue
            reached.add(key)
            queue += _notifies(handler)
    return reached


def test_named_tasks_carry_the_roles_tag() -> None:
    """The exact slice `02-postgres.yml --tags roles` must sync."""
    pg = {t.get("name"): t for t in _tasks("postgres")}
    pgb = {t.get("name"): t for t in _tasks("pgbouncer")}
    postgres_named = [
        "Set postgres runtime user",
        "Resolve uv binary path",
        "Create postgres-admin venv (macOS — python3.12 for psycopg2-binary wheels)",
        "Install psycopg2-binary into postgres-admin venv (macOS)",
        "Use psycopg2-equipped interpreter for DB-management modules (macOS)",
        "Wait for PostgreSQL to accept connections on the /tmp socket",
        "Create database roles",
        "Grant precis_system membership of agent_rw",
        "Grant precis_ingress membership of agent_rw",
        "Set defensive statement timeouts on the application roles",
    ]
    missing = [n for n in postgres_named if not _roles_tagged(pg.get(n, {}))]
    assert not missing, f"postgres tasks missing `tags: [roles]`: {missing}"
    pgbouncer_named = [
        "Deploy userlist.txt",
        "Wait for PgBouncer to listen",
    ]
    missing = [n for n in pgbouncer_named if not _roles_tagged(pgb.get(n, {}))]
    assert not missing, f"pgbouncer tasks missing `tags: [roles]`: {missing}"


def test_no_roles_tagged_task_can_restart_postgresql() -> None:
    """A tagged task that notifies `restart postgresql` — directly or via a
    handler chain — defeats the tag: the sync would restart Postgres."""
    bad = []
    for role in ("postgres", "pgbouncer"):
        for t in _tasks(role):
            if not _roles_tagged(t):
                continue
            reached = _reachable_handlers(_notifies(t))
            restarts = [h for h in reached if "restart postgresql" in h]
            if restarts:
                bad.append(f"{role}: {t.get('name')} -> {restarts}")
    assert not bad, "roles-tagged tasks reaching a postgres restart: " + "; ".join(bad)


def test_no_postgresql_conf_or_pg_hba_task_carries_roles() -> None:
    bad = []
    for t in _tasks("postgres"):
        if not _roles_tagged(t):
            continue
        body = yaml.dump(t)
        for needle in ("postgresql.conf", "pg_hba"):
            if needle in body:
                bad.append(t.get("name"))
    assert not bad, f"config tasks must not carry `roles`: {bad}"


def test_userlist_notifies_only_the_pgbouncer_reload() -> None:
    """The userlist takes effect through a reload (SIGHUP re-reads the auth
    file), never a restart — and that handler must carry `roles` itself or
    `--tags roles` notifies a handler it never runs."""
    tasks = {t.get("name"): t for t in _tasks("pgbouncer")}
    notify = _as_list(tasks["Deploy userlist.txt"].get("notify"))
    assert notify == ["reload pgbouncer"], (
        f"userlist must notify only the pgbouncer reload, got {notify}"
    )
    handlers = _handlers("pgbouncer")
    reloads = [h for h in handlers if h.get("name") == "reload pgbouncer"]
    assert reloads, "no `reload pgbouncer` handler defined"
    assert all(_roles_tagged(h) for h in reloads), (
        "the reload handler must carry `tags: [roles]` to fire under --tags roles"
    )


def test_no_roles_tagged_task_notifies_pgbouncer_restart() -> None:
    """A restart drops every pooled connection mid-deploy; the sync's only
    allowed pgbouncer handler is the reload."""
    bad = []
    for t in _tasks("pgbouncer"):
        if _roles_tagged(t) and "restart pgbouncer" in _reachable_handlers(
            _notifies(t)
        ):
            bad.append(t.get("name"))
    assert not bad, f"roles-tagged tasks reaching a pgbouncer restart: {bad}"
