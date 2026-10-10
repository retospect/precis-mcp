"""Per-kind eye render — an eye's *neighborhood* depends on its
kind, so the ladder generalizes but its shape does not:

- **Tree kinds** (``draft`` / ``plan``): reading-order neighborhood — the
  :func:`precis.utils.fisheye.render_fisheye` span + reference ring.
- **Doc kinds** (``paper`` / ``patent`` / ``web`` / ``datasheet`` / ``cfp``): a
  long ingested document with no heading tree, so its structure *is* the
  per-chunk KeyBERT clustering. The eye renders that dynamic
  **keyword-cluster TOC** around the eyeball — similar chunks grouped for
  separate exploration:

  * A **whole-doc eye** (``pa5``) is the cluster *map*: one row per cluster,
    keyed by its lead **chunk handle** ``pc<id>`` + keyword label — a skimmable
    shape you drill by placing an eye on a ``pc`` handle. A whole-doc eye
    never spills verbatim text; reading real text is always a deliberate
    drill to a chunk eye.
  * A **chunk eye** (``pc13234``) is a fisheye *within* its cluster: the chunks
    before it and after it as summary lines (each its own ``pc`` handle to drill),
    the eye chunk itself verbatim (or a summary at ``summary``), and every *other*
    cluster collapsed to a one-line label. So focusing a chunk opens its
    neighborhood and leaves the rest of the paper as a drillable map.

  Everything is addressed by its universal ``pc<id>`` handle — the
  legacy ``slug~pos`` form is never emitted here.
- **Link kinds** (``memory`` / ``finding`` / …): the ref renders as its note
  (title → gist → body), and at ``fisheye+1hop`` it grows its **link
  neighborhood** — every ref linked to it, **either direction**, with the
  relation as it reads from this side (``serves`` out, ``served-by`` in),
  under one heading per ring group (``refeye.RING_GROUPS``: claim graph,
  roadmap, taxonomy, concepts, parts, argument, notes) and capped per
  label. A note linked to a paper surfaces when you fisheye the paper (via
  the doc eye's ring) and the paper surfaces when you fisheye the note.

- **Skill eyes** (``sk:<slug>``): a skill is file-backed, not refs-backed, so
  it has no numeric pk for ``handle_registry.parse``'s decimal grammar
  (keeps ``skill`` on its existing slug addressing rather than
  folding it into the registry — see that module's docstring). A skill eye
  is dispatched on its ``sk:`` prefix ahead of the decimal parse and renders
  straight from the skill corpus (``handlers.skill``'s own accessors —
  ``_load_skill`` / ``_skill_title``, the same ones ``SkillHandler.get``
  uses). It's **atomic**: no fisheye/1hop neighborhood (a skill has no
  corpus position to be a neighbor of) — ``toc``/``none`` collapse to a
  one-line bookmark, anything richer is the verbatim body.

Reached from every kind's ``get`` through one door: ``get(kind=K, id=…,
extent=<rung>)`` (or a ladder label in ``view=``) is routed by the
dispatcher to :meth:`precis.protocol.Handler.eye`, whose default resolves
``(kind, id)`` to a handle via :mod:`precis.handlers._eye` and calls
:func:`render_eye`; a kind with no graph node (a codeless provider, a
file-backed kind, ``tag``, a whole draft/plan) answers ``Unsupported``
there with the reason in one sentence. The browser form is
``/eye/<handle>`` (``precis_web.routes.eye``). The composer
(:func:`precis.utils.working_set_render.render_working_set`) also
dispatches non-tree eyes here, for multi-eye working-set assembly
(planner/dream passes) — that composer's own eye-placement loop is
worker-internal, not an agent-facing verb.
"""

from __future__ import annotations

import logging
import weakref
from typing import TYPE_CHECKING, Any

from precis.utils import handle_registry
from precis.utils.refeye import RING_GROUPS, ring_group
from precis.workers.working_set import Extent

if TYPE_CHECKING:
    from precis.store.store import Store

#: Reading-order tree kinds — routed to the spatial fisheye.
_TREE_KINDS: frozenset[str] = frozenset({"draft", "plan"})

#: Long ingested documents whose structure is per-chunk KeyBERT clustering
#: rather than a heading tree — routed to the keyword-cluster fisheye.
#:
#: NOT a pure ``KindSpec.corpus_role`` derivation (kept hand-maintained on
#: purpose, pinned by ``tests/test_kind_totality.py``): ``web`` carries no
#: ``corpus_role`` (it's a fetched-cache provider, not a document-family
#: kind) yet legitimately belongs here — a scraped page has body chunks and
#: no heading tree, same as a paper. A pure ``corpus_role in ("evidence",
#: "spec")`` derivation would therefore *drop* ``web`` from this set, which
#: is exactly the dangerous direction ("derived set removes a hand-
#: maintained member") that stays a hand call rather than an auto-swap.
#: ``edgar`` is here by the matching human call: a long SEC filing with
#: section-labelled body chunks is the same shape as a patent, so an
#: ``eg<id>`` eye gets the cluster-TOC renderer, not the link-kind note one.
_DOC_KINDS: frozenset[str] = frozenset(
    {"paper", "patent", "web", "datasheet", "cfp", "edgar"}
)

_SUMMARY_CAP = 300
log = logging.getLogger(__name__)

_VERBATIM_CAP = 4000
_NEIGHBOR_TITLE_CAP = 80
#: Per-relation cap on the ``fisheye+1hop`` link neighborhood — a claim hub
#: can carry dozens of evidence edges, so a flat uncapped dump per relation
#: group is replaced by this cap plus a visible overflow line (no silent
#: truncation).
_NEIGHBOR_GROUP_CAP = 8
_CHUNK_SUMMARY_CAP = 140
#: Keep the cluster map / label lists skimmable even on a huge doc; the
#: clusterer already caps the top level, this bounds the collapsed labels.
_MAP_CLUSTER_CAP = 20

#: Forward-biased summary window *within* the eye's home cluster (mirrors the
#: draft fisheye's falloff): a keyword-homogeneous section can cluster into
#: dozens of chunks, so show the eye's neighbours and collapse the far tail
#: to a ``⋯ N more ⋯`` marker rather than dumping the whole section.
_HOME_BACK = 6
_HOME_FWD = 12

#: Prefix marking a skill eye's handle. ``handle_registry.parse`` only
#: decodes ``<2-char code><digits>`` and ``skill`` has no numeric pk (file-
#: backed, slug-addressed — see that module's docstring), so a skill eye
#: uses its own ``sk:<slug>`` shape and is dispatched here, ahead of the
#: decimal parse, rather than folded into the registry's grammar.
_SKILL_HANDLE_PREFIX = handle_registry.code_for_kind("skill") + ":"


def render_eye(
    # store stays Any: tests pass a hand-rolled fake narrower than Store
    # (test_skill_eye_* pass None for the skill-eye path, which never
    # touches store)
    store: Any,
    handle: str,
    extent: Extent | str | int,
    *,
    q: str | None = None,
) -> str:
    """Render one eye by its kind's neighborhood strategy. Raises ``ValueError``
    if the handle does not resolve to a live ref/chunk, or asks a rung its
    kind does not have.

    ``extent`` is a ladder rung, optionally suffixed ``+recall``
    (``fisheye+1hop+recall``); a bare ``+recall`` is ``fisheye+1hop+recall``.
    ``q='<kind>:<label>'`` at ``fisheye+2hop`` expands one second-hop group
    (:func:`_second_hop`); ``q='<label>'`` (no colon) at ``fisheye+1hop`` or
    above lists one first-hop group uncapped (:func:`_expand_first_hop`) — the
    call a ``… +N more`` line carries."""
    ext, recall = parse_extent(extent)
    if q is not None and (ext < Extent.HOP1 or (ext < Extent.HOP2 and ":" in q)):
        raise ValueError(
            "eye: q='<label>' lists one first-hop group at fisheye+1hop; "
            "q='<kind>:<label>' expands a second-hop group at fisheye+2hop"
        )
    if handle.startswith(_SKILL_HANDLE_PREFIX):
        return _render_skill_eye(handle, ext)
    parsed = handle_registry.parse(handle)
    if parsed is None:
        raise ValueError(f"eye: unresolvable handle {handle!r}")
    kind, is_chunk, pk = parsed
    if kind in _TREE_KINDS:
        if ext > Extent.HOP1 or recall:
            raise ValueError(
                f"eye: {kind} sections stop at fisheye+1hop — the reference "
                "ring's entries are refs; fisheye one of those to walk further"
            )
        from precis.utils.fisheye import render_fisheye

        return render_fisheye(store, kind=kind, handle=handle, extent=ext)
    if kind in _DOC_KINDS:
        return _render_doc_eye(
            store, handle, kind, ext, is_chunk=is_chunk, recall=recall, expand=q
        )
    return _render_note_eye(store, handle, kind, ext, recall=recall, expand=q)


#: The suffix that adds the similarity rung to any extent.
RECALL_SUFFIX = "+recall"


def parse_extent(extent: Extent | str | int) -> tuple[Extent, bool]:
    """``(rung, recall)`` from an extent value — the ladder rung plus whether
    the ``+recall`` suffix was given. A bare ``+recall`` sits on top of
    ``fisheye+1hop``: recall without the edges it complements reads as if
    similarity were the neighbourhood. Raises ``ValueError`` on an unknown
    rung, like :meth:`Extent.parse`."""
    recall = isinstance(extent, str) and extent.strip().lower().endswith(RECALL_SUFFIX)
    rung: Extent | str | int = extent
    if recall:
        rung = str(extent).strip()[: -len(RECALL_SUFFIX)] or Extent.HOP1
    try:
        return Extent.parse(rung), recall
    except (KeyError, ValueError) as e:
        raise ValueError(f"eye: unknown extent {extent!r}") from e


# ── shared helpers ───────────────────────────────────────────────────


def _resolve_ref(store: Store, handle: str) -> Any:
    r = store.resolve_handle(handle)
    if r is None:
        return None
    rid = int(r.ref_id)
    return store.fetch_refs_by_ids([rid]).get(rid)


def _mirror_name(ref: Any) -> str:
    """``meta.file_mirror.filename`` of a mirrored repo-dev memory, else ''."""
    mirror = (getattr(ref, "meta", None) or {}).get("file_mirror")
    name = mirror.get("filename") if isinstance(mirror, dict) else None
    return name if isinstance(name, str) else ""


def _handle_with_file(kind: str, ref: Any, ref_id: int) -> str:
    """The handle, with the mirror filename beside it when the ref has one:
    ``me4641 (worker_busy_vs_starved_diagnosis.md)``. A code-less kind
    (``handle_registry.CODELESS_KINDS``: websearch, web, perplexity-*, …)
    is addressed by ``<kind>:<slug>``, the form ``get`` resolves, so a memory
    whose ring cites a websearch cache renders instead of raising
    (gr476896)."""
    hid = handle_registry.try_format(kind, ref_id)
    if hid is None:
        hid = f"{kind}:{getattr(ref, 'slug', None) or ref_id}"
    name = _mirror_name(ref)
    return f"{hid} ({name})" if name else hid


def _head(ref: Any, kind: str) -> str:
    hid = _handle_with_file(kind, ref, int(ref.id))
    title = " ".join((getattr(ref, "title", None) or "").split())
    return f"{hid} [{kind}] {title}".rstrip()


def _cap(text: str, cap: int) -> str:
    t = (text or "").strip()
    return t if len(t) <= cap else t[: cap - 1].rstrip() + "…"


# ── skill kind: file-backed, atomic verbatim (no neighborhood) ────────


def _render_skill_eye(handle: str, ext: Extent) -> str:
    """A skill eye (``sk:<slug>``): file-backed, so it renders straight from
    the skill corpus via ``handlers.skill``'s own accessors — the same ones
    ``SkillHandler.get`` uses — rather than a store round trip. No fisheye /
    1hop ring: a skill has no corpus position to be a neighbor of, so the
    ladder collapses to two rungs — ``toc``/``none`` render a one-line
    bookmark, anything richer renders the verbatim body (capped like any
    other eye)."""
    from precis.handlers.skill import _load_skill, _skill_title

    slug = handle[len(_SKILL_HANDLE_PREFIX) :]
    text = _load_skill(slug)
    if text is None:
        raise ValueError(f"eye: no live skill for {handle!r}")
    title = _skill_title(slug) or slug
    head = f"{handle} [skill] {title}".rstrip()
    if ext <= Extent.TOC:
        return f"· {head}"
    return f"{head}\n{_cap(text, _VERBATIM_CAP)}"


# ── doc kinds: the keyword-cluster fisheye (paper / patent / web / …) ──


def _chunk_handle(kind: str, block: Any) -> str:
    """The block's universal ``pc<id>`` chunk handle."""
    return handle_registry.format_handle(kind, int(block.id), chunk=True)


def _chunk_summary(block: Any) -> str:
    """A one-line summary for a chunk: its KeyBERT keywords, else its first line
    of text, whitespace-collapsed and capped."""
    kws = block.keywords or []
    if kws:
        return _cap(", ".join(kws), _CHUNK_SUMMARY_CAP)
    return _cap(" ".join((block.text or "").split()), _CHUNK_SUMMARY_CAP)


def _cluster_label(kind: str, bucket: list[Any], kws: list[str]) -> str:
    """One collapsed label line for a cluster — its lead ``pc`` handle, the span
    size, and the keyword label. Drill it by focusing the handle."""
    lead = _chunk_handle(kind, bucket[0])
    span = f" +{len(bucket) - 1}" if len(bucket) > 1 else ""
    label = ", ".join(kws) or _chunk_summary(bucket[0]) or "…"
    return f"  · {lead}{span}  {_cap(label, _CHUNK_SUMMARY_CAP)}"


def _cluster_map(kind: str, clusters: list[tuple[list[Any], list[str]]]) -> str:
    """A whole-doc eye: the cluster map — one label row per cluster, no
    verbatim text. Drill any cluster by focusing its ``pc`` handle."""
    shown = clusters[:_MAP_CLUSTER_CAP]
    lines = [f"— {len(clusters)} clusters (focus a pc handle to open one) —"]
    lines.extend(_cluster_label(kind, bucket, kws) for bucket, kws in shown)
    if len(clusters) > len(shown):
        lines.append(f"  +{len(clusters) - len(shown)} more clusters")
    return "\n".join(lines)


def _fisheye_split(
    kind: str,
    clusters: list[tuple[list[Any], list[str]]],
    eye_ord: int,
    ext: Extent,
) -> str:
    """A chunk eye: the fisheye *within* its cluster. Other clusters collapse to
    labels; the home cluster splits into before-chunks (summary) / the eye chunk
    (verbatim, or a summary at ``summary``) / after-chunks (summary) — each chunk
    its own ``pc`` handle to drill next."""
    home = next(
        (
            i
            for i, (bucket, _) in enumerate(clusters)
            if bucket and bucket[0].ord <= eye_ord <= bucket[-1].ord
        ),
        None,
    )
    if home is None:  # eye ord fell outside every cluster — degrade to the map
        return _cluster_map(kind, clusters)

    lines: list[str] = []
    for bucket, kws in clusters[:home]:
        lines.append(_cluster_label(kind, bucket, kws))

    home_bucket, _kws = clusters[home]
    lines.append("— cluster —")
    # A forward-biased window around the eye within its cluster; the far tail of
    # a big keyword-homogeneous section collapses rather than dumping every line.
    eye_i = next((i for i, b in enumerate(home_bucket) if b.ord == eye_ord), 0)
    lo = max(0, eye_i - _HOME_BACK)
    hi = min(len(home_bucket), eye_i + _HOME_FWD + 1)
    if lo > 0:
        lines.append(f"  ⋯ {lo} more ⋯")
    for b in home_bucket[lo:hi]:
        h = _chunk_handle(kind, b)
        if b.ord == eye_ord:
            if ext is Extent.SUMMARY:
                lines.append(f"▸ {h} [{b.chunk_kind}]  {_chunk_summary(b)}")
            else:
                lines.append(f"▸ {h} [{b.chunk_kind}]\n{_cap(b.text, _VERBATIM_CAP)}")
        else:
            lines.append(f"  · {h}  {_chunk_summary(b)}")
    if hi < len(home_bucket):
        lines.append(f"  ⋯ {len(home_bucket) - hi} more ⋯")

    for bucket, kws in clusters[home + 1 :]:
        lines.append(_cluster_label(kind, bucket, kws))
    return "\n".join(lines)


def _render_doc_eye(
    store: Store,
    handle: str,
    kind: str,
    ext: Extent,
    *,
    is_chunk: bool,
    recall: bool = False,
    expand: str | None = None,
) -> str:
    """A doc-kind eye (paper / patent / web / …): the dynamic keyword-cluster TOC
    around the eyeball. A whole-doc handle renders the cluster map; a ``pc``
    chunk handle renders the fisheye split within its cluster. ``fisheye+1hop``
    appends the ref's symmetric link ring."""
    rh = store.resolve_handle(handle)
    if rh is None:
        raise ValueError(f"eye: no live {kind} for {handle!r}")
    ref_id = int(rh.ref_id)
    ref = store.fetch_refs_by_ids([ref_id]).get(ref_id)
    if ref is None or getattr(ref, "retired_at", None) is not None:
        raise ValueError(f"eye: no live {kind} ref for {handle!r}")
    head = _head(ref, kind)
    if ext <= Extent.TOC:
        return f"· {head}"

    blocks = store.chunks.list_chunks_for_ref(ref_id)
    if blocks:
        from precis.utils.toc_db import cluster_blocks

        clusters = cluster_blocks(blocks)
        if is_chunk and rh.chunk_ord is not None:
            body = _fisheye_split(kind, clusters, int(rh.chunk_ord), ext)
        else:
            body = _cluster_map(kind, clusters)
        block = f"{head}\n{body}"
    else:
        block = head  # no body chunks yet — head alone, but the ring still shows

    # The reference ring is a property of the ref, not its body — an empty
    # paper linked to a note still surfaces that note at fisheye+1hop.
    sections = _rings(store, ref_id, kind, ext, recall=recall, expand=expand)
    return "\n\n".join([block, *sections])


# ── link kinds: the note + its link graph (memory / finding / …) ──────


def _ordered_body(store: Store, ref_id: int, *, cap: int) -> tuple[str, int]:
    """``(the ref's body capped, its full length)`` — ord≥0 chunks in order."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT text FROM chunks WHERE ref_id = %s AND ord >= 0 ORDER BY ord",
            (ref_id,),
        ).fetchall()
    body = "\n".join(str(r[0]) for r in rows if r[0]).strip()
    return _cap(body, cap), len(body)


def _cut_marker(kind: str, handle: str, shown: int, total: int) -> str:
    """The line closing a body the eye cut at ``_VERBATIM_CAP``: the call that
    returns it whole, and for a memory the way to stop it recurring."""
    line = f"… cut: {shown} of {total} chars — full body: get(kind={kind!r}, id={handle!r})"
    if kind == "memory":
        line += " · split it: a summary node + part-of children"
    return line


def _render_note_eye(
    store: Store,
    handle: str,
    kind: str,
    ext: Extent,
    *,
    recall: bool = False,
    expand: str | None = None,
) -> str:
    """A link-kind ref (memory / finding / …): the note at its extent (title →
    gist → body), and at ``fisheye+1hop`` its **link neighborhood** — every ref
    linked to it, *either direction*, with its relation type. For a memory the
    body *is* the note and the links are its point."""
    ref = _resolve_ref(store, handle)
    if ref is None or getattr(ref, "retired_at", None) is not None:
        raise ValueError(f"eye: no live {kind} ref for {handle!r}")
    if ext <= Extent.TOC and not recall:
        return f"· {_head(ref, kind)}"
    cap = _SUMMARY_CAP if ext <= Extent.SUMMARY else _VERBATIM_CAP
    body, total = _ordered_body(store, int(ref.id), cap=cap)
    if cap == _VERBATIM_CAP and total > _VERBATIM_CAP:
        marker = _cut_marker(
            kind, handle_registry.format_handle(kind, int(ref.id)), _VERBATIM_CAP, total
        )
        body = f"{body}\n{marker}"
    block = f"{_head(ref, kind)}\n{body}" if body else _head(ref, kind)
    sections = _rings(store, int(ref.id), kind, ext, recall=recall, expand=expand)
    return "\n\n".join([block, *sections])


#: Per-store cache for :func:`_relation_reading`, dropped with its store.
_RELATION_READING: weakref.WeakKeyDictionary[
    Store, tuple[dict[str, str], frozenset[str]]
] = weakref.WeakKeyDictionary()


def _relation_reading(store: Store) -> tuple[dict[str, str], frozenset[str]]:
    """``(inverse slug by slug, symmetric slugs)`` from the ``relations``
    table, cached per store for its lifetime like
    ``Store.inverse_relation`` — the vocabulary is static once migrations
    have run."""
    cached = _RELATION_READING.get(store)
    if cached is not None:
        return cached
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT slug, inverse_slug, is_symmetric FROM relations"
        ).fetchall()
    reading = (
        {str(r[0]): str(r[1]) for r in rows if r[1] is not None},
        frozenset(str(r[0]) for r in rows if r[2]),
    )
    _RELATION_READING[store] = reading
    return reading


def _as_seen_from_here(
    relation: str,
    *,
    outbound: bool,
    inverses: dict[str, str],
    symmetric: frozenset[str],
) -> str:
    """The label an edge reads with from this ref's side.

    ``links_for`` returns each edge as stored, so the same ``serves`` row is
    "Q serves P" on Q's eye and "P is served by Q" on P's. An outbound edge
    keeps its slug; an inbound one reads as its inverse slug (``served-by``,
    ``part-of``), as itself when the relation is symmetric, and as
    ``<-slug`` when it has neither — the ``<-`` form ``_links_render`` and
    ``search_merge`` already use for an inbound edge with no passive name.
    Without this a quest's ring could not say which quests it serves and
    which serve it (``fisheye-everywhere.md`` AC 2).
    """
    if outbound or relation in symmetric:
        return relation
    return inverses.get(relation) or f"<-{relation}"


def _link_neighbors(store: Store, ref_id: int) -> str:
    """The ref's one-hop link neighborhood — the ``fisheye+1hop`` layer for a
    non-tree eye. Follows every relation in the ring registry
    (:data:`~precis.utils.refeye.RING_GROUPS`), **both directions**
    (``links_for`` matches either endpoint, incl. chunk-level edges since
    they carry the ref id); the neighbor is the *other* end of each edge,
    labelled as the edge reads from this side (:func:`_as_seen_from_here`).

    Rendered under one heading per ring group, in registry order, then one
    block per label (sorted), each capped at `_NEIGHBOR_GROUP_CAP` live
    neighbours — a claim hub can carry dozens of evidence edges, so this is
    graduated rather than a flat uncapped dump. A truncated block ends with
    a visible ``… +N more`` line (no silent cap); the count is against
    *rendered* (live, non-deleted) neighbours, not raw edges."""
    hop1, refs = _first_hop(store, ref_id)
    return _render_ring(store, ref_id, hop1, refs)


def _render_ring(
    store: Store,
    ref_id: int,
    hop1: dict[tuple[str, str], list[int]],
    refs: dict[int, Any],
) -> str:
    """:func:`_render_first_hop` with the hub listing switched on for a
    ``section:index`` memory (its ``contains`` members are not capped)."""
    notes = _hub_member_notes(store, ref_id, hop1, refs)
    return _render_first_hop(
        hop1,
        refs,
        audit=_audit_flagged(store, refs),
        hub_members=notes,
        more_hint=_more_hint_factory(store, ref_id, hop1),
        hub_handle=handle_registry.format_handle("memory", ref_id)
        if notes is not None
        else None,
    )


def _more_hint_factory(
    store: Store, ref_id: int, hop1: dict[tuple[str, str], list[int]]
) -> Any:
    """``(label, member ids) -> ' · get(…) …'`` for a group's ``… +N more``
    line, or ``None`` when no group overflows (no extra queries then).

    The call re-reads the focus with ``q='<label>'``, which lists that whole
    group (:func:`_expand_first_hop`). For a memory whose group members share
    a ``section:<type>`` tag a scoped ``search`` is offered too."""
    if not any(len(oids) > _NEIGHBOR_GROUP_CAP for oids in hop1.values()):
        return None
    focus = store.fetch_refs_by_ids([ref_id]).get(ref_id)
    if focus is None:
        return None
    kind = str(getattr(focus, "kind", "") or "")
    handle = handle_registry.format_handle(kind, ref_id)
    space = None
    if kind == "memory":
        space = next(
            (
                f"SPACE:{v}"
                for ns, v in store.ref_tags_bulk([ref_id]).get(ref_id, [])
                if ns == "SPACE"
            ),
            None,
        )

    def hint(label: str, oids: list[int]) -> str:
        out = (
            f" · get(kind={kind!r}, id={handle!r}, extent='fisheye+1hop', q={label!r})"
        )
        if kind == "memory" and label == _HUB_MEMBER_LABEL:
            # a summary node's part-of children: search the subtree itself
            out += (
                f" · or search(kind='memory', q='<terms>', "
                f"args={{'under': {handle!r}}})"
            )
        elif kind == "memory" and space is not None:
            tagged = store.ref_tags_bulk(oids)
            shared = set.intersection(
                *(
                    {
                        v
                        for _ns, v in tagged.get(o, [])
                        if v.startswith("section:") and v != _HUB_TAG
                    }
                    for o in oids
                )
            )
            if shared:
                tags = [space, sorted(shared)[0]]
                out += f" · or search(kind='memory', tags={tags!r}, q='<terms>')"
        return out

    return hint


#: The tag marking a memory as a subject hub, and the label its members read
#: with from the hub's side (``part-of`` stored on the member, inverse here).
_HUB_TAG = "section:index"
_HUB_MEMBER_LABEL = "contains"
#: Characters of a hub member's hook / first body line kept in its listing.
_HUB_HOOK_CAP = 100
#: Most members a hub's listing renders; the rest sit behind a ``+N more`` line.
_HUB_LIST_CAP = 300


def _is_hub(store: Store, ref_id: int) -> bool:
    """True iff ``ref_id`` is a memory tagged ``section:index``."""
    return ref_id in _hub_ids(store, [ref_id])


def _hub_ids(store: Store, ref_ids: list[int]) -> set[int]:
    """The ``section:index``-tagged memory refs among ``ref_ids`` (one query)."""
    if not ref_ids:
        return set()
    try:
        with store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT rt.ref_id FROM ref_tags rt "
                "JOIN tags t ON t.tag_id = rt.tag_id "
                "JOIN refs r ON r.ref_id = rt.ref_id "
                "WHERE rt.ref_id = ANY(%s) AND r.kind = 'memory' "
                "AND t.value = %s",
                (list(ref_ids), _HUB_TAG),
            ).fetchall()
    except Exception:
        log.debug("hub tag read failed", exc_info=True)
        return set()
    return {int(r[0]) for r in rows}


def _hub_member_notes(
    store: Store,
    ref_id: int,
    hop1: dict[tuple[str, str], list[int]],
    refs: dict[int, Any],
) -> dict[int, str] | None:
    """For a ``section:index`` memory: ``{member id: hook}`` for each live
    ``contains`` neighbour (``meta.hook``, else the body's first line, cut to
    :data:`_HUB_HOOK_CAP`); ``None`` for any other ref."""
    if not any(label == _HUB_MEMBER_LABEL for _g, label in hop1):
        return None
    if not _is_hub(store, ref_id):
        return None
    members = [
        oid
        for (_g, label), oids in hop1.items()
        if label == _HUB_MEMBER_LABEL
        for oid in oids
    ]
    shown = _by_recency(store, members)[:_HUB_LIST_CAP]
    firsts = _first_body_lines(
        store,
        [o for o in shown if not (getattr(refs[o], "meta", None) or {}).get("hook")],
    )
    notes: dict[int, str] = {}
    for oid in shown:
        hook = str(((getattr(refs[oid], "meta", None) or {}).get("hook")) or "")
        hook = " ".join((hook or firsts.get(oid, "")).split())
        if len(hook) > _HUB_HOOK_CAP:
            hook = hook[: _HUB_HOOK_CAP - 1].rstrip() + "…"
        notes[oid] = hook
    return notes


def _first_body_lines(store: Store, ids: list[int]) -> dict[int, str]:
    """``{ref id: first non-blank line of its memory_body chunk}`` in ONE
    query. A failure is logged and reads as no hook, never as a broken render."""
    if not ids:
        return {}
    try:
        with store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT ref_id, text FROM chunks WHERE ref_id = ANY(%s) "
                "AND chunk_kind = 'memory_body' AND ord >= 0 ORDER BY ord",
                (list(ids),),
            ).fetchall()
    except Exception:
        log.debug("hub member first-line read failed", exc_info=True)
        return {}
    out: dict[int, str] = {}
    for rid, text in rows:
        if int(rid) in out:
            continue
        out[int(rid)] = next(
            (ln.strip() for ln in str(text or "").splitlines() if ln.strip()), ""
        )
    return out


def _by_recency(store: Store, ids: list[int]) -> list[int]:
    """``ids`` newest-first by ``GREATEST(last_viewed_at, last_recalled_at,
    updated_at)`` (NULLs ignored); input order on a tie or a query failure."""
    if len(ids) < 2:
        return ids
    try:
        with store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT ref_id, GREATEST(last_viewed_at, last_recalled_at, "
                "updated_at) FROM refs WHERE ref_id = ANY(%s)",
                (list(ids),),
            ).fetchall()
    except Exception:
        log.debug("recency read failed", exc_info=True)
        return ids
    stamp = {int(r[0]): r[1] for r in rows if r[1] is not None}
    floor = min(stamp.values()) if stamp else None
    if floor is None:
        return ids
    return sorted(ids, key=lambda i: stamp.get(i, floor), reverse=True)


def _first_hop(
    store: Store, ref_id: int
) -> tuple[dict[tuple[str, str], list[int]], dict[int, Any]]:
    """``(live neighbour ids by (ring group, label), the neighbour refs)`` —
    the collected form of :func:`_link_neighbors`, shared with the second
    hop, which walks out from exactly these neighbours."""
    links = store.links_for(ref_id, direction="both")
    by_label = _bucket_edges(
        store,
        ref_id,
        [(int(lk.src_ref_id), int(lk.dst_ref_id), lk.relation) for lk in links],
    )
    if not by_label:
        return {}, {}
    refs = store.fetch_refs_by_ids({o for oids in by_label.values() for o in oids})
    live = {
        key: [oid for oid in oids if _live(refs, oid)] for key, oids in by_label.items()
    }
    return {key: oids for key, oids in live.items() if oids}, refs


def _live(refs: dict[int, Any], oid: int) -> bool:
    r = refs.get(oid)
    return r is not None and getattr(r, "retired_at", None) is None


def _bucket_edges(
    store: Store, ref_id: int, edges: list[tuple[int, int, Any]]
) -> dict[tuple[str, str], list[int]]:
    """Group ``ref_id``'s ``(src, dst, relation)`` edges by ``(ring group,
    label as seen from here)``, listing the other endpoint of each — dead
    ends included; the caller filters by liveness."""
    inverses, symmetric = _relation_reading(store)
    by_label: dict[tuple[str, str], list[int]] = {}
    for src, dst, rel in edges:
        group = ring_group(str(rel)) if rel is not None else None
        if group is None:
            continue
        outbound = src == ref_id
        other = dst if outbound else src
        if other == ref_id:
            continue
        label = _as_seen_from_here(
            str(rel), outbound=outbound, inverses=inverses, symmetric=symmetric
        )
        by_label.setdefault((group, label), []).append(other)
    return by_label


def first_hop_shape(
    store: Store, ref_ids: list[int]
) -> dict[int, tuple[int, int, int]]:
    """What ``fisheye+1hop`` shows of each ref's link ring, in three queries:
    ``{ref id: (live neighbours, largest live label group, dead ends)}``.

    The largest group is what :data:`_NEIGHBOR_GROUP_CAP` truncates; a dead
    end is a ring edge to a retired or missing ref, which the eye hides.
    ``scripts/memory-lint`` reads these from the memory export manifest.
    A ``section:index`` hub lists its ``contains`` members uncapped, so they
    never count toward the largest (truncated) group."""
    if not ref_ids:
        return {}
    hubs = _hub_ids(store, list(ref_ids))
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT src_ref_id, dst_ref_id, relation FROM links "
            "WHERE src_ref_id = ANY(%s) OR dst_ref_id = ANY(%s)",
            (ref_ids, ref_ids),
        ).fetchall()
    wanted = set(ref_ids)
    edges: dict[int, list[tuple[int, int, Any]]] = {r: [] for r in ref_ids}
    for src, dst, rel in rows:
        for end in {int(src), int(dst)} & wanted:
            edges[end].append((int(src), int(dst), rel))
    buckets = {r: _bucket_edges(store, r, e) for r, e in edges.items()}
    others = {o for b in buckets.values() for oids in b.values() for o in oids}
    refs = store.fetch_refs_by_ids(others)
    shape: dict[int, tuple[int, int, int]] = {}
    for r, by_label in buckets.items():
        live_groups = [
            {o for o in oids if _live(refs, o)}
            for (_g, label), oids in by_label.items()
            if not (r in hubs and label == _HUB_MEMBER_LABEL)
        ]
        live_all = [{o for o in oids if _live(refs, o)} for oids in by_label.values()]
        every = {o for oids in by_label.values() for o in oids}
        live = set().union(*live_all) if live_all else set()
        shape[r] = (len(live), max(map(len, live_groups), default=0), len(every - live))
    return shape


#: Most neighbours an expanded first-hop group lists (``q='<label>'``).
_FIRST_HOP_EXPAND_CAP = 500


def _expand_first_hop(
    store: Store,
    hop1: dict[tuple[str, str], list[int]],
    refs: dict[int, Any],
    label: str,
) -> str:
    """One first-hop label's live neighbours, uncapped by the ring's per-group
    cap (bounded by :data:`_FIRST_HOP_EXPAND_CAP`)."""
    ids = [o for (_g, lab), oids in hop1.items() if lab == label for o in oids]
    if not ids:
        known = ", ".join(sorted({lab for _g, lab in hop1})) or "none"
        raise ValueError(f"eye: no first-hop group {label!r}; groups here: {known}")
    audit = _audit_flagged(store, refs)
    lines = [f"— linked (1 hop) · {label}: {len(ids)} —"]
    lines.extend(
        f"  {label}: {_neighbor_label(refs[o], o, audit=o in audit)}"
        for o in ids[:_FIRST_HOP_EXPAND_CAP]
    )
    if len(ids) > _FIRST_HOP_EXPAND_CAP:
        lines.append(f"    … +{len(ids) - _FIRST_HOP_EXPAND_CAP} more")
    return "\n".join(lines)


def _audit_flagged(store: Store, ref_ids: Any) -> frozenset[int]:
    """Ids among ``ref_ids`` carrying ``AUDIT:ungrounded-number`` (one query),
    so a ring label can warn the reader before they trust the prose."""
    ids = [int(i) for i in ref_ids]
    if not ids:
        return frozenset()
    try:
        with store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT rt.ref_id FROM ref_tags rt "
                "JOIN tags t ON t.tag_id = rt.tag_id "
                "WHERE rt.ref_id = ANY(%s) AND t.namespace = 'AUDIT' "
                "AND t.value = 'ungrounded-number'",
                (ids,),
            ).fetchall()
    except Exception:
        # A label decoration must never break a render.
        return frozenset()
    return frozenset(int(r[0]) for r in rows)


def _render_first_hop(
    by_label: dict[tuple[str, str], list[int]],
    refs: dict[int, Any],
    audit: frozenset[int] = frozenset(),
    hub_members: dict[int, str] | None = None,
    more_hint: Any = None,
    hub_handle: str | None = None,
) -> str:
    order = {group: i for i, group in enumerate(RING_GROUPS)}
    lines = ["— linked (1 hop) —"]
    heading = None
    for group, label in sorted(by_label, key=lambda k: (order[k[0]], k[1])):
        live_ids = by_label[(group, label)]
        if group != heading:
            lines.append(f"{group}:")
            heading = group
        if hub_members is not None and label == _HUB_MEMBER_LABEL:
            live = set(live_ids)
            listed = [o for o in hub_members if o in live]
            for oid in listed:
                hook = hub_members.get(oid, "")
                row = _neighbor_label(refs[oid], oid, audit=oid in audit)
                lines.append(
                    f"  {label}: {row} — {hook}" if hook else f"  {label}: {row}"
                )
            if len(live_ids) > len(listed):
                lines.append(
                    f"    … +{len(live_ids) - len(listed)} more · get(kind='memory', "
                    f"id='{hub_handle}', extent='fisheye+1hop', q='{label}')"
                )
            continue
        for oid in live_ids[:_NEIGHBOR_GROUP_CAP]:
            lines.append(
                f"  {label}: {_neighbor_label(refs[oid], oid, audit=oid in audit)}"
            )
        if len(live_ids) > _NEIGHBOR_GROUP_CAP:
            more = more_hint(label, live_ids) if more_hint is not None else ""
            lines.append(f"    … +{len(live_ids) - _NEIGHBOR_GROUP_CAP} more{more}")
    if hub_handle is not None and heading is not None:
        lines.append(
            f"search within: search(kind='memory', q='<terms>', "
            f"args={{'under': '{hub_handle}'}})"
        )
    return "\n".join(lines) if heading is not None else ""


def _neighbor_label(ref: Any, ref_id: int, *, audit: bool = False) -> str:
    oh = _handle_with_file(getattr(ref, "kind", "?"), ref, ref_id)
    title = " ".join((getattr(ref, "title", None) or "").split())
    if len(title) > _NEIGHBOR_TITLE_CAP:
        title = title[: _NEIGHBOR_TITLE_CAP - 1].rstrip() + "…"
    label = f"{oh} — {title}" if title else oh
    return f"{label}  [AUDIT:ungrounded-number]" if audit else label


#: Most ``(kind, label)`` count lines the second hop renders before its
#: overflow line. The second hop is counts, never a list, so its size is
#: bounded by how many distinct (kind, relation) pairs exist, not by edges:
#: a hub with thousands of second-hop edges still renders this many lines.
_SECOND_HOP_LINE_CAP = 24
#: Most refs an expanded second-hop group lists (``q='<kind>:<label>'``).
_SECOND_HOP_EXPAND_CAP = 40
#: Edge rows read for the second hop. A safety bound on one SQL read, not a
#: rendering cap: the counts say "≥" when it is hit.
_SECOND_HOP_ROW_CAP = 20000


def _second_hop(
    store: Store,
    ref_id: int,
    hop1: dict[tuple[str, str], list[int]],
    *,
    expand: str | None = None,
) -> str:
    """The ``fisheye+2hop`` layer: what the first-hop neighbours link to.

    Rendered as counts per ``(kind, label)`` — "12 paper via cites" — read
    from the first-hop neighbour's side, excluding the focus itself and refs
    already on the first hop. A list of second-hop refs would grow with the
    graph; counts grow only with the vocabulary, which is what keeps a hub
    with hundreds of second-hop edges inside the response frame
    (``fisheye-everywhere.md`` AC 4).

    ``expand='<kind>:<label>'`` (the eye's ``q=``) lists that one group's
    refs instead, capped. The backlog item asked for ``more()`` to expand a
    group, but ``more()`` only pages an over-long body (``tools/core.py::
    more``) and has no notion of a named group, so the expansion is the
    same call with a filter — decided 2026-10-02, recorded in the item.
    """
    hop1_ids = {oid for oids in hop1.values() for oid in oids}
    if not hop1_ids:
        return ""
    inverses, symmetric = _relation_reading(store)
    from precis.utils.refeye import RING_RELATIONS

    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT l.src_ref_id, l.dst_ref_id, l.relation, "
            "       rs.kind, rs.retired_at IS NULL, rd.kind, rd.retired_at IS NULL "
            "FROM links l "
            "JOIN refs rs ON rs.ref_id = l.src_ref_id "
            "JOIN refs rd ON rd.ref_id = l.dst_ref_id "
            "WHERE (l.src_ref_id = ANY(%s) OR l.dst_ref_id = ANY(%s)) "
            "  AND l.relation = ANY(%s) "
            "LIMIT %s",
            (
                list(hop1_ids),
                list(hop1_ids),
                sorted(RING_RELATIONS),
                _SECOND_HOP_ROW_CAP,
            ),
        ).fetchall()
    groups: dict[tuple[str, str], set[int]] = {}
    for src, dst, rel, src_kind, src_live, dst_kind, dst_live in rows:
        src, dst = int(src), int(dst)
        # Read each edge from the first-hop end. An edge between two
        # first-hop refs, or back to the focus, is already on screen.
        for near, far, far_kind, far_live, outbound in (
            (src, dst, dst_kind, dst_live, True),
            (dst, src, src_kind, src_live, False),
        ):
            if near not in hop1_ids or far == ref_id or far in hop1_ids:
                continue
            if not far_live:
                continue
            label = _as_seen_from_here(
                str(rel), outbound=outbound, inverses=inverses, symmetric=symmetric
            )
            groups.setdefault((str(far_kind), label), set()).add(far)
    if not groups:
        if expand is not None:
            raise ValueError(
                f"eye: no second-hop group {expand!r}; the second hop is empty"
            )
        return ""
    floor = "≥" if len(rows) >= _SECOND_HOP_ROW_CAP else ""
    if expand is not None:
        return _expand_second_hop(store, groups, expand)
    ranked = sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    lines = [
        f"— second hop ({len(hop1_ids)} neighbours out; counts — "
        "expand one with q='<kind>:<label>') —"
    ]
    for (kind, label), ids in ranked[:_SECOND_HOP_LINE_CAP]:
        lines.append(f"  {floor}{len(ids)} {kind} via {label}")
    if len(ranked) > _SECOND_HOP_LINE_CAP:
        rest = sum(len(ids) for _key, ids in ranked[_SECOND_HOP_LINE_CAP:])
        lines.append(
            f"    … +{len(ranked) - _SECOND_HOP_LINE_CAP} more groups ({rest} refs)"
        )
    return "\n".join(lines)


def _expand_second_hop(
    store: Store, groups: dict[tuple[str, str], set[int]], expand: str
) -> str:
    kind, _, label = expand.partition(":")
    ids = groups.get((kind.strip(), label.strip()))
    if not ids:
        known = ", ".join(f"{k}:{lab}" for k, lab in sorted(groups))
        raise ValueError(f"eye: no second-hop group {expand!r}; groups here: {known}")
    shown = sorted(ids)[:_SECOND_HOP_EXPAND_CAP]
    refs = store.fetch_refs_by_ids(shown)
    lines = [f"— second hop: {len(ids)} {kind} via {label} —"]
    flagged = _audit_flagged(store, refs)
    lines.extend(
        f"  {_neighbor_label(refs[i], i, audit=i in flagged)}"
        for i in shown
        if i in refs
    )
    if len(ids) > len(shown):
        lines.append(f"    … +{len(ids) - len(shown)} more")
    return "\n".join(lines)


#: Nearest neighbours the ``+recall`` rung lists (``fisheye-everywhere.md``
#: open question: start k=8, same kind + finding).
_RECALL_K = 8
#: Cosine-distance floor for ``+recall``: past it a "neighbour" is just the
#: closest unrelated chunk, and listing it would read as a connection.
_RECALL_MAX_DISTANCE = 0.6


def _recall(store: Store, ref_id: int, kind: str) -> str:
    """The ``+recall`` rung: the k nearest chunks by embedding, same kind +
    ``finding``, each with its gist line. Similarity, not edges — the ring
    above says what is linked; this says what is *about* the same thing and
    was never linked (``refeye``'s module docstring draws the same line).
    """
    seed = store.chunks.seed_chunk_for_ref(ref_id)
    vec = store.chunks.get_chunk_vector(seed) if seed is not None else None
    if vec is None:
        return "— recall: no embedded chunk on this ref yet —"
    kinds = sorted({kind, "finding"})
    # A SPACE: tag is a partition (research / repo-dev / personal): recall
    # stays inside the focus's own, filtered in the query so k fills from it.
    space = [str(t) for t in store.tags_for(ref_id) if t.prefix == "SPACE"]
    hits = store.chunks.search_chunks_semantic(
        query_vec=vec,
        kinds=kinds,
        tags=space or None,
        limit=_RECALL_K * 3,
        max_distance=_RECALL_MAX_DISTANCE,
        exclude_ref_ids=[ref_id],
        # memory-like kinds embed their whole-ref card (ord -1), not a body
        # chunk; let it match, as `seed_chunk_for_ref` does for the seed.
        card_kinds=("card_combined",),
    )
    flagged = _audit_flagged(store, {int(ref.id) for _b, ref, _d in hits})
    seen: set[int] = set()
    lines = [f"— recall (nearest by embedding, {'+'.join(kinds)}, k≤{_RECALL_K}) —"]
    for block, ref, dist in hits:
        rid = int(ref.id)
        if rid in seen:
            continue  # one line per ref: its nearest chunk speaks for it
        seen.add(rid)
        gist = _cap(" ".join((block.text or "").split()), _CHUNK_SUMMARY_CAP)
        lines.append(
            f"  {_neighbor_label(ref, rid, audit=rid in flagged)}  ({1 - dist:.2f})"
        )
        if gist:
            lines.append(f"    {gist}")
        if len(seen) == _RECALL_K:
            break
    if not seen:
        lines.append("  — nothing within the similarity floor —")
    return "\n".join(lines)


def _rings(
    store: Store,
    ref_id: int,
    kind: str,
    ext: Extent,
    *,
    recall: bool,
    expand: str | None,
) -> list[str]:
    """Every neighbourhood section a non-tree eye appends at ``ext``:
    the first hop at ``fisheye+1hop`` and up, the second hop at
    ``fisheye+2hop``, recall when asked."""
    sections: list[str] = []
    if ext >= Extent.HOP1:
        hop1, refs = _first_hop(store, ref_id)
        first_hop_label = expand if expand is not None and ":" not in expand else None
        if first_hop_label is not None:
            sections.append(_expand_first_hop(store, hop1, refs, first_hop_label))
        elif expand is None:
            sections.append(_render_ring(store, ref_id, hop1, refs))
        if ext >= Extent.HOP2 and first_hop_label is None:
            sections.append(_second_hop(store, ref_id, hop1, expand=expand))
    if recall:
        sections.append(_recall(store, ref_id, kind))
    return [s for s in sections if s]
