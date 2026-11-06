"""Who may run DDL: the owner DSN check for runtime DDL paths (db-own).

Since the db-own cutover (migration 0279, ``scripts/db-own-cutover``) every
object in the precis schemas is owned by ``precis_owner``; the daemons' roles
(``agent_rw``, ``precis_system``, ``precis_ingress``) hold DML grants only.
DDL — ``precis migrate``, ``CREATE INDEX CONCURRENTLY``, a table swap — needs
the owner DSN, the ``precis_owner`` login, which is never a daemon's login.
On a single-login install the invoking login owns everything and every check
here passes.

:func:`owner_refusal` names the gap before a DDL path starts, so an operator
on the wrong DSN gets one line naming the owner DSN instead of a "must be
owner of table" halfway through a bulk job.
"""

from __future__ import annotations

from typing import Any

#: The owner DSN's login role (multi-user-overhaul.md §DB ownership).
OWNER_LOGIN = "precis_owner"


class OwnerRequired(RuntimeError):
    """A DDL path was started on a connection that does not own its table."""


def owner_refusal(conn: Any, table: str, *, purpose: str) -> str | None:
    """None when ``conn`` may run DDL on ``table``; else the refusal text.

    "May" = the connection's ``current_user`` has the privileges of the
    table's owner (``pg_has_role(..., 'USAGE')``, true for the owner itself
    and for superusers). A missing table passes — the DDL path reports that
    itself.
    """
    row = conn.execute(
        "SELECT c.relowner::regrole::text, current_user::text, "
        "       pg_has_role(current_user, c.relowner, 'USAGE') "
        "  FROM pg_class c WHERE c.oid = to_regclass(%s)",
        (table,),
    ).fetchone()
    if row is None or row[2]:
        return None
    owner, user = row[0], row[1]
    return (
        f"{purpose} runs DDL on {table}, owned by {owner}; this connection is "
        f"{user}. Re-run it on the owner DSN (PRECIS_DATABASE_URL="
        f"postgresql://{OWNER_LOGIN}@…/precis_prod) — "
        "docs/runbooks/prod-db-access.md."
    )


def require_owner(conn: Any, table: str, *, purpose: str) -> None:
    """Raise :class:`OwnerRequired` unless ``conn`` may run DDL on ``table``."""
    refusal = owner_refusal(conn, table, purpose=purpose)
    if refusal is not None:
        raise OwnerRequired(refusal)


__all__ = ["OWNER_LOGIN", "OwnerRequired", "owner_refusal", "require_owner"]
