"""Design-tree leaf hints and the STATUS:done finish gate (S4, gr516957).

Two read-time surfaces over the S0/S1 design-tree vocabulary
(:mod:`precis.design_tree`, ``joints``' ``params.leaf``):

* :func:`leaf_hint_rows` — the ``Next:`` rows an ``edit(kind='se')``
  reply appends when a ``connect``/``set_joint`` op names no
  ``joint.params.leaf``. Candidates are the export leaves whose
  ``implies`` ``{class, mechanism}`` matches the joint's;
  class+mechanism hits rank above class-only, each tier sorted by
  ``(implies.rank, id)``, the list caps at three. Each row is the exact
  call that reads the leaf's imported
  finding — a leaf id resolves to ``fi<N>`` through the live finding
  carrying ``meta.dtree.id`` (the importer's key, ``_KEY_SQL['dtree']``
  in :mod:`precis.cli.conventions`). A leaf no import has landed
  degrades to the importer's search door; no match at all means no row
  — the packaged export legitimately carries zero ``implies`` today,
  and a hint must never sink an edit that already landed.
* :func:`finish_warnings` — the soft gate :meth:`SeHandler.tag` appends
  when ``STATUS:done`` is added: the parts with no ``principles`` note
  and the connects still leafless. Warnings only, never a refusal.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from precis import design_tree as core_design_tree

if TYPE_CHECKING:
    from precis.store import Store
    from precis_se.ops import SeTree

log = logging.getLogger(__name__)

#: How many leaf rows a leafless connect can earn — the brief's "top
#: 2-3" (gr516957).
_MAX_LEAVES = 3

#: A class carrying more leaves than this is broad — its class-only
#: tier is noise (three arbitrary picks out of dozens), so only exact
#: class+mechanism hits earn a row (DESIGN, gr516957).
_BROAD_CLASS = 5

#: ``implies.rank`` absent or non-numeric — sorts behind ranked leaves.
_NO_RANK = 99

#: The space tag the design-tree importer stamps on every finding it
#: mints (:mod:`precis.cli.conventions`) — the fallback search door when
#: a leaf has no imported row to name.
_SPACE_TAG = "SPACE:design-conventions"


def leaf_candidates(klass: str | None, mechanism: str | None) -> list[str]:
    """Export leaf ids whose ``implies`` matches a joint's
    class/mechanism: class+mechanism hits first, then class-only (a
    class-matching leaf carrying no mechanism of its own), each tier
    sorted by ``(implies.rank, id)``, capped at ``_MAX_LEAVES``.

    ``None``/empty class means no match. A leaf implying a *different*
    mechanism than the connect declared contradicts it — dropped, not
    demoted. And when the class has more than ``_BROAD_CLASS``
    ``implies``-carrying leaves the class-only tier is noise and drops
    entirely."""
    if not klass:
        return []
    export = core_design_tree.load_export()
    leaves = frozenset(export.get("leaves") or ())
    matched: list[tuple[str, dict[str, Any]]] = []
    for nid, entry in (export.get("nodes") or {}).items():
        if nid not in leaves or not isinstance(entry, dict):
            continue
        implies = entry.get("implies")
        if isinstance(implies, dict) and implies.get("class") == klass:
            matched.append((nid, implies))
    both: list[str] = []
    class_only: list[str] = []
    rank_of: dict[str, float] = {}
    for nid, implies in matched:
        rank = implies.get("rank")
        rank_of[nid] = (
            rank
            if isinstance(rank, (int, float)) and not isinstance(rank, bool)
            else _NO_RANK
        )
        implied_mech = implies.get("mechanism")
        if mechanism is not None and implied_mech == mechanism:
            both.append(nid)
        elif implied_mech is None or mechanism is None:
            # class-only: the leaf carries no mechanism, or the connect
            # declared none — either way nothing contradicts.
            class_only.append(nid)
        # else the leaf implies a DIFFERENT mechanism — a contradiction,
        # not a class-only match; fewer honest rows beat padding.
    if len(matched) > _BROAD_CLASS:
        class_only = []
    return (
        sorted(both, key=lambda nid: (rank_of[nid], nid))
        + sorted(class_only, key=lambda nid: (rank_of[nid], nid))
    )[:_MAX_LEAVES]


def leaf_hint_rows(
    store: Store, op_list: list[dict[str, Any]] | None
) -> list[tuple[str, str]]:
    """The ``(call, description)`` Next: rows for an edit's ``connect``/
    ``set_joint`` ops carrying no ``joint.params.leaf`` — one row per
    leaf candidate, deduped across ops (several same-class connects
    share the walk)."""
    rows: list[tuple[str, str]] = []
    seen: set[str] = set()
    for op in op_list or []:
        if not isinstance(op, dict) or op.get("op") not in ("connect", "set_joint"):
            continue
        joint = op.get("joint")
        if not isinstance(joint, dict):
            continue
        params = joint.get("params")
        if isinstance(params, dict) and params.get("leaf"):
            continue
        klass = joint.get("class")
        mech = joint.get("mechanism")
        for leaf in leaf_candidates(
            str(klass).strip() if klass else None,
            str(mech).strip() if mech is not None else None,
        ):
            if leaf in seen:
                continue
            seen.add(leaf)
            function = leaf.split("/", 1)[0]
            ref_id = _finding_ref_id(store, leaf)
            if ref_id is not None:
                rows.append(
                    (
                        f"get(kind='finding', id='fi{ref_id}', extent='fisheye+1hop')",
                        f"read the {function} leaf {leaf!r} — then set "
                        "params.leaf on the connect",
                    )
                )
            else:
                rows.append(
                    (
                        f"search(kind='finding', tags=['{_SPACE_TAG}'], q={leaf!r})",
                        f"find the {function} leaf {leaf!r}'s finding — "
                        "no imported row names it yet",
                    )
                )
    return rows


def _finding_ref_id(store: Store, leaf_id: str) -> int | None:
    """The live finding ref holding this design-tree node
    (``meta.dtree.id``), or ``None`` when no import has landed it. A
    lookup fault degrades to the search row — the hint is a courtesy on
    a write that already succeeded."""
    try:
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT ref_id FROM refs WHERE kind = 'finding' "
                "AND meta->'dtree'->>'id' = %s AND retired_at IS NULL "
                "ORDER BY ref_id LIMIT 1",
                (leaf_id,),
            ).fetchone()
    except Exception as exc:
        log.warning("se: leaf-hint finding lookup failed: %s", exc)
        return None
    return int(row[0]) if row is not None else None


def _has_principles(tree: SeTree, block: str) -> bool:
    """A part counts as covered by a ``principles`` note whose ``about``
    names it — ``'lid'`` or any ``'lid.<sub>'`` anchor — or by any note
    carrying the ``principles-<part>`` name (the pre-S4 'decision note'
    convention the dtree skill documented)."""
    for n in tree.notes:
        if n.kind == "principles" and any(a.split(".", 1)[0] == block for a in n.about):
            return True
        if n.name == f"principles-{block}":
            return True
    return False


def _bought(tree: SeTree, node: Any) -> bool:
    """A bought block — ``bound_kind`` ``component``/``part`` on it or
    on the template it instances (the binding is template-owned, so an
    instance reads ``None``). Bought parts have no design principles to
    record; flagging 30 screws as missing notes is noise."""
    if node.bound_kind in ("component", "part"):
        return True
    tmpl = tree.blocks.get(node.template) if node.template else None
    return tmpl is not None and tmpl.bound_kind in ("component", "part")


def finish_warnings(tree: SeTree) -> str:
    """The ``STATUS:done`` soft gate: the parts with no ``principles``
    note and the connects with no ``joint.params.leaf`` (``None`` joint
    counts). In-use template definitions and bought component/part
    blocks skip the check — instances of a template and purchased
    hardware don't each owe a note. Empty string means clean."""
    templates_in_use = {
        node.template
        for node in tree.blocks.values()
        if node.template is not None and "#" not in node.template
    }
    no_principles = [
        name
        for name in sorted(tree.blocks)
        if name not in templates_in_use
        and not _bought(tree, tree.blocks[name])
        and not _has_principles(tree, name)
    ]
    leafless = [
        f"{c.a_block}.{c.a_port}—{c.b_block}.{c.b_port}"
        for c in tree.connects
        if not ((c.joint or {}).get("params") or {}).get("leaf")
    ]
    if not no_principles and not leafless:
        return ""
    lines = ["finish check — warnings only; STATUS:done already landed:"]
    if no_principles:
        lines.append("- parts with no principles note: " + ", ".join(no_principles))
    if leafless:
        lines.append("- connects with no params.leaf: " + ", ".join(leafless))
    return "\n".join(lines)
