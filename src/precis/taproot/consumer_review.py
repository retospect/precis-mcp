"""Re-review the graph users of an unsigned claim after an in-place edit.

Uses the existing review ledger and draft un-review mechanism, plus a durable
per-source metadata marker for the cause. No versions, jobs or model calls.
All reads/writes use the claim edit's transaction; refs are locked in id order
before either the claim or its users are changed, including reciprocal claims.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from psycopg.types.json import Jsonb

from precis.utils import handle_registry

if TYPE_CHECKING:
    from precis.store import Store


_FINDING_RELATIONS = (
    "establishes",
    "corroborates",
    "contradicts",
    "refines",
    "conjunct-of",
    "motivated-by",
    "disputes",
    "cites",
    "mentions",
)


def _users(conn: Any, hub_ref_id: int) -> list[dict[str, Any]]:
    """Resolve live direct graph users and exact prose references."""
    rows = conn.execute(
        "SELECT DISTINCT r.ref_id, r.kind, r.title, l.relation "
        "FROM links l JOIN refs r ON r.ref_id = CASE "
        "WHEN l.src_ref_id = %(hub)s THEN l.dst_ref_id ELSE l.src_ref_id END "
        "WHERE (l.src_ref_id = %(hub)s OR l.dst_ref_id = %(hub)s) "
        "AND r.ref_id <> %(hub)s AND r.retired_at IS NULL "
        "AND (r.kind IN ('draft', 'pathway', 'quest', 'todo') "
        "OR (r.kind = 'finding' AND l.relation = ANY(%(relations)s))) "
        "AND (l.src_chunk_id IS NULL OR EXISTS (SELECT 1 FROM chunks c "
        "WHERE c.chunk_id = l.src_chunk_id AND c.retired_at IS NULL)) "
        "AND (l.dst_chunk_id IS NULL OR EXISTS (SELECT 1 FROM chunks c "
        "WHERE c.chunk_id = l.dst_chunk_id AND c.retired_at IS NULL)) "
        "ORDER BY r.ref_id, l.relation",
        {"hub": hub_ref_id, "relations": list(_FINDING_RELATIONS)},
    ).fetchall()
    users: dict[int, dict[str, Any]] = {}
    for ref_id, kind, title, relation in rows:
        user = users.setdefault(
            int(ref_id),
            {
                "ref_id": int(ref_id),
                "kind": kind,
                "title": title or "",
                "handle": handle_registry.try_format(kind, ref_id)
                or f"{kind}:{ref_id}",
                "relations": [],
            },
        )
        user["relations"].append(relation)
    # Autolinking is best-effort. Consult the authoritative prose too, using
    # the same grammar, so a previous sync failure cannot hide a consumer.
    from precis.utils.draft_markup import parse_references
    from precis.utils.mentions import extract_handles

    aliases = {f"fi{hub_ref_id}", f"finding:{hub_ref_id}"}
    aliases.update(
        r[0]
        for r in conn.execute(
            "SELECT id_value FROM ref_identifiers WHERE ref_id = %s "
            "AND id_kind IN ('pub_id', 'cite_key')",
            (hub_ref_id,),
        ).fetchall()
    )
    chunk_handles: set[str] = set()
    for chunk_id, handle in conn.execute(
        "SELECT chunk_id, handle FROM chunks WHERE ref_id = %s "
        "AND (chunk_kind = 'finding_body' OR ord < 0)",
        (hub_ref_id,),
    ).fetchall():
        chunk_handles.add(f"fb{chunk_id}")
        if handle:
            chunk_handles.add(handle)
    # The SQL filter only narrows candidates; exact grammar matches below
    # exclude substring lookalikes (fi12 must not match fi123).
    tokens = aliases | chunk_handles
    patterns = [
        "%" + token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        for token in tokens
    ]
    prose = conn.execute(
        "SELECT r.ref_id, r.kind, r.title, c.text FROM chunks c "
        "JOIN refs r ON r.ref_id = c.ref_id WHERE r.ref_id <> %s "
        "AND r.kind IN ('draft','finding','pathway','quest','todo') "
        "AND r.retired_at IS NULL AND c.retired_at IS NULL AND c.ord >= 0 "
        "AND c.text LIKE ANY(%s)",
        (hub_ref_id, patterns),
    ).fetchall()
    for ref_id, kind, title, body in prose:
        targets = {
            r.target.lstrip("§¶").split("~", 1)[0] for r in parse_references(body)
        }
        targets.update(f"{k}:{i}" for k, i, _ in extract_handles(body))
        if not targets.intersection(tokens):
            continue
        user = users.setdefault(
            int(ref_id),
            {
                "ref_id": int(ref_id),
                "kind": kind,
                "title": title or "",
                "handle": handle_registry.try_format(kind, ref_id)
                or f"{kind}:{ref_id}",
                "relations": [],
            },
        )
        relation = (
            "chunk-reference"
            if targets.intersection(chunk_handles)
            else "prose-reference"
        )
        if relation not in user["relations"]:
            user["relations"].append(relation)
    return [users[ref_id] for ref_id in sorted(users)]


class _MembershipChanged(Exception):
    """Release this attempt's locks before reacquiring a different ordered set."""


def lock_users(conn: Any, hub_ref_id: int) -> list[dict[str, Any]]:
    """Lock in id order; refresh discovery after waits with bounded safe retries.

    Membership is the post-lock discovery snapshot. New users created after
    that snapshot belong to a subsequent operation, as with other graph reads.
    """
    from precis.errors import BadInput

    for _ in range(3):
        users = _users(conn, hub_ref_id)
        locked = {hub_ref_id, *(u["ref_id"] for u in users)}
        try:
            with conn.transaction():  # savepoint releases locks on retry
                conn.execute(
                    "SELECT ref_id FROM refs WHERE ref_id = ANY(%s) "
                    "AND retired_at IS NULL ORDER BY ref_id FOR NO KEY UPDATE",
                    (sorted(locked),),
                ).fetchall()
                refreshed = _users(conn, hub_ref_id)
                if any(u["ref_id"] not in locked for u in refreshed):
                    raise _MembershipChanged
                return refreshed
        except _MembershipChanged:
            continue
    raise BadInput("claim users changed during edit; retry the unchanged request")


def require_unsigned(conn: Any, hub_ref_id: int) -> None:
    """Serialize with signing, then inspect artifacts using a fresh snapshot."""
    from precis.taproot.hub import HubFrozenError

    rows = conn.execute(
        "SELECT p.id, p.state FROM nanopub_publish p "
        "WHERE p.claim_ref_id = %s ORDER BY p.id FOR UPDATE OF p",
        (hub_ref_id,),
    ).fetchall()
    # Do not combine this with SELECT FOR UPDATE: an artifact FK insert can
    # commit while it waits, invisible to that statement's original snapshot.
    artifacts = {
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT publish_id FROM nanopub_artifacts "
            "WHERE publish_id = ANY(%s)",
            ([r[0] for r in rows],),
        ).fetchall()
    }
    for publish_id, state in rows:
        if state in ("signed", "anchored", "published") or (
            state not in ("superseded", "retracted", "rejected")
            and publish_id in artifacts
        ):
            raise HubFrozenError(
                f"fi{hub_ref_id} has a {state} nanopub with signed bytes — "
                "its content is immutable; supersede it instead of editing the hub"
            )


def reopen_unsigned(conn: Any, hub_ref_id: int) -> None:
    """Discard an unsigned approval in place; caller holds the publish locks."""
    conn.execute(
        "UPDATE nanopub_publish SET state = 'candidate', approved_title = NULL, "
        "claim_sha = NULL, aida_uri = NULL, grounding = NULL, "
        "dependency_codes = NULL, trusty_uri = NULL, artifact_id = NULL, "
        "updated_at = now() WHERE claim_ref_id = %s AND state = 'reviewed'",
        (hub_ref_id,),
    )


def invalidate(
    store: Store, conn: Any, hub_ref_id: int, users: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Flag each user atomically; keep unrelated metadata/reviews unchanged."""
    changed_at = conn.execute(
        "SELECT to_char(clock_timestamp() AT TIME ZONE 'UTC', "
        '\'YYYY-MM-DD"T"HH24:MI:SS.US"Z"\')'
    ).fetchone()[0]
    source = f"fi{hub_ref_id}"
    reason = f"needs re-review because {source} changed at {changed_at}"
    marker = {"source": source, "changed_at": changed_at, "reason": reason}
    for user in users:
        ref_id = user["ref_id"]
        conn.execute(
            "UPDATE refs SET meta = jsonb_set(meta, '{claim_review_required}', "
            "(CASE WHEN jsonb_typeof(meta->'claim_review_required') = 'object' "
            "THEN meta->'claim_review_required' ELSE '{}'::jsonb END) || %s::jsonb), "
            "updated_at = now() WHERE ref_id = %s",
            (Jsonb({source: marker}), ref_id),
        )
        if user["kind"] == "draft":
            # The existing un-review operation deletes approval watermarks.
            # A ref-level citation cannot identify a single affected paragraph;
            # invalidate the draft's approvals, preserving its prose/history.
            conn.execute(
                "DELETE FROM chunk_review WHERE chunk_id IN "
                "(SELECT chunk_id FROM chunks WHERE ref_id = %s)",
                (ref_id,),
            )
        store.record_target_review(
            "ref",
            ref_id,
            actor="claim-change",
            verdict="proposed",
            note=reason,
            conn=conn,
        )
        user["reason"] = reason
        user["changed_at"] = changed_at
    return users


def render_users(users: list[dict[str, Any]]) -> str:
    """The edit acknowledgement lists every affected consumer, without truncation."""
    if not users:
        return "\nneeds re-review: none"
    lines = [f"\nneeds re-review ({len(users)}):"]
    lines.extend(
        f"- {u['handle']} ({u['kind']}): {u['title']} — "
        f"{', '.join(u['relations'])}; {u['reason']}"
        for u in users
    )
    return "\n".join(lines)
