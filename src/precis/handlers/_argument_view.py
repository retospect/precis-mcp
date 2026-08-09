"""``get(kind='memory', id=<inference|lemma>, view='argument')``.

The read-time half of the kind-scoped argument-graph walk (the write-time
half — the retraction push hook — lives in
:mod:`precis.store._argument_ops`, which this module deliberately does
*not* share code with: that module walks raw ``Connection`` rows inside a
link-write transaction; this one renders through the ordinary
:class:`~precis.store.Store` API, one small tree at a time, modelled on
``FindingHandler._render_one``'s begat style and
``QuestHandler._render_tree``'s recursive walk).

Kind-scoped exactly like the push hook: only ``finding`` and ``memory``
tagged the open tag ``kind:lemma`` / ``kind:inference`` count as graph
nodes, so a premise edge (``derived-from`` *into* a ``kind:inference``) is
never confused with unrelated ``derived-from`` provenance.

Two flag passes, both pure graph walks — no text reading:

* **stale-premise** — a premise cites a paper carrying an inbound
  ``retracts`` / ``raises-concern-about`` edge. (The system-set
  ``STALE:retracted-premise`` tag on the *inference* itself, set by the
  write-time push hook, is rendered too — this is the read-time backstop
  for arguments built *after* the retraction, before the next ripple.)
* **inherited-caveat** — a caveat (``memory`` tagged ``kind:caveat``)
  reaches a premise via ``qualified-by``, listed *"inherited — confirm
  still addressed"* (never auto-discharged — the argument graph).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from precis.errors import BadInput
from precis.response import Response
from precis.utils import handle_registry

if TYPE_CHECKING:
    from precis.store import Ref, Store
    from precis.store.types import Relation

#: Recursion guard — mirrors ``QuestHandler._MAX_TREE_DEPTH``. The
#: argument graph is sparse by design; this is a
#: defensive cap, not a real ceiling on legitimate chains.
_MAX_DEPTH = 6

_RETRACTION_CHECK_RELATIONS: tuple[Relation, ...] = (
    "retracted-by",
    "concern-raised-by",
)


def _handle(kind: str, ref_id: int) -> str:
    return handle_registry.try_format(kind, ref_id) or f"{kind}:{ref_id}"


def _subkind(store: Store, ref_id: int) -> str | None:
    """The ``kind:<x>`` open-tag value on a memory ref, or ``None``."""
    for t in store.tags_for(ref_id):
        s = str(t)
        if s.startswith("kind:"):
            return s.split(":", 1)[1]
    return None


def classify_node(store: Store, ref: Ref) -> tuple[str, str | None] | None:
    """``(kind, subkind)`` for a walkable argument-graph node, or ``None``.

    ``subkind`` is the memory's ``kind:lemma`` / ``kind:inference`` /
    ``kind:caveat`` tag value; ``None`` for a ``finding`` (no sub-kind) or
    when the memory carries none of the three tags.
    """
    if ref.kind == "finding":
        return ("finding", None)
    if ref.kind != "memory":
        return None
    sub = _subkind(store, ref.id)
    if sub in ("lemma", "inference", "caveat"):
        return ("memory", sub)
    return None


def _is_premise_node(cls: tuple[str, str | None] | None) -> bool:
    return cls is not None and (cls[0] == "finding" or cls[1] == "lemma")


def _is_distrusted(store: Store, ref_id: int) -> bool:
    """Does ``ref_id`` carry an inbound ``retracts`` /
    ``raises-concern-about`` edge (either physical write direction)?

    One ``links_for`` call per relation covers both forms: the inverse
    rewrite (``relations.inverse_slug``) matches the opposite direction +
    relation automatically, so ``direction='out', relation='retracted-by'``
    also matches an inbound ``retracts`` row.
    """
    for rel in _RETRACTION_CHECK_RELATIONS:
        if store.links_for(ref_id, direction="out", relation=rel):
            return True
    return False


def _premise_cites_distrusted(store: Store, premise_ref_id: int) -> bool:
    """Does this premise cite (any outbound link to) a distrusted ref?"""
    for link in store.links_for(premise_ref_id, direction="out"):
        if _is_distrusted(store, link.dst_ref_id):
            return True
    return False


def _inherited_caveats(store: Store, premise_ref_id: int) -> list[Ref]:
    """Caveat refs reachable via this premise's ``qualified-by`` edge.

    ``links_for`` returns the row as physically stored, not rewritten to
    the queried logical relation — so which endpoint is "the caveat"
    depends on which physical direction actually matched: the canonical
    write is ``caveat --qualifies--> claim`` (src=caveat), but a caller
    that wrote the edge the other way (``claim --qualified-by-->
    caveat``, dst=caveat) is equally valid. Disambiguate on
    ``link.relation`` rather than assuming one direction.
    """
    caveats: list[Ref] = []
    for link in store.links_for(
        premise_ref_id, direction="out", relation="qualified-by"
    ):
        caveat_ref_id = (
            link.dst_ref_id if link.relation == "qualified-by" else link.src_ref_id
        )
        caveat_ref = _fetch_any(store, caveat_ref_id)
        if caveat_ref is not None:
            caveats.append(caveat_ref)
    return caveats


def _fetch_any(store: Store, ref_id: int) -> Ref | None:
    """Look up a ref by id without knowing its kind (mirrors
    ``FindingHandler._fetch_ref_any_kind``)."""
    from precis.store._mappers import _REFS_COLS, _row_to_ref

    with store.pool.connection() as conn:
        row = conn.execute(
            f"SELECT {_REFS_COLS} FROM refs WHERE ref_id = %s AND deleted_at IS NULL",
            (ref_id,),
        ).fetchone()
    return _row_to_ref(row) if row is not None else None


def _title_of(ref: Ref) -> str:
    return (ref.title or "").splitlines()[0][:100] if ref.title else "(untitled)"


def _premise_line(store: Store, premise_id: int, *, indent: str) -> list[str]:
    ref = _fetch_any(store, premise_id)
    if ref is None:
        return [f"{indent}- {premise_id} (missing)"]
    handle = _handle(ref.kind, ref.id)
    flags: list[str] = []
    if _premise_cites_distrusted(store, premise_id):
        flags.append("STALE-SOURCE")
    flag_str = f" [{', '.join(flags)}]" if flags else ""
    lines = [f"{indent}- {handle} ({ref.kind}): {_title_of(ref)}{flag_str}"]
    for caveat in _inherited_caveats(store, premise_id):
        caveat_handle = _handle(caveat.kind, caveat.id)
        lines.append(
            f"{indent}    caveat {caveat_handle}: {_title_of(caveat)} "
            "— inherited, confirm still addressed"
        )
    return lines


def _render_inference(
    store: Store, ref: Ref, *, depth: int, visited: set[int]
) -> list[str]:
    visited.add(ref.id)
    indent = "  " * depth
    handle = _handle("memory", ref.id)
    meta = ref.meta or {}
    tags = store.tags_for(ref.id)
    stale_tagged = any(str(t) == "STALE:retracted-premise" for t in tags)
    stale_str = "  [STALE:retracted-premise]" if stale_tagged else ""
    lines = [f"{indent}◆ {handle}: {_title_of(ref)}{stale_str}"]
    rule = meta.get("rule")
    warrant = meta.get("warrant")
    if rule:
        lines.append(f"{indent}  rule: {rule}")
    if warrant:
        lines.append(f"{indent}  warrant: {warrant}")

    premise_links = store.links_for(ref.id, direction="out", relation="derived-from")
    premise_refs: list[Ref] = []
    for link in premise_links:
        p_ref = _fetch_any(store, link.dst_ref_id)
        if p_ref is not None and _is_premise_node(classify_node(store, p_ref)):
            premise_refs.append(p_ref)

    if premise_refs:
        lines.append(f"{indent}  premises:")
        for premise_ref in premise_refs:
            lines.extend(_premise_line(store, premise_ref.id, indent=indent + "    "))
            # Recurse into an upstream inference when this premise is
            # itself a reused conclusion lemma (proof-tree depth).
            if (
                classify_node(store, premise_ref) == ("memory", "lemma")
                and depth + 1 < _MAX_DEPTH
            ):
                for up_link in store.links_for(
                    premise_ref.id, direction="in", relation="entails"
                ):
                    if up_link.src_ref_id in visited:
                        continue
                    up_ref = _fetch_any(store, up_link.src_ref_id)
                    if up_ref is None:
                        continue
                    lines.append(f"{indent}      ↳ entailed by:")
                    lines.append(
                        "\n".join(
                            _render_inference(
                                store, up_ref, depth=depth + 3, visited=visited
                            )
                        )
                    )
    else:
        lines.append(f"{indent}  premises: (none)")

    conclusions = store.links_for(ref.id, direction="out", relation="entails")
    if conclusions:
        lines.append(f"{indent}  conclusion:")
        for link in conclusions:
            concl_ref = _fetch_any(store, link.dst_ref_id)
            if concl_ref is None:
                continue
            concl_handle = _handle(concl_ref.kind, concl_ref.id)
            lines.append(f"{indent}    → {concl_handle}: {_title_of(concl_ref)}")
    return lines


def render_argument_view(store: Store, ref: Ref) -> Response:
    """Render ``view='argument'`` for a ``memory`` id.

    Applies to ``kind:inference`` (the proof tree: premises → this step →
    conclusion) and ``kind:lemma`` (rendered as the inference(s) that
    entail it, recursed the same way). Any other memory raises — the view
    only makes sense on an argument-graph node.
    """
    cls = classify_node(store, ref)
    if cls is None or cls[0] != "memory" or cls[1] not in ("lemma", "inference"):
        raise BadInput(
            f"view='argument' only applies to a memory tagged "
            f"'kind:lemma' or 'kind:inference' (id={ref.id} has neither)",
            next=(
                "tag(kind='memory', id="
                f"{ref.id}, add=['kind:inference']) if this is a reasoning "
                "step, or see get(kind='skill', id='precis-argument-help')"
            ),
        )

    header = f"# argument — {_handle('memory', ref.id)}: {_title_of(ref)}"
    if cls[1] == "inference":
        body_lines = [
            header,
            *_render_inference(store, ref, depth=0, visited=set())[1:],
        ]
        # _render_inference's own first line duplicates the header info at
        # depth 0 — drop it in favour of the explicit "# argument —" header
        # above, then keep the rest (rule/warrant/premises/conclusion).
        return Response(body="\n".join(body_lines))

    # kind:lemma — show the inference(s) that entail it (the "what
    # produced this claim?" direction), recursing the same walk.
    lines = [header]
    upstream = store.links_for(ref.id, direction="in", relation="entails")
    if not upstream:
        lines.append("")
        lines.append("(not yet entailed by any inference)")
    for link in upstream:
        up_ref = _fetch_any(store, link.src_ref_id)
        if up_ref is None:
            continue
        lines.append("")
        lines.extend(_render_inference(store, up_ref, depth=0, visited={ref.id}))
    caveats = _inherited_caveats(store, ref.id)
    if caveats:
        lines.append("")
        lines.append("caveats:")
        for caveat in caveats:
            caveat_handle = _handle(caveat.kind, caveat.id)
            lines.append(
                f"  {caveat_handle}: {_title_of(caveat)} — inherited, "
                "confirm still addressed"
            )
    return Response(body="\n".join(lines))


__all__ = ["classify_node", "render_argument_view"]
