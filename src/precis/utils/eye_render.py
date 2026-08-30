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
  neighborhood** — every ref linked to it, **either direction**, with its
  **relation type**, grouped by relation and capped per group. Links are
  symmetric, so a note linked to a paper surfaces when you fisheye the paper
  (via the doc eye's ring) and the paper surfaces when you fisheye the note.
  For a claim hub the neighborhood now also includes its claim graph
  (``RING_RELATIONS`` = ``SEMANTIC_RELATIONS`` + ``CLAIM_RELATIONS`` —
  ``establishes``/``corroborates``/``contradicts``/``refines``/
  ``conjunct-of``/``motivated-by``), not just plain notes/links.

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

Wired for ``draft`` via ``src/precis/handlers/draft.py::DraftHandler.get``'s
``extent=`` kwarg, which calls :func:`render_eye` directly. Not yet wired for ``plan``
(``PlanHandler.get`` has no ``extent=`` kwarg and silently ignores one). The
composer (:func:`precis.utils.working_set_render.render_working_set`) also
dispatches non-tree eyes here, for multi-eye working-set assembly
(planner/dream passes) — that composer's own eye-placement loop is
worker-internal, not an agent-facing verb.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from precis.utils import handle_registry
from precis.utils.refeye import RING_RELATIONS
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
) -> str:
    """Render one eye by its kind's neighborhood strategy. Raises ``ValueError``
    if the handle does not resolve to a live ref/chunk."""
    if handle.startswith(_SKILL_HANDLE_PREFIX):
        return _render_skill_eye(handle, Extent.parse(extent))
    parsed = handle_registry.parse(handle)
    if parsed is None:
        raise ValueError(f"eye: unresolvable handle {handle!r}")
    kind, is_chunk, pk = parsed
    ext = Extent.parse(extent)
    if kind in _TREE_KINDS:
        from precis.utils.fisheye import render_fisheye

        return render_fisheye(store, kind=kind, handle=handle, extent=ext)
    if kind in _DOC_KINDS:
        return _render_doc_eye(store, handle, kind, ext, is_chunk=is_chunk)
    return _render_note_eye(store, handle, kind, ext)


# ── shared helpers ───────────────────────────────────────────────────


def _resolve_ref(store: Store, handle: str) -> Any:
    r = store.resolve_handle(handle)
    if r is None:
        return None
    rid = int(r.ref_id)
    return store.fetch_refs_by_ids([rid]).get(rid)


def _head(ref: Any, kind: str) -> str:
    hid = handle_registry.format_handle(kind, int(ref.id))
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
    store: Store, handle: str, kind: str, ext: Extent, *, is_chunk: bool
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
    if ext >= Extent.HOP1:
        ring = _link_neighbors(store, ref_id)
        if ring:
            block += f"\n\n{ring}"
    return block


# ── link kinds: the note + its link graph (memory / finding / …) ──────


def _ordered_body(store: Store, ref_id: int, *, cap: int) -> str:
    """The ref's body — its ord≥0 chunks in order, capped."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT text FROM chunks WHERE ref_id = %s AND ord >= 0 ORDER BY ord",
            (ref_id,),
        ).fetchall()
    body = "\n".join(str(r[0]) for r in rows if r[0]).strip()
    return _cap(body, cap)


def _render_note_eye(store: Store, handle: str, kind: str, ext: Extent) -> str:
    """A link-kind ref (memory / finding / …): the note at its extent (title →
    gist → body), and at ``fisheye+1hop`` its **link neighborhood** — every ref
    linked to it, *either direction*, with its relation type. For a memory the
    body *is* the note and the links are its point."""
    ref = _resolve_ref(store, handle)
    if ref is None or getattr(ref, "retired_at", None) is not None:
        raise ValueError(f"eye: no live {kind} ref for {handle!r}")
    if ext <= Extent.TOC:
        return f"· {_head(ref, kind)}"
    cap = _SUMMARY_CAP if ext is Extent.SUMMARY else _VERBATIM_CAP
    body = _ordered_body(store, int(ref.id), cap=cap)
    block = f"{_head(ref, kind)}\n{body}" if body else _head(ref, kind)
    if ext < Extent.HOP1:
        return block
    neighbors = _link_neighbors(store, int(ref.id))
    return f"{block}\n\n{neighbors}" if neighbors else block


def _link_neighbors(store: Store, ref_id: int) -> str:
    """The ref's one-hop link neighborhood, grouped by relation type — the
    ``fisheye+1hop`` layer for a non-tree eye. Follows meaning edges +
    claim-graph edges (`RING_RELATIONS`), **both directions** (``links_for``
    matches either endpoint, incl. chunk-level edges since they carry the
    ref id); the neighbor is the *other* end of each edge.

    Grouped by relation (relations in sorted order) and capped at
    `_NEIGHBOR_GROUP_CAP` live neighbours per group — a claim hub can carry
    dozens of evidence edges, so this is graduated rather than a flat
    uncapped dump. A truncated group ends with a visible ``… +N more``
    line (no silent cap); the count is against *rendered* (live,
    non-deleted) neighbours, not raw edges."""
    links = store.links_for(ref_id, direction="both")
    by_rel: dict[str, list[int]] = {}
    ids: set[int] = set()
    for link in links:
        rel = getattr(link, "relation", None)
        if rel not in RING_RELATIONS:
            continue
        other = (
            int(link.dst_ref_id)
            if int(link.src_ref_id) == ref_id
            else int(link.src_ref_id)
        )
        if other == ref_id:
            continue
        by_rel.setdefault(str(rel), []).append(other)
        ids.add(other)
    if not by_rel:
        return ""
    refs = store.fetch_refs_by_ids(list(ids))

    def _live(oid: int) -> bool:
        r = refs.get(oid)
        return r is not None and getattr(r, "retired_at", None) is None

    lines = ["— linked (1 hop) —"]
    rendered_any = False
    for rel in sorted(by_rel):
        live_ids = [oid for oid in by_rel[rel] if _live(oid)]
        for oid in live_ids[:_NEIGHBOR_GROUP_CAP]:
            r = refs[oid]
            rendered_any = True
            oh = handle_registry.format_handle(getattr(r, "kind", "?"), oid)
            title = " ".join((getattr(r, "title", None) or "").split())
            if len(title) > _NEIGHBOR_TITLE_CAP:
                title = title[: _NEIGHBOR_TITLE_CAP - 1].rstrip() + "…"
            lines.append(f"  {rel}: {oh} — {title}" if title else f"  {rel}: {oh}")
        if len(live_ids) > _NEIGHBOR_GROUP_CAP:
            lines.append(f"    … +{len(live_ids) - _NEIGHBOR_GROUP_CAP} more")
    return "\n".join(lines) if rendered_any else ""
