"""Per-kind eye render (ADR 0051 §6) — an eye's *neighborhood* depends on its
kind, so the ladder generalizes but its shape does not:

- **Tree kinds** (``draft`` / ``plan``): reading-order neighborhood — the
  :func:`precis.utils.fisheye.render_fisheye` span + reference ring.
- **Doc kinds** (``paper`` / ``patent`` / ``web`` / ``datasheet`` / ``cfp``): a
  long ingested document with no heading tree, so its structure *is* the
  per-chunk KeyBERT clustering (F20/ADR-0018). The eye renders that dynamic
  **keyword-cluster TOC** around the eyeball — similar chunks grouped for
  separate exploration:

  * A **whole-doc eye** (``pa5``) is the cluster *map*: one row per cluster,
    keyed by its lead **chunk handle** ``pc<id>`` + keyword label — a skimmable
    shape you drill by placing an eye on a ``pc`` handle. Per §6 a whole-doc
    eye never spills verbatim text; reading real text is always a deliberate
    drill to a chunk eye.
  * A **chunk eye** (``pc13234``) is a fisheye *within* its cluster: the chunks
    before it and after it as gloss lines (each its own ``pc`` handle to drill),
    the eye chunk itself verbatim (or a gloss at ``summary``), and every *other*
    cluster collapsed to a one-line label. So focusing a chunk opens its
    neighborhood and leaves the rest of the paper as a drillable map.

  Everything is addressed by its universal ``pc<id>`` handle (ADR 0036) — the
  legacy ``slug~pos`` form is never emitted here.
- **Link kinds** (``memory`` / ``finding`` / …): the ref renders as its note
  (title → gist → body), and at ``fisheye+1hop`` it grows its **link
  neighborhood** — every ref linked to it, **either direction**, with its
  **relation type**. Links are symmetric, so a note linked to a paper surfaces
  when you fisheye the paper (via the doc eye's ring) and the paper surfaces
  when you fisheye the note.

``skill`` is file-backed (``resolve_handle`` returns ``None``), so an eye on a
skill needs the handler, not this store-only path — a documented follow-up.

Ships dark; the composer (:func:`precis.utils.working_set_render`) dispatches
non-tree eyes here.
"""

from __future__ import annotations

from typing import Any

from precis.utils import handle_registry
from precis.utils.refeye import SEMANTIC_RELATIONS
from precis.workers.working_set import Extent

#: Reading-order tree kinds — routed to the spatial fisheye.
_TREE_KINDS: frozenset[str] = frozenset({"draft", "plan"})

#: Long ingested documents whose structure is per-chunk KeyBERT clustering
#: (F20) rather than a heading tree — routed to the keyword-cluster fisheye.
_DOC_KINDS: frozenset[str] = frozenset({"paper", "patent", "web", "datasheet", "cfp"})

_SUMMARY_CAP = 300
_VERBATIM_CAP = 4000
_NEIGHBOR_TITLE_CAP = 80
_GLOSS_CAP = 140
#: Keep the cluster map / label lists skimmable even on a huge doc; the
#: clusterer already caps the top level, this bounds the collapsed labels.
_MAP_CLUSTER_CAP = 20

#: Forward-biased gloss window *within* the eye's home cluster (mirrors the
#: draft fisheye's falloff): a keyword-homogeneous section can cluster into
#: dozens of chunks, so show the eye's neighbours and collapse the far tail
#: to a ``⋯ N more ⋯`` marker rather than dumping the whole section.
_HOME_BACK = 6
_HOME_FWD = 12


def render_eye(store: Any, handle: str, extent: Extent | str | int) -> str:
    """Render one eye by its kind's neighborhood strategy. Raises ``ValueError``
    if the handle does not resolve to a live ref/chunk."""
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


def _resolve_ref(store: Any, handle: str) -> Any:
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


# ── doc kinds: the keyword-cluster fisheye (paper / patent / web / …) ──


def _chunk_handle(kind: str, block: Any) -> str:
    """The block's universal ``pc<id>`` chunk handle (ADR 0036)."""
    return handle_registry.format_handle(kind, int(block.id), chunk=True)


def _block_gloss(block: Any) -> str:
    """A one-line gloss for a chunk: its KeyBERT keywords, else its first line
    of text, whitespace-collapsed and capped."""
    kws = block.keywords or []
    if kws:
        return _cap(", ".join(kws), _GLOSS_CAP)
    return _cap(" ".join((block.text or "").split()), _GLOSS_CAP)


def _cluster_label(kind: str, bucket: list[Any], kws: list[str]) -> str:
    """One collapsed label line for a cluster — its lead ``pc`` handle, the span
    size, and the keyword label. Drill it by focusing the handle."""
    lead = _chunk_handle(kind, bucket[0])
    span = f" +{len(bucket) - 1}" if len(bucket) > 1 else ""
    label = ", ".join(kws) or _block_gloss(bucket[0]) or "…"
    return f"  · {lead}{span}  {_cap(label, _GLOSS_CAP)}"


def _cluster_map(kind: str, clusters: list[tuple[list[Any], list[str]]]) -> str:
    """A whole-doc eye: the cluster map — one label row per cluster (§6, "TOC
    map, no verbatim text"). Drill any cluster by focusing its ``pc`` handle."""
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
    labels; the home cluster splits into before-chunks (gloss) / the eye chunk
    (verbatim, or a gloss at ``summary``) / after-chunks (gloss) — each chunk
    its own ``pc`` handle to drill next."""
    home = next(
        (
            i
            for i, (bucket, _) in enumerate(clusters)
            if bucket and bucket[0].pos <= eye_ord <= bucket[-1].pos
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
    eye_i = next((i for i, b in enumerate(home_bucket) if b.pos == eye_ord), 0)
    lo = max(0, eye_i - _HOME_BACK)
    hi = min(len(home_bucket), eye_i + _HOME_FWD + 1)
    if lo > 0:
        lines.append(f"  ⋯ {lo} more ⋯")
    for b in home_bucket[lo:hi]:
        h = _chunk_handle(kind, b)
        if b.pos == eye_ord:
            if ext is Extent.SUMMARY:
                lines.append(f"▸ {h} [{b.chunk_kind}]  {_block_gloss(b)}")
            else:
                lines.append(f"▸ {h} [{b.chunk_kind}]\n{_cap(b.text, _VERBATIM_CAP)}")
        else:
            lines.append(f"  · {h}  {_block_gloss(b)}")
    if hi < len(home_bucket):
        lines.append(f"  ⋯ {len(home_bucket) - hi} more ⋯")

    for bucket, kws in clusters[home + 1 :]:
        lines.append(_cluster_label(kind, bucket, kws))
    return "\n".join(lines)


def _render_doc_eye(
    store: Any, handle: str, kind: str, ext: Extent, *, is_chunk: bool
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
    if ref is None or getattr(ref, "deleted_at", None) is not None:
        raise ValueError(f"eye: no live {kind} ref for {handle!r}")
    head = _head(ref, kind)
    if ext <= Extent.TOC:
        return f"· {head}"

    blocks = store.list_blocks_for_ref(ref_id)
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


def _ordered_body(store: Any, ref_id: int, *, cap: int) -> str:
    """The ref's body — its ord≥0 chunks in order, capped."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT text FROM chunks WHERE ref_id = %s AND ord >= 0 ORDER BY ord",
            (ref_id,),
        ).fetchall()
    body = "\n".join(str(r[0]) for r in rows if r[0]).strip()
    return _cap(body, cap)


def _render_note_eye(store: Any, handle: str, kind: str, ext: Extent) -> str:
    """A link-kind ref (memory / finding / …): the note at its extent (title →
    gist → body), and at ``fisheye+1hop`` its **link neighborhood** — every ref
    linked to it, *either direction*, with its relation type. For a memory the
    body *is* the note and the links are its point."""
    ref = _resolve_ref(store, handle)
    if ref is None or getattr(ref, "deleted_at", None) is not None:
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


def _link_neighbors(store: Any, ref_id: int) -> str:
    """The ref's one-hop link neighborhood, grouped by relation type — the
    ``fisheye+1hop`` layer for a non-tree eye. Follows meaning edges
    (`SEMANTIC_RELATIONS`), **both directions** (``links_for`` matches either
    endpoint, incl. chunk-level edges since they carry the ref id); the neighbor
    is the *other* end of each edge."""
    links = store.links_for(ref_id, direction="both")
    edges: list[tuple[str, int]] = []
    ids: set[int] = set()
    for link in links:
        rel = getattr(link, "relation", None)
        if rel not in SEMANTIC_RELATIONS:
            continue
        other = (
            int(link.dst_ref_id)
            if int(link.src_ref_id) == ref_id
            else int(link.src_ref_id)
        )
        if other == ref_id:
            continue
        edges.append((str(rel), other))
        ids.add(other)
    if not edges:
        return ""
    refs = store.fetch_refs_by_ids(list(ids))
    lines = ["— linked (1 hop) —"]
    for rel, oid in edges:
        r = refs.get(oid)
        if r is None or getattr(r, "deleted_at", None) is not None:
            continue
        oh = handle_registry.format_handle(getattr(r, "kind", "?"), oid)
        title = " ".join((getattr(r, "title", None) or "").split())
        if len(title) > _NEIGHBOR_TITLE_CAP:
            title = title[: _NEIGHBOR_TITLE_CAP - 1].rstrip() + "…"
        lines.append(f"  {rel}: {oh} — {title}" if title else f"  {rel}: {oh}")
    return "\n".join(lines) if len(lines) > 1 else ""
