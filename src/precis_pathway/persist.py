"""Persist a autocatpath run artifact onto a `pathway` ref.

Shared by the two write paths so they stay identical:

* the handler's **in-process** `put` (slice 0), and
* the **`autocatpath_explore`** job dispatch (slice 1, runs on the pinned node).

Imports only precis's public Store surface (no autocatpath deps), so it is cheap to
import inside the job dispatcher.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from precis.store import Store

    from .types import PathwayArtifact

log = logging.getLogger(__name__)

BODY_KIND = "pathway_body"

#: The quest-candidate slug convention (``precis.quest.compute._candidate_slug``
#: mints ``q<quest_id>cand-<digest>`` and ``dispatch_autocatpath`` names the
#: pathway ``<cand>-rx-<key>``): a quest-minted pathway's candidate resolves
#: off its own slug even on refs that predate the ``meta.candidate_ref`` stamp.
_QUEST_CAND_SLUG_RE = re.compile(r"^q\d+cand-")

#: A pathway title is a one-line header — the catalyst slot is capped at
#: ~120 chars with an ellipsis (the full label stays on the structure
#: card, which has no length constraint).
_CATALYST_NAME_CAP = 120


def quest_minted_slug(slug: str | None) -> bool:
    """A quest-minted pathway's slug — ``q<quest_id>cand-<digest>-rx-…`` —
    the pre-``candidate_ref``-stamp mark (the stamp itself is the modern
    check; both name the barrier-lane mint)."""
    head = str(slug or "").split("-rx-", 1)[0]
    return bool(_QUEST_CAND_SLUG_RE.match(head))


def _json_finite(obj: Any) -> Any:
    """Recursively replace non-finite floats (NaN/Inf) with None.

    A NaN barrier (EMT can produce one for a toy reaction it has no parameters
    for) is valid Python but **invalid JSON** — psycopg's Jsonb write rejects it
    ("Token NaN is invalid"). NaN here means "no trustworthy value", so null is
    the faithful serialization; the views already render a missing value as n/a.
    """
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _json_finite(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_finite(v) for v in obj]
    return obj


def pathway_meta(
    artifact: PathwayArtifact, *, extra: dict[str, Any] | None = None
) -> dict[str, Any]:
    """The `refs.meta` payload for a pathway ref: the authoritative config +
    snapshot, the reaction graph, the pooled-uncertainty results, warnings."""
    meta: dict[str, Any] = {
        "content_key": artifact["content_key"],
        "autocatpath_version": artifact["autocatpath_version"],
        "config": artifact["config"],
        "config_snapshot_yaml": artifact["config_snapshot_yaml"],
        "results": artifact["results_json"],
        "graph": artifact["graph_json"],
        "warnings": artifact["warnings"],
        "n_structures": len(artifact["structures_extxyz"]),
        "status": "ready",
    }
    if extra:
        meta.update(extra)
    return _json_finite(meta)


def slab_name(slab: dict[str, Any] | None) -> str | None:
    """``<element>`` / ``<element>(<facet>)`` — the catalyst slot's fallback
    when no candidate structure names it. The facet renders only when
    ``slab.miller`` is set (a missing miller means 'Pd', not 'Pd(111)' —
    the config's default is an engine detail the title does not assert);
    negative indices take the bar-free comma form ('Pd(1,-1,1)').
    ``None`` when the config names no element at all — callers keep the
    existing title then."""
    slab = slab or {}
    element = slab.get("element")
    if not element:
        return None
    miller = slab.get("miller")
    if not miller:
        return str(element)
    sep = "," if any(int(i) < 0 or int(i) > 9 for i in miller) else ""
    return f"{element}({sep.join(str(int(i)) for i in miller)})"


def _related_structure_ids(store: Store, ref_id: int) -> list[int]:
    """ref_ids of ``structure`` refs linked ``related-to`` this pathway —
    the same symmetric read ``links_for`` does everywhere. The per-state
    intermediates ride this relation too, so several ids means the set
    does not single out a candidate."""
    others = {
        ln.dst_ref_id if ln.src_ref_id == ref_id else ln.src_ref_id
        for ln in store.links_for(ref_id, direction="both", relation="related-to")
    }
    others.discard(ref_id)
    if not others:
        return []
    refs = store.fetch_refs_by_ids(others)
    return sorted(rid for rid, r in refs.items() if r.kind == "structure")


def _candidate_structure(
    store: Store,
    meta: dict[str, Any],
    slug: str | None,
    *,
    ref_id: int | None = None,
    structs: dict[int, Any] | None = None,
    related: dict[int, set[int]] | None = None,
    conn: Any = None,
) -> tuple[Any | None, bool, str | None]:
    """``(structure ref | None, named, note)`` — the structure a pathway
    ran on, whether one was identified at all, and an ambiguity note for
    the retitle dry-run.

    The pathway's own candidate link is the source of truth, in order:
    the mint-stamped ``meta.candidate_ref`` (what the handler/web views
    already resolve); when that is absent, the pathway's ``related-to``
    structure link (the quest harvest's ``_link_pathway`` — exactly one
    linked structure IS the candidate; several name none, since the
    per-state intermediates ride the same relation); then the slug's
    ``q<quest>cand-<digest>`` prefix for pathways minted before the stamp.
    Tombstones resolve too — a deleted candidate still names the catalyst.
    ``(None, True, _)`` means a candidate was named but is gone without a
    tombstone.
    """
    struct = None
    named = False
    note = None
    cand_id = (meta or {}).get("candidate_ref")
    if cand_id is not None:
        named = True
        try:
            cid = int(cand_id)
        except (TypeError, ValueError):
            cid = None
        if cid is not None:
            struct = (
                structs.get(cid)
                if structs is not None
                else store.get_ref(
                    kind="structure", id=cid, include_deleted=True, conn=conn
                )
            )
    elif ref_id is not None:
        ids = (
            sorted(related.get(ref_id, ()))
            if related is not None
            else _related_structure_ids(store, ref_id)
        )
        if len(ids) == 1:
            named = True
            struct = (
                structs.get(ids[0])
                if structs is not None
                else store.get_ref(
                    kind="structure", id=ids[0], include_deleted=True, conn=conn
                )
            )
        elif len(ids) > 1:
            note = f"ambiguous: {len(ids)} related-to structures"
    if struct is None and slug:
        head = str(slug).split("-rx-", 1)[0]
        if _QUEST_CAND_SLUG_RE.match(head):
            struct = store.get_ref(
                kind="structure", id=head, include_deleted=True, conn=conn
            )
            if struct is not None:
                named = True
    return struct, named, note


def _catalyst_name(store: Store, struct: Any | None, *, conn: Any = None) -> str | None:
    """``'<name>'`` / ``'<name> (deleted)'`` for the candidate structure —
    the readable catalyst label off its recorded construction (gr553416,
    :func:`precis.structure.catalyst_label.catalyst_label` over the
    ``design_revisions`` recipe + ``meta.params``), else the title with
    dashes rendered as spaces (gr549574) when no construction is
    recorded."""
    if struct is None:
        return None
    name = None
    try:
        # Local imports: persist's module surface stays cheap for the job
        # dispatcher (precis.structure pulls numpy/ase).
        from precis.design.history import recipe_ops
        from precis.structure.catalyst_label import catalyst_label

        name = catalyst_label(
            {
                "ops": recipe_ops(store, int(struct.id), conn=conn),
                "meta": getattr(struct, "meta", None) or {},
            }
        )
    except Exception:
        log.debug(
            "catalyst label failed for structure %s — title fallback",
            getattr(struct, "id", None),
            exc_info=True,
        )
    if not name:
        name = str(getattr(struct, "title", "") or "").replace("-", " ").strip()
    if not name:
        return None
    # A title is a one-line header: cap the catalyst slot (the card keeps
    # the full label).
    if len(name) > _CATALYST_NAME_CAP:
        name = name[: _CATALYST_NAME_CAP - 1].rstrip(" ,+") + "…"
    return f"{name} (deleted)" if getattr(struct, "retired_at", None) else name


def pathway_title(
    artifact: PathwayArtifact, *, catalyst: str | None = None
) -> str | None:
    r = artifact["results_json"]
    name = catalyst or slab_name(artifact["config"].get("slab"))
    if name is None:
        return None
    return f"{r['substrate']} → {r['target']} on {name}"


@dataclass(frozen=True)
class Retitle:
    """One pathway row in a retitle plan. ``new_title == old_title`` with
    a ``note`` is an informational keep (e.g. an ambiguous candidate
    link), not a change — apply skips it."""

    ref_id: int
    slug: str | None
    old_title: str
    new_title: str
    note: str | None = None


def stored_title(
    store: Store,
    meta: dict[str, Any],
    slug: str | None,
    *,
    ref_id: int | None = None,
    structs: dict[int, Any] | None = None,
    related: dict[int, set[int]] | None = None,
    conn: Any = None,
) -> tuple[str | None, str | None]:
    """``(title, note)`` — the spec title for a stored pathway
    (``'<substrate> → <target> on <catalyst>'`` off ``meta.results`` + the
    candidate link), and a dry-run note when the candidate evidence needs
    a human look. ``title=None`` keeps the stored title: no results yet,
    a named candidate gone without a tombstone, or no element to name the
    slab."""
    results = (meta or {}).get("results") or {}
    substrate, target = results.get("substrate"), results.get("target")
    if not substrate or not target:
        return None, None
    struct, named, note = _candidate_structure(
        store,
        meta or {},
        slug,
        ref_id=ref_id,
        structs=structs,
        related=related,
        conn=conn,
    )
    if struct is None and named:
        return None, note or "candidate named but gone, no tombstone"
    catalyst = _catalyst_name(store, struct, conn=conn) or slab_name(
        (meta or {}).get("config", {}).get("slab")
    )
    if catalyst is None:
        return None, "no slab element to name"
    return f"{substrate} → {target} on {catalyst}", note


def retitle_plan(store: Store) -> tuple[int, list[Retitle]]:
    """``(checked, rows)`` — every live pathway whose stored title differs
    from :func:`stored_title`, plus informational keeps (``note`` set,
    title unchanged). Title-only: no meta/body churn, so an apply is
    idempotent by construction."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT r.ref_id, r.title, r.meta, "
            "(SELECT ri.id_value FROM ref_identifiers ri "
            " WHERE ri.ref_id = r.ref_id AND ri.id_kind = 'cite_key' "
            " LIMIT 1) "
            "FROM refs r WHERE r.kind = 'pathway' AND r.retired_at IS NULL "
            "ORDER BY r.ref_id"
        ).fetchall()
        cand_ids = set()
        unlinked = []
        for ref_id, _, meta, _ in rows:
            cid = (meta or {}).get("candidate_ref")
            if cid is not None:
                try:
                    cand_ids.add(int(cid))
                except (TypeError, ValueError):
                    pass
            else:
                unlinked.append(int(ref_id))
        # One batched related-to read for every pathway that names no
        # candidate — the quest harvest's structure↔pathway link (either
        # direction, the relation is symmetric), structure ends only.
        related: dict[int, set[int]] = {}
        if unlinked:
            for pw_id, struct_id in conn.execute(
                "SELECT l.src_ref_id, l.dst_ref_id FROM links l "
                "JOIN refs r ON r.ref_id = l.dst_ref_id "
                "WHERE l.relation = 'related-to' AND l.src_ref_id = ANY(%s) "
                "AND r.kind = 'structure' "
                "UNION ALL "
                "SELECT l.dst_ref_id, l.src_ref_id FROM links l "
                "JOIN refs r ON r.ref_id = l.src_ref_id "
                "WHERE l.relation = 'related-to' AND l.dst_ref_id = ANY(%s) "
                "AND r.kind = 'structure'",
                (unlinked, unlinked),
            ).fetchall():
                related.setdefault(int(pw_id), set()).add(int(struct_id))
    structs = store.fetch_refs_by_ids(
        cand_ids | {sid for ids in related.values() for sid in ids}
    )
    changes = []
    for ref_id, title, meta, slug in rows:
        old = str(title or "")
        new, note = stored_title(
            store,
            meta or {},
            slug,
            ref_id=int(ref_id),
            structs=structs,
            related=related,
        )
        if (new is not None and new != old) or note is not None:
            changes.append(
                Retitle(
                    ref_id=int(ref_id),
                    slug=slug,
                    old_title=old,
                    new_title=new if new is not None else old,
                    note=note,
                )
            )
    return len(rows), changes


def persist_result(
    store: Store,
    ref_id: int,
    artifact: PathwayArtifact,
    *,
    pathway_slug: str | None = None,
    ingest: bool = True,
    extra_meta: dict[str, Any] | None = None,
    conn: Any = None,
) -> None:
    """Stamp the pathway ref's meta and (re)write its methods body chunk, and
    (slice 1b) ingest each relaxed intermediate as a `structure` ref linked back
    to the pathway. The ref must already exist. Runs in its own transaction
    unless a `conn` is supplied.

    Structure ingest runs *before* the meta stamp (it opens its own
    transactions via `structure_save`) so the resulting `{state → ref_id}` map
    lands in the same meta. Best-effort — an ingest failure never blocks the
    core result write-back."""
    extra = dict(extra_meta or {})
    if ingest and pathway_slug and not artifact.get("structures_extxyz"):
        # The gate below silently skipping is exactly how the fan-out era
        # shipped geometry-less pathways for ten days (the seed→aggregate
        # glue dropped the ``structures`` key; 2026-08-13). An ingest-enabled
        # persist with nothing to ingest is always worth a loud line.
        log.warning(
            "pathway %s: artifact carries no structures_extxyz — "
            "structure_refs will not be set (per-state geometry lost "
            "upstream?)",
            pathway_slug,
        )
    if ingest and pathway_slug and artifact.get("structures_extxyz"):
        try:
            from .ingest import ingest_intermediates

            extra["structure_refs"] = ingest_intermediates(
                store,
                ref_id,
                pathway_slug,
                artifact["content_key"],
                artifact["structures_extxyz"],
            )
        except Exception:
            # Native ingest is additive — keep the pathway result regardless —
            # but never silently: a swallowed failure here is exactly how
            # per-state geometry went missing unnoticed (empty structure_refs
            # with no trace). Log loudly so a regression surfaces.
            log.warning(
                "pathway %s: per-state structure ingest failed; "
                "structure_refs left unset",
                pathway_slug,
                exc_info=True,
            )

    meta = pathway_meta(artifact, extra=extra)

    def _do(c: Any) -> None:
        store.stamp_ref_meta(ref_id, meta, conn=c)
        # The dispatched-job path seeds the ref as ``pathway <slug>
        # (computing)`` because no artifact exists yet, and until 2026-09-30
        # nothing ever rewrote it: 697 of prod's 701 pathway refs carried a
        # "(computing)" title, including all 528 that were ``status: ready``.
        # An agent listing pathways reads the title, so every finished run
        # advertised itself as still running.
        #
        # The catalyst slot names the candidate structure when the pathway
        # has one (gr549574): the mint-stamped ``meta.candidate_ref`` the
        # just-written merge preserves, else the related-to link, else the
        # slug's candidate prefix. A named candidate gone without a
        # tombstone (or a config naming no element) keeps the existing
        # title — the same keep rule ``stored_title`` applies.
        ref = store.get_ref(kind="pathway", id=ref_id, conn=c)
        struct, named, _note = _candidate_structure(
            store,
            (ref.meta if ref is not None else meta),
            (ref.slug if ref is not None else None) or pathway_slug,
            ref_id=ref_id,
            conn=c,
        )
        title = (
            pathway_title(artifact, catalyst=_catalyst_name(store, struct, conn=c))
            if struct is not None or not named
            else None
        )
        if title is not None:
            store.update_ref(ref_id, title=title, conn=c)
        store.chunks.replace_body_chunk(
            ref_id, artifact["methods_md"], chunk_kind=BODY_KIND, conn=c
        )

    if conn is not None:
        _do(conn)
    else:
        with store.tx() as c:
            _do(c)
