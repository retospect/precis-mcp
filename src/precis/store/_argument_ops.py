"""Argument-graph retraction ripple (ADR 0054 §5). Mixin on
:class:`precis.store.Store`.

The kind-scoped walk that backs both build-order steps 3 (the
``view='argument'`` read-time stale-premise flag) and 4 (the write-time
retraction push hook). Two node kinds are "walkable":

* ``finding`` — the grounded lemma, chase's chain head.
* ``memory`` tagged the open tag ``kind:lemma`` or ``kind:inference``.

A *premise* is an outbound link from a walkable node to the paper it cites
(any relation — ``cites`` for a hand-authored lemma, the finding's own
``derived-from`` chain hop). An *inference* attaches to its premises via
``derived-from`` (inference → premise, reused per ADR 0054 §2) and to its
conclusion lemma via ``entails``. Kind-scoping (only finding/kind:lemma/
kind:inference nodes count) is what keeps this walk from confusing a
premise edge with unrelated ``derived-from`` provenance (chase hops,
summary distillation) — ADR 0054 §5/§Risks R2.

Public entry point: :meth:`ArgumentGraphMixin.argument_ripple_retraction`,
called from ``_links_ops.add_link`` / ``remove_link`` whenever a
``retracts`` / ``retracted-by`` / ``raises-concern-about`` /
``concern-raised-by`` edge is created or removed (a link-handler hook, not
a background sweep). It is **recomputed, not toggled**: every call walks
forward from the affected ref to the set of candidate inferences, then
independently re-derives each candidate's staleness from current
reachability and sets/clears ``STALE:retracted-premise`` to match — so
removing the last retracting edge clears the flag while a second
still-reaching retraction keeps it (ADR 0054 §5/R5).
"""

from __future__ import annotations

from typing import Any

from psycopg import Connection
from psycopg_pool import ConnectionPool

from precis.store.types import Tag

#: Link relations whose creation/removal is retraction-ripple-relevant.
#: One physical row is ever written per edge (no write-time mirroring);
#: which endpoint is "the distrusted ref" depends on which half of the
#: pair was actually stored — see :func:`_retracted_endpoint`.
_SRC_IS_DISTRUSTED: frozenset[str] = frozenset({"retracted-by", "concern-raised-by"})
_DST_IS_DISTRUSTED: frozenset[str] = frozenset({"retracts", "raises-concern-about"})
RETRACTION_RELATIONS: frozenset[str] = _SRC_IS_DISTRUSTED | _DST_IS_DISTRUSTED

_STALE_TAG = Tag.closed("STALE", "retracted-premise")

#: Bound on the forward/backward walk depth — the argument graph is sparse
#: by design (ADR 0054 §Risks R1); this is a defensive cap against a
#: pathological cycle slipping past the visited-set guard, not a real
#: ceiling on legitimate chains.
_MAX_WALK_DEPTH = 12


def retracted_endpoint(relation: str, src_ref_id: int, dst_ref_id: int) -> int | None:
    """Which endpoint of a (relation, src, dst) link is "the distrusted ref"?

    Returns ``None`` when ``relation`` isn't retraction-ripple-relevant.
    """
    if relation in _SRC_IS_DISTRUSTED:
        return src_ref_id
    if relation in _DST_IS_DISTRUSTED:
        return dst_ref_id
    return None


def _classify_node(conn: Connection, ref_id: int) -> tuple[str, str | None] | None:
    """Return ``(kind, subkind)`` for a walkable argument-graph node, or
    ``None`` when ``ref_id`` isn't one.

    ``subkind`` is the ``kind:<x>`` open-tag value on a ``memory`` ref
    (``'lemma'`` / ``'inference'`` / anything else) — ``None`` for a
    ``finding`` (findings have no sub-kind tag).
    """
    row = conn.execute(
        "SELECT kind FROM refs WHERE ref_id = %s AND deleted_at IS NULL",
        (ref_id,),
    ).fetchone()
    if row is None:
        return None
    kind = row[0]
    if kind == "finding":
        return ("finding", None)
    if kind != "memory":
        return None
    sub_rows = conn.execute(
        "SELECT t.value FROM ref_tags rt "
        "JOIN tags t ON t.tag_id = rt.tag_id "
        "WHERE rt.ref_id = %s AND t.namespace = 'OPEN' AND t.value LIKE 'kind:%%'",
        (ref_id,),
    ).fetchall()
    for (value,) in sub_rows:
        sub = value.split(":", 1)[1]
        if sub in ("lemma", "inference"):
            return ("memory", sub)
    return None


def _is_premise_node(cls: tuple[str, str | None] | None) -> bool:
    """A walkable node that can serve as a *premise* (finding or kind:lemma)."""
    return cls is not None and (cls[0] == "finding" or cls[1] == "lemma")


def _is_inference_node(cls: tuple[str, str | None] | None) -> bool:
    return cls is not None and cls[1] == "inference"


def _premise_targets(conn: Connection, premise_ref_id: int) -> set[int]:
    """Every ref a premise (finding or kind:lemma) points at — the proxy
    for "what does this premise cite?" (no text reading: any outbound
    link target, typically the cited paper via ``cites`` or a finding's
    own chase-chain ``derived-from`` hop)."""
    rows = conn.execute(
        "SELECT DISTINCT dst_ref_id FROM links WHERE src_ref_id = %s",
        (premise_ref_id,),
    ).fetchall()
    return {r[0] for r in rows}


def _premises_citing(conn: Connection, ref_id: int) -> list[int]:
    """finding / kind:lemma nodes with an outbound link to ``ref_id``."""
    rows = conn.execute(
        "SELECT DISTINCT l.src_ref_id FROM links l "
        "JOIN refs r ON r.ref_id = l.src_ref_id AND r.deleted_at IS NULL "
        "WHERE l.dst_ref_id = %s AND r.kind IN ('finding', 'memory')",
        (ref_id,),
    ).fetchall()
    return [rid for (rid,) in rows if _is_premise_node(_classify_node(conn, rid))]


def _inferences_derived_from(conn: Connection, premise_ref_id: int) -> list[int]:
    """kind:inference memories with an outbound ``derived-from`` to
    ``premise_ref_id`` (the "inference was produced from this premise"
    edge — ADR 0054 §2)."""
    rows = conn.execute(
        "SELECT DISTINCT l.src_ref_id FROM links l "
        "JOIN refs r ON r.ref_id = l.src_ref_id AND r.deleted_at IS NULL "
        "WHERE l.dst_ref_id = %s AND l.relation = 'derived-from' AND r.kind = 'memory'",
        (premise_ref_id,),
    ).fetchall()
    return [rid for (rid,) in rows if _is_inference_node(_classify_node(conn, rid))]


def _inference_premises(conn: Connection, inference_ref_id: int) -> list[int]:
    """The kind-scoped premises of an inference: outbound ``derived-from``
    targets that are themselves finding / kind:lemma nodes."""
    rows = conn.execute(
        "SELECT dst_ref_id FROM links "
        "WHERE src_ref_id = %s AND relation = 'derived-from'",
        (inference_ref_id,),
    ).fetchall()
    return [rid for (rid,) in rows if _is_premise_node(_classify_node(conn, rid))]


def _entails_targets(conn: Connection, inference_ref_id: int) -> list[int]:
    """The conclusion lemma(s) this inference entails."""
    rows = conn.execute(
        "SELECT dst_ref_id FROM links WHERE src_ref_id = %s AND relation = 'entails'",
        (inference_ref_id,),
    ).fetchall()
    return [r[0] for r in rows]


def _entailed_by(conn: Connection, lemma_ref_id: int) -> list[int]:
    """The inference(s) that entail this lemma (its "upstream" step)."""
    rows = conn.execute(
        "SELECT src_ref_id FROM links WHERE dst_ref_id = %s AND relation = 'entails'",
        (lemma_ref_id,),
    ).fetchall()
    return [r[0] for r in rows]


def distrusted_ref_ids(conn: Connection, ref_ids: set[int]) -> set[int]:
    """Which of ``ref_ids`` carry an inbound ``retracts`` /
    ``raises-concern-about`` edge (in either physical write direction)?

    A ref is distrusted when a live link row exists in *either* form of
    the pair — the automated provenance write-through stores
    ``paper --retracted-by--> notice``; a hand-authored edge (the
    ``precis-relations`` worked example) stores
    ``memory --retracts--> paper``. Both mean the same thing.
    """
    if not ref_ids:
        return set()
    ids = list(ref_ids)
    rows = conn.execute(
        "SELECT DISTINCT ref_id FROM ("
        "  SELECT src_ref_id AS ref_id FROM links "
        "    WHERE relation = ANY(%s) AND src_ref_id = ANY(%s)"
        "  UNION"
        "  SELECT dst_ref_id AS ref_id FROM links "
        "    WHERE relation = ANY(%s) AND dst_ref_id = ANY(%s)"
        ") x",
        (
            list(_SRC_IS_DISTRUSTED),
            ids,
            list(_DST_IS_DISTRUSTED),
            ids,
        ),
    ).fetchall()
    return {r[0] for r in rows}


def _reachable_inferences_from(conn: Connection, start_ref_id: int) -> set[int]:
    """Forward BFS: every inference reachable from ``start_ref_id`` via the
    kind-scoped premise → inference → entails → (conclusion becomes the
    next premise) chain. ``start_ref_id`` need not itself be a walkable
    node (the common case: it's the retracted/concerned paper)."""
    found: set[int] = set()
    frontier: set[int] = {start_ref_id}
    seen: set[int] = set()
    depth = 0
    while frontier and depth < _MAX_WALK_DEPTH:
        next_frontier: set[int] = set()
        for node_id in frontier:
            if node_id in seen:
                continue
            seen.add(node_id)
            # Premises citing this node (node_id as a paper), plus the node
            # itself when it's already a premise (a conclusion lemma that
            # becomes the next inference's premise directly, no "citing"
            # edge involved).
            premise_candidates = set(_premises_citing(conn, node_id))
            premise_candidates.add(node_id)
            for premise_id in premise_candidates:
                if not _is_premise_node(_classify_node(conn, premise_id)):
                    continue
                for inference_id in _inferences_derived_from(conn, premise_id):
                    if inference_id in found:
                        continue
                    found.add(inference_id)
                    next_frontier.update(_entails_targets(conn, inference_id))
        frontier = next_frontier
        depth += 1
    return found


def is_inference_stale(
    conn: Connection, inference_ref_id: int, *, visited: set[int] | None = None
) -> bool:
    """Does ``inference_ref_id`` (transitively) rest on a currently
    distrusted premise?

    Walks the inference's kind-scoped premises; a premise is stale either
    directly (it cites a distrusted ref) or transitively (it's a
    conclusion lemma entailed by an upstream inference that is itself
    stale). ``visited`` guards against a cycle in a malformed graph.
    """
    visited = visited if visited is not None else set()
    if inference_ref_id in visited:
        return False
    visited.add(inference_ref_id)
    if len(visited) > _MAX_WALK_DEPTH:
        return False

    premises = _inference_premises(conn, inference_ref_id)
    if not premises:
        return False

    targets: set[int] = set()
    for premise_id in premises:
        targets |= _premise_targets(conn, premise_id)
    if targets and distrusted_ref_ids(conn, targets):
        return True

    for premise_id in premises:
        cls = _classify_node(conn, premise_id)
        if cls is not None and cls[0] == "memory" and cls[1] == "lemma":
            for upstream_id in _entailed_by(conn, premise_id):
                if is_inference_stale(conn, upstream_id, visited=visited):
                    return True
    return False


class ArgumentGraphMixin:
    """Retraction-ripple recompute for the argument graph (ADR 0054 §5)."""

    pool: ConnectionPool

    # Provided by TagsMixin — declared here (mirrors the ``soft_delete_ref:
    # Any`` forward-reference pattern in ``_links_ops.LinksMixin``) so this
    # mixin can write/clear the STALE: tag without importing TagsMixin.
    add_tag: Any
    remove_tag: Any

    def argument_ripple_retraction(
        self, conn: Connection, distrusted_ref_id: int
    ) -> set[int]:
        """Recompute ``STALE:retracted-premise`` for every inference
        reachable from ``distrusted_ref_id``.

        Called on *both* creation and removal of a retraction/concern
        edge — the walk is a pure function of current graph state, so
        the same call recomputes correctly either way (ADR 0054 §5/R5).
        Returns the set of inference ref_ids visited (mainly for tests).
        """
        candidates = _reachable_inferences_from(conn, distrusted_ref_id)
        for inference_id in candidates:
            stale = is_inference_stale(conn, inference_id)
            if stale:
                self.add_tag(inference_id, _STALE_TAG, set_by="system", conn=conn)
            else:
                self.remove_tag(inference_id, _STALE_TAG, conn=conn)
        return candidates


__all__ = [
    "RETRACTION_RELATIONS",
    "ArgumentGraphMixin",
    "distrusted_ref_ids",
    "is_inference_stale",
    "retracted_endpoint",
]
