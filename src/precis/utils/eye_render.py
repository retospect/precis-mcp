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
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import psycopg

from precis.errors import NotFound
from precis.store.types import _CLOSED_VOCAB
from precis.utils import handle_registry
from precis.utils.refeye import RING_GROUP_KIND_RANK, RING_GROUPS, ring_group
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
#: Passage handles a collapsed row prints before its ``· +N more`` tail —
#: a hub paper can ground a claim at dozens of chunks; the row lists the
#: first few and counts the rest.
_RING_PASSAGE_CAP = 4
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

#: The closed ``SPACE:`` tag axis (``docs/backlog/file-mirror.md``
#: §Pillar-review deltas) — the partition a ``space=`` ring filter selects
#: inside. Derived from the store's closed vocabulary so a new SPACE value
#: (e.g. ``design-conventions``) is filterable everywhere at once; kept as
#: this module's name so the renderer and the web pages
#: (``precis_web.graph_filters``) share one list.
SPACE_AXIS: frozenset[str] = _CLOSED_VOCAB["SPACE"]


@dataclass(frozen=True)
class RingFilter:
    """A narrowing on a link-kind eye's rings — by neighbour kind and/or by
    the ``SPACE:`` tag axis (``fisheye-everywhere.md`` in-scope 1), so e.g.
    repo-dev and research neighbourhoods can be shown apart. ``None`` on an
    axis leaves it open; ``spaces`` drops every neighbour carrying no
    ``SPACE:`` tag (the axis is a partition). The focus itself always
    renders; the tree eyes (reading order, not link edges) and the atomic
    skill eye have no ring to narrow.
    """

    kinds: frozenset[str] | None = None
    spaces: frozenset[str] | None = None

    @classmethod
    def parse(cls, raw: Any) -> RingFilter | None:
        """``ring=``'s wire form → a :class:`RingFilter`: ``None``, an
        existing ``RingFilter``, or a dict ``{"kinds": <csv|list>,
        "space": <csv|list>}`` (values may carry or omit the ``SPACE:``
        prefix). Raises ``ValueError`` on a non-dict or an unknown kind —
        the same error channel as an unknown rung."""
        if raw is None or isinstance(raw, cls):
            return raw
        if not isinstance(raw, dict):
            raise ValueError(
                f"eye: ring= takes a dict like {{'kinds': 'memory', "
                f"'space': 'repo-dev'}}, got {raw!r}"
            )
        kinds = _csv_set(raw.get("kinds"))
        spaces = _csv_set(raw.get("space"))
        bad_kinds = [k for k in kinds or () if not handle_registry.is_known_kind(k)]
        if bad_kinds:
            raise ValueError(f"eye: unknown ring kind(s) {bad_kinds!r}")
        normed = (
            frozenset(
                v[len("SPACE:") :] if v.upper().startswith("SPACE:") else v
                for v in spaces
            )
            if spaces is not None
            else None
        )
        return cls(kinds=kinds, spaces=normed)


def _csv_set(raw: Any) -> frozenset[str] | None:
    """``<csv>`` or a list/tuple/set of values → a frozenset, ``None`` when
    absent or empty."""
    if raw is None:
        return None
    vals = (
        raw if isinstance(raw, (list, tuple, set, frozenset)) else str(raw).split(",")
    )
    out = frozenset(str(v).strip() for v in vals if str(v).strip())
    return out or None


def render_eye(
    # store stays Any: tests pass a hand-rolled fake narrower than Store
    # (test_skill_eye_* pass None for the skill-eye path, which never
    # touches store)
    store: Any,
    handle: str,
    extent: Extent | str | int,
    *,
    q: str | None = None,
    ring: RingFilter | dict[str, Any] | None = None,
) -> str:
    """Render one eye by its kind's neighborhood strategy. Raises ``ValueError``
    if the handle does not resolve to a live ref/chunk, or asks a rung its
    kind does not have.

    ``extent`` is a ladder rung, optionally suffixed ``+recall``
    (``fisheye+1hop+recall``); a bare ``+recall`` is ``fisheye+1hop+recall``.
    ``q='<kind>:<label>'`` at ``fisheye+2hop`` expands one second-hop group
    (:func:`_second_hop`); ``q='<label>'`` (no colon) at ``fisheye+1hop`` or
    above lists one first-hop group uncapped (:func:`_expand_first_hop`) — the
    call a ``… +N more`` line carries. ``ring`` narrows every ring a link or
    doc eye renders (first hop, second hop, recall) by neighbour kind and/or
    ``SPACE:`` tag (:class:`RingFilter`)."""
    ext, recall = parse_extent(extent)
    filt = RingFilter.parse(ring)
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
            store,
            handle,
            kind,
            ext,
            is_chunk=is_chunk,
            recall=recall,
            expand=q,
            ring=filt,
        )
    return _render_note_eye(
        store, handle, kind, ext, recall=recall, expand=q, ring=filt
    )


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


def _head(ref: Any, kind: str, chips: str = "") -> str:
    hid = _handle_with_file(kind, ref, int(ref.id))
    title = " ".join((getattr(ref, "title", None) or "").split())
    head = f"{hid} [{kind}] {title}".rstrip()
    return f"{head}  {chips}" if chips else head


def _head_chips(store: Any, ref: Any, kind: str) -> str:
    """The focus ref's chips for its ``_head`` line — its tag chips (same
    helper and order as the row chips, wider cap since it renders once)
    plus the age/read chip (gr524009)."""
    from precis.utils import tag_chips

    ref_id = int(ref.id)
    tags = tag_chips.chips_bulk(store, {ref_id: kind}, cap=tag_chips.HEAD_CHIP_CAP).get(
        ref_id, ""
    )
    return f"{tags} {_read_chip(store, ref)}".strip()


def _coarse_age(at: Any) -> str:
    """``-10d`` / ``-4h`` / ``-2mo`` — a coarse UTC-relative age in the eye
    chips' ``-N<u>`` form (gr524009): seconds under a minute, then
    m/h/d/mo/y."""
    s = max(0, int((datetime.now(UTC) - at.astimezone(UTC)).total_seconds()))
    if s < 60:
        return f"-{s}s"
    if s < 3600:
        return f"-{s // 60}m"
    if s < 86400:
        return f"-{s // 3600}h"
    if s < 30 * 86400:
        return f"-{s // 86400}d"
    if s < 365 * 86400:
        return f"-{s // (30 * 86400)}mo"
    return f"-{s // (365 * 86400)}y"


def _read_chip(store: Any, ref: Any) -> str:
    """``[created -10d · edited -2d · read 12× (3 human) · last -4h]`` —
    the focus header's age/read chip (gr524009). ``created``/``edited``
    come from the ref row (``edited`` only when its coarse age differs
    from ``created``'s — the "much later" case); the read count and
    ``last`` come from ``ref_reads``. A failed read (pre-0261 schema)
    drops the whole chip; a never-read ref shows age only."""
    created = getattr(ref, "created_at", None)
    if created is None:
        return ""
    try:
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT n_agent + n_human, n_human, "
                "GREATEST(last_agent_at, last_human_at) "
                "FROM ref_reads WHERE ref_id = %s",
                (int(ref.id),),
            ).fetchone()
    except Exception:
        log.debug("ref_reads chip read failed", exc_info=True)
        return ""
    parts = [f"created {_coarse_age(created)}"]
    updated = getattr(ref, "updated_at", None)
    if updated is not None and _coarse_age(updated) != _coarse_age(created):
        parts.append(f"edited {_coarse_age(updated)}")
    if row is not None:
        n_human = int(row[1])
        read = f"read {int(row[0])}×"
        if n_human:
            read += f" ({n_human} human)"
        parts.append(read)
        if row[2] is not None:
            parts.append(f"last {_coarse_age(row[2])}")
    return f"[{' · '.join(parts)}]"


def _row_age(ref: Any) -> str:
    """``-10d`` (+`` · edited -2d`` when the edit's coarse age differs) —
    the age-only chip a ring row carries (gr524009: read counts stay on
    the header, not on every neighbour label)."""
    created = getattr(ref, "created_at", None)
    if created is None:
        return ""
    age = _coarse_age(created)
    updated = getattr(ref, "updated_at", None)
    if updated is not None and _coarse_age(updated) != age:
        return f"{age} · edited {_coarse_age(updated)}"
    return age


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


def _preview_text(text: str) -> str:
    """A chunk's text as a one-line preview surface: LaTeX/HTML markup
    folded to its reading form (``<sup>`` author marks, ``$…$`` math and
    split ``|`` cells out — the author-line ``<sup>`` leak of gr520231)
    and emphasis wrappers unwrapped."""
    from precis.utils.text import markup_surface, strip_emphasis

    return strip_emphasis(markup_surface(text))


def _chunk_summary(block: Any) -> str:
    """A one-line summary for a chunk: its KeyBERT keywords, else its first line
    of text cleaned of markup, whitespace-collapsed and capped."""
    kws = block.keywords or []
    if kws:
        return _cap(", ".join(kws), _CHUNK_SUMMARY_CAP)
    return _cap(
        " ".join(_preview_text(getattr(block, "text", "") or "").split()),
        _CHUNK_SUMMARY_CAP,
    )


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
        # ``pc<N> ~<ord>`` like the ``#`` chunk-get header — the decimal
        # handle alone hides the block's ``~ord`` selector (gr520328).
        h = f"{_chunk_handle(kind, b)} ~{b.ord}"
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
    ring: RingFilter | None = None,
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
    head = _head(ref, kind, _head_chips(store, ref, kind))
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
            lead = _abstract_lead(store, ref, ref_id)
            if lead:
                body = f"{lead}\n\n{body}"
        block = f"{head}\n{body}"
    else:
        block = head  # no body chunks yet — head alone, but the ring still shows

    # The reference ring is a property of the ref, not its body — an empty
    # paper linked to a note still surfaces that note at fisheye+1hop.
    sections = _rings(store, ref_id, kind, ext, recall=recall, expand=expand, ring=ring)
    return "\n\n".join([block, *sections])


def _abstract_lead(store: Store, ref: Any, ref_id: int) -> str:
    """The whole-doc eye's lead line: ``meta.abstract`` (JATS-stripped)
    when on file, else the leading body text marked as such — an
    abstract *synthesised* from the chunks is labelled so it never reads
    as the publisher's (gr520232: the fisheye showed no abstract even
    when one was on file)."""
    abstract = (getattr(ref, "meta", None) or {}).get("abstract")
    if abstract:
        from precis.handlers._paper_format import _strip_jats

        return _cap(" ".join(_strip_jats(str(abstract)).split()), _SUMMARY_CAP)
    try:
        preview = store.chunks.abstract_previews([ref_id]).get(ref_id)
    except psycopg.Error:
        log.debug("abstract preview read failed", exc_info=True)
        return ""
    if not preview:
        return ""
    return "Abstract (from body text): " + _cap(
        " ".join(str(preview).split()), _SUMMARY_CAP
    )


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


def _note_body(store: Store, ref: Any, kind: str, *, cap: int) -> tuple[str, int]:
    """``(the eye's body chunk capped, its full length)``. A quest's chunks
    are its *logbook* — append-only ``quest_log`` entries — so its eye body
    is the one-chunk gist instead: the striving statement + rubric,
    composed from ``refs.title``/``refs.meta`` by
    :func:`precis.handlers.quest.quest_gist` (``fisheye-everywhere.md``
    in-scope 4). Composed, not stored: meta rubric edits can't drift a
    derived gist, and pre-change quests need no backfill."""
    if kind == "quest":
        from precis.handlers.quest import quest_gist

        gist = quest_gist(ref)
        return _cap(gist, cap), len(gist)
    return _ordered_body(store, int(ref.id), cap=cap)


def _render_note_eye(
    store: Store,
    handle: str,
    kind: str,
    ext: Extent,
    *,
    recall: bool = False,
    expand: str | None = None,
    ring: RingFilter | None = None,
) -> str:
    """A link-kind ref (memory / finding / …): the note at its extent (title →
    gist → body), and at ``fisheye+1hop`` its **link neighborhood** — every ref
    linked to it, *either direction*, with its relation type. For a memory the
    body *is* the note and the links are its point."""
    ref = _resolve_ref(store, handle)
    if ref is None or getattr(ref, "retired_at", None) is not None:
        raise ValueError(f"eye: no live {kind} ref for {handle!r}")
    chips = _head_chips(store, ref, kind)
    if ext <= Extent.TOC and not recall:
        return f"· {_head(ref, kind, chips)}"
    cap = _SUMMARY_CAP if ext <= Extent.SUMMARY else _VERBATIM_CAP
    body, total = _note_body(store, ref, kind, cap=cap)
    if cap == _VERBATIM_CAP and total > _VERBATIM_CAP:
        marker = _cut_marker(
            kind, handle_registry.format_handle(kind, int(ref.id)), _VERBATIM_CAP, total
        )
        body = f"{body}\n{marker}"
    block = f"{_head(ref, kind, chips)}\n{body}" if body else _head(ref, kind, chips)
    sections = _rings(
        store, int(ref.id), kind, ext, recall=recall, expand=expand, ring=ring
    )
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


@dataclass(frozen=True, slots=True)
class _RingNeighbour:
    """One collapsed ring row: the far ref, how many of the focus's edges
    reach it (parallel chunk-grounded edges — a claim hub's evidence, any
    passage-level fan-in — fold into one row, gr524665), and the far-end
    ``(chunk_id, ord)`` passages those edges arrive at, first-seen order.
    ``edges`` counts every collapsed edge, ref-level ones included."""

    ref_id: int
    edges: int = 1
    passages: tuple[tuple[int, int | None], ...] = ()


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
    hop1: dict[tuple[str, str], list[_RingNeighbour]],
    refs: dict[int, Any],
    ring: RingFilter | None = None,
) -> str:
    """:func:`_render_first_hop` with the hub listing switched on for a
    ``section:index`` memory (its ``contains`` members are not capped)."""
    notes = _hub_member_notes(store, ref_id, hop1, refs)
    # Chips only for the ids the ring prints — ``refs`` holds every
    # fetched neighbour (retired and ring-filtered included).
    chips = _row_chips(
        store,
        {
            n.ref_id: refs[n.ref_id]
            for ns in hop1.values()
            for n in ns
            if n.ref_id in refs
        },
        ring=ring,
    )
    return _render_first_hop(
        hop1,
        refs,
        chips=chips,
        hub_members=notes,
        more_hint=_more_hint_factory(store, ref_id, hop1),
        hub_handle=handle_registry.format_handle("memory", ref_id)
        if notes is not None
        else None,
    )


def _row_chips(
    store: Store, refs: Any, *, ring: RingFilter | None = None
) -> dict[int, str]:
    """``{ref id: chips}`` for a page of neighbour rows — one
    ``ref_tags_bulk`` read (:func:`precis.utils.tag_chips.chips_bulk`),
    the ring's ``SPACE:`` values dropped as context-restating."""
    from precis.utils import tag_chips

    return tag_chips.chips_bulk(
        store,
        {int(i): str(getattr(r, "kind", "") or "") for i, r in refs.items()},
        ring_spaces=ring.spaces if ring is not None else None,
    )


def _more_hint_factory(
    store: Store, ref_id: int, hop1: dict[tuple[str, str], list[_RingNeighbour]]
) -> Any:
    """``(label, neighbours) -> ' · get(…) …'`` for a group's ``… +N more``
    line, or ``None`` when no group overflows (no extra queries then).

    The call re-reads the focus with ``q='<label>'``, which lists that whole
    group (:func:`_expand_first_hop`). For a memory whose group members share
    a ``section:<type>`` tag a scoped ``search`` is offered too."""
    if not any(len(ns) > _NEIGHBOR_GROUP_CAP for ns in hop1.values()):
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

    def hint(label: str, ns: list[_RingNeighbour]) -> str:
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
            tagged = store.ref_tags_bulk([n.ref_id for n in ns])
            shared = set.intersection(
                *(
                    {
                        v
                        for _ns, v in tagged.get(o, [])
                        if v.startswith("section:") and v != _HUB_TAG
                    }
                    for o in (n.ref_id for n in ns)
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
    hop1: dict[tuple[str, str], list[_RingNeighbour]],
    refs: dict[int, Any],
) -> dict[int, str] | None:
    """For a ``section:index`` memory: ``{member id: hook}`` for each live
    ``contains`` neighbour (``meta.hook``, else the body's first line, cut to
    :data:`_HUB_HOOK_CAP`), band-ordered — ACTIVE members head the listing
    (:func:`precis.utils.memory_hubs.band_order`); ``None`` for any other ref."""
    if not any(label == _HUB_MEMBER_LABEL for _g, label in hop1):
        return None
    if not _is_hub(store, ref_id):
        return None
    members = [
        n.ref_id
        for (_g, label), ns in hop1.items()
        if label == _HUB_MEMBER_LABEL
        for n in ns
    ]
    from precis.utils.memory_hubs import band_order

    shown = band_order(store, members)[:_HUB_LIST_CAP]
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


def _space_tags(store: Store, ref_ids: Any) -> dict[int, frozenset[str]]:
    """``{ref id: its SPACE:-axis values}`` for many refs in one query —
    ``frozenset()`` for a ref carrying none (outside every partition)."""
    ids = {int(i) for i in ref_ids}
    if not ids:
        return {}
    tagged = store.ref_tags_bulk(list(ids))
    return {
        rid: frozenset(v for ns, v in pairs if ns == "SPACE")
        for rid, pairs in tagged.items()
    }


def _ring_kept(
    store: Store,
    ring: RingFilter,
    refs: dict[int, Any],
    pairs: list[tuple[int, int | None]],
) -> list[tuple[int, int | None]]:
    """``pairs`` (``(neighbour ref, far chunk id)`` per edge) narrowed by
    ``ring``: kind from the ref row, ``SPACE:`` tags in one bulk read (only
    when the filter uses the axis)."""
    out = pairs
    if ring.kinds is not None:
        out = [p for p in out if str(getattr(refs[p[0]], "kind", "")) in ring.kinds]
    if ring.spaces is not None:
        space_of = _space_tags(store, {o for o, _c in out})
        out = [p for p in out if space_of.get(p[0], frozenset()) & ring.spaces]
    return out


def _chunk_ords(store: Store, chunk_ids: Any) -> dict[int, int]:
    """``{chunk_id: ord}`` for a set of chunk ids in one query — the
    ``~<ord>`` suffix a collapsed row's ``pc<id>`` handles print. A read
    failure reads as no ords, never a broken render."""
    ids = {int(c) for c in chunk_ids}
    if not ids:
        return {}
    try:
        with store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT chunk_id, ord FROM chunks WHERE chunk_id = ANY(%s)",
                (list(ids),),
            ).fetchall()
    except Exception:
        log.debug("ring passage ord read failed", exc_info=True)
        return {}
    return {int(r[0]): int(r[1]) for r in rows}


def _collapse_pairs(
    pairs: list[tuple[int, int | None]], ords: dict[int, int]
) -> list[_RingNeighbour]:
    """``(ref, far chunk)`` per edge → one :class:`_RingNeighbour` per ref —
    first-seen order, every edge counted, far-end chunks deduped (gr524665:
    a claim hub's ``paper --corroborates--> hub`` fan-in grounds at N chunks
    of one source and must render as one row, not N identical rows)."""
    acc: dict[int, tuple[int, list[int]]] = {}
    order: list[int] = []
    for oid, ck in pairs:
        slot = acc.get(oid)
        if slot is None:
            acc[oid] = slot = (0, [])
            order.append(oid)
        edges, chunks = slot
        acc[oid] = (edges + 1, chunks)
        if ck is not None and ck not in chunks:
            chunks.append(ck)
    return [
        _RingNeighbour(
            oid,
            edges=acc[oid][0],
            passages=tuple((c, ords.get(c)) for c in acc[oid][1]),
        )
        for oid in order
    ]


def _first_hop(
    store: Store, ref_id: int, *, ring: RingFilter | None = None
) -> tuple[dict[tuple[str, str], list[_RingNeighbour]], dict[int, Any]]:
    """``(live neighbours by (ring group, label), the neighbour refs)`` —
    the collected form of :func:`_link_neighbors`, shared with the second
    hop, which walks out from exactly these neighbours. ``ring`` narrows
    the neighbours kept; ``RING_GROUP_KIND_RANK`` then orders each label
    block (Roadmap lists sub-quests before papers).

    The edges come from :meth:`Store.neighbourhood` (the web graph panel's
    read), so both surfaces see one neighbour set: retired far ends and
    self-links dropped, an inbound edge's relation read through the
    ``relations.inverse_slug`` map. The ring keeps its own per-label cap,
    so the read is uncapped here; it is walked oldest link first. Edges
    sharing a far ref collapse to one :class:`_RingNeighbour` carrying the
    count and the far-end chunk ids they ground at."""
    by_label = _bucket_edges(store, ref_id, _neighbourhood_edges(store, ref_id))
    if not by_label:
        return {}, {}
    refs = store.fetch_refs_by_ids(
        {o for pairs in by_label.values() for o, _c in pairs}
    )
    live = {
        key: [p for p in pairs if _live(refs, p[0])] for key, pairs in by_label.items()
    }
    live = {key: pairs for key, pairs in live.items() if pairs}
    if ring is not None:
        live = {
            key: kept
            for key, pairs in live.items()
            if (kept := _ring_kept(store, ring, refs, pairs))
        }
    ords = _chunk_ords(
        store, (c for pairs in live.values() for _o, c in pairs if c is not None)
    )
    out = {key: _collapse_pairs(pairs, ords) for key, pairs in live.items()}
    for (group, _label), ns in out.items():
        rank = RING_GROUP_KIND_RANK.get(group)
        if rank:
            pri = {k: i for i, k in enumerate(rank)}
            ns.sort(
                key=lambda n: pri.get(
                    str(getattr(refs[n.ref_id], "kind", "")), len(rank)
                )
            )
    return out, refs


#: ``cap`` for the ring's :meth:`Store.neighbourhood` read: past any real
#: ref's link count (the ring caps per label at render time), inside int4.
_NBH_UNCAPPED = 1_000_000


def _neighbourhood_edges(
    store: Store, ref_id: int
) -> list[tuple[int, int, Any, int | None, int | None]]:
    """``ref_id``'s live first-hop edges as stored ``(src, dst, relation,
    src_chunk_id, dst_chunk_id)``, oldest link first, read through
    :meth:`Store.neighbourhood` with ``link_ids=True`` so a chunk-grounded
    edge keeps the chunk it grounds at (gr524665).

    ``neighbourhood`` presents an inbound edge under its inverse slug; the
    inverse map is an involution, so mapping it back gives the stored slug
    that :func:`_bucket_edges` labels from this side."""
    focus = store.fetch_refs_by_ids({ref_id}).get(ref_id)
    if focus is None:
        return []
    try:
        nbh = store.neighbourhood(
            str(focus.kind), ref_id, cap=_NBH_UNCAPPED, link_ids=True
        )
    except NotFound:
        return []
    far_of = {
        handle_registry.try_format(n["kind"], n["id"]) or f"{n['kind']}:{n['id']}": n[
            "id"
        ]
        for n in nbh["nodes"]
    }
    inverses, _symmetric = _relation_reading(store)
    edges: list[tuple[int, int, Any, int | None, int | None]] = []
    for e in reversed(nbh["edges"]):
        sck = e.get("src_chunk_id")
        dck = e.get("dst_chunk_id")
        if e["dir"] == "out":
            edges.append((ref_id, int(far_of[e["dst"]]), e["rel"], sck, dck))
        else:
            rel = inverses.get(e["rel"], e["rel"])
            edges.append((int(far_of[e["src"]]), ref_id, rel, sck, dck))
    return edges


def _live(refs: dict[int, Any], oid: int) -> bool:
    r = refs.get(oid)
    return r is not None and getattr(r, "retired_at", None) is None


def _bucket_edges(
    store: Store,
    ref_id: int,
    edges: list[tuple[int, int, Any, int | None, int | None]],
) -> dict[tuple[str, str], list[tuple[int, int | None]]]:
    """Group ``ref_id``'s ``(src, dst, relation, src_chunk, dst_chunk)``
    edges by ``(ring group, label as seen from here)``, listing the other
    endpoint of each as ``(ref, far chunk id)`` — the chunk the edge
    grounds at on the neighbour's side. Dead ends included; the caller
    filters by liveness and collapses."""
    inverses, symmetric = _relation_reading(store)
    by_label: dict[tuple[str, str], list[tuple[int, int | None]]] = {}
    for src, dst, rel, sck, dck in edges:
        group = ring_group(str(rel)) if rel is not None else None
        if group is None:
            continue
        outbound = src == ref_id
        other = dst if outbound else src
        if other == ref_id:
            continue
        other_chunk = dck if outbound else sck
        label = _as_seen_from_here(
            str(rel), outbound=outbound, inverses=inverses, symmetric=symmetric
        )
        by_label.setdefault((group, label), []).append(
            (other, int(other_chunk) if other_chunk is not None else None)
        )
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
            "SELECT src_ref_id, dst_ref_id, relation, src_chunk_id, dst_chunk_id "
            "FROM links "
            "WHERE src_ref_id = ANY(%s) OR dst_ref_id = ANY(%s)",
            (ref_ids, ref_ids),
        ).fetchall()
    wanted = set(ref_ids)
    edges: dict[int, list[tuple[int, int, Any, Any, Any]]] = {r: [] for r in ref_ids}
    for src, dst, rel, sck, dck in rows:
        for end in {int(src), int(dst)} & wanted:
            edges[end].append((int(src), int(dst), rel, sck, dck))
    buckets = {r: _bucket_edges(store, r, e) for r, e in edges.items()}
    others = {o for b in buckets.values() for pairs in b.values() for o, _c in pairs}
    refs = store.fetch_refs_by_ids(others | {int(src) for src, _d, _r, _s, _k in rows})
    shape: dict[int, tuple[int, int, int]] = {}
    for r, by_label in buckets.items():
        # inbound edges from a retired source are unremovable from this end
        # (the source is Gone), so they are not this ref's dead links
        removable = {dst for src, dst, _rel, _s, _k in edges[r] if src == r}
        orphaned = {
            src
            for src, dst, _rel, _s, _k in edges[r]
            if dst == r and src not in removable and not _live(refs, src)
        }
        # an outbound ``supersedes`` edge to a retired ref is the merge record
        # precis-memory-help prescribes (link supersedes, then delete the old
        # node), not a dead end to repoint
        tombstones = {
            dst
            for src, dst, rel, _s, _k in edges[r]
            if src == r and rel == "supersedes" and not _live(refs, dst)
        }
        live_groups = [
            {o for o, _c in pairs if _live(refs, o)}
            for (_g, label), pairs in by_label.items()
            if not (r in hubs and label == _HUB_MEMBER_LABEL)
        ]
        live_all = [
            {o for o, _c in pairs if _live(refs, o)} for pairs in by_label.values()
        ]
        every = {o for pairs in by_label.values() for o, _c in pairs}
        live = set().union(*live_all) if live_all else set()
        shape[r] = (
            len(live),
            max(map(len, live_groups), default=0),
            len(every - live - orphaned - tombstones),
        )
    return shape


#: Most neighbours an expanded first-hop group lists (``q='<label>'``).
_FIRST_HOP_EXPAND_CAP = 500


def _expand_first_hop(
    store: Store,
    ref_id: int,
    hop1: dict[tuple[str, str], list[_RingNeighbour]],
    refs: dict[int, Any],
    label: str,
    ring: RingFilter | None = None,
) -> str:
    """One first-hop label's live neighbours, uncapped by the ring's per-group
    cap (bounded by :data:`_FIRST_HOP_EXPAND_CAP`). A ``section:index``
    memory's ``contains`` expansion keeps the hub listing's band order."""
    ns = [n for (_g, lab), group_ns in hop1.items() if lab == label for n in group_ns]
    if not ns:
        known = ", ".join(sorted({lab for _g, lab in hop1})) or "none"
        raise ValueError(f"eye: no first-hop group {label!r}; groups here: {known}")
    if label == _HUB_MEMBER_LABEL and _is_hub(store, ref_id):
        from precis.utils.memory_hubs import band_order

        ordered = band_order(store, [n.ref_id for n in ns])
        by_id = {n.ref_id: n for n in ns}
        ns = [by_id[o] for o in ordered]
    chips = _row_chips(
        store, {n.ref_id: refs[n.ref_id] for n in ns if n.ref_id in refs}, ring=ring
    )
    lines = [f"— linked (1 hop) · {label} ({len(ns)}) —"]
    lines.extend(
        f"    {_ring_row(refs[n.ref_id], n, chips=chips.get(n.ref_id, ''))}"
        for n in ns[:_FIRST_HOP_EXPAND_CAP]
    )
    if len(ns) > _FIRST_HOP_EXPAND_CAP:
        lines.append(f"    … +{len(ns) - _FIRST_HOP_EXPAND_CAP} more")
    return "\n".join(lines)


def _passage_suffix(n: _RingNeighbour, kind: str) -> str:
    """A collapsed row's tail (gr524665): ``  ×N`` when parallel edges folded
    into it, plus the far-end chunk handles those edges ground at —
    ``pc<id> ~<ord>``, the doc eye's form — first few, then ``· +N more``.
    A kind with no chunk code (memory chunks are not agent-addressable)
    drops the handle but keeps the count."""
    handles: list[str] = []
    for cid, ord_ in n.passages:
        h = handle_registry.try_format(kind, cid, chunk=True)
        if h is not None:
            handles.append(f"{h} ~{ord_}" if ord_ is not None else h)
    shown = handles[:_RING_PASSAGE_CAP]
    listed = " · ".join(shown)
    if len(handles) > len(shown):
        listed += f" · +{len(handles) - len(shown)} more"
    if n.edges > 1:
        return f"  ×{n.edges} ({listed})" if listed else f"  ×{n.edges}"
    return f"  ({listed})" if listed else ""


def _ring_row(ref: Any, n: _RingNeighbour, *, chips: str = "") -> str:
    """One ring row: the neighbour label plus its collapsed-edge tail."""
    return _neighbor_label(ref, n.ref_id, chips=chips) + _passage_suffix(
        n, str(getattr(ref, "kind", "") or "")
    )


def _render_first_hop(
    by_label: dict[tuple[str, str], list[_RingNeighbour]],
    refs: dict[int, Any],
    chips: dict[int, str] | None = None,
    hub_members: dict[int, str] | None = None,
    more_hint: Any = None,
    hub_handle: str | None = None,
) -> str:
    chips = chips or {}
    order = {group: i for i, group in enumerate(RING_GROUPS)}
    lines = ["— linked (1 hop) —"]
    heading = None
    for group, label in sorted(by_label, key=lambda k: (order[k[0]], k[1])):
        live_ids = by_label[(group, label)]
        if group != heading:
            lines.append(f"{group}:")
            heading = group
        if hub_members is not None and label == _HUB_MEMBER_LABEL:
            live = {n.ref_id for n in live_ids}
            listed = [o for o in hub_members if o in live]
            if len(live_ids) == 1:
                oid = live_ids[0].ref_id
                hook = hub_members.get(oid, "")
                row = _neighbor_label(refs[oid], oid, chips=chips.get(oid, ""))
                lines.append(
                    f"  {label}: {row} — {hook}" if hook else f"  {label}: {row}"
                )
            else:
                lines.append(f"  {label} ({len(live_ids)}):")
                for oid in listed:
                    hook = hub_members.get(oid, "")
                    row = _neighbor_label(refs[oid], oid, chips=chips.get(oid, ""))
                    lines.append(f"    {row} — {hook}" if hook else f"    {row}")
                if len(live_ids) > len(listed):
                    lines.append(
                        f"    … +{len(live_ids) - len(listed)} more · "
                        f"get(kind='memory', id='{hub_handle}', "
                        f"extent='fisheye+1hop', q='{label}')"
                    )
            continue
        if len(live_ids) == 1:
            n = live_ids[0]
            lines.append(
                f"  {label}: "
                f"{_ring_row(refs[n.ref_id], n, chips=chips.get(n.ref_id, ''))}"
            )
            continue
        lines.append(f"  {label} ({len(live_ids)}):")
        for n in live_ids[:_NEIGHBOR_GROUP_CAP]:
            lines.append(
                f"    {_ring_row(refs[n.ref_id], n, chips=chips.get(n.ref_id, ''))}"
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


def _neighbor_label(ref: Any, ref_id: int, *, chips: str = "") -> str:
    oh = _handle_with_file(getattr(ref, "kind", "?"), ref, ref_id)
    title = " ".join((getattr(ref, "title", None) or "").split())
    if getattr(ref, "kind", None) == "part":
        # gr520243: a part with a datasheet brief labels itself by its
        # one-line distillation, not the raw catalog title — a ring that
        # names parts (a todo citing C-numbers) then reads what each is.
        brief = ((getattr(ref, "meta", None) or {}).get("datasheet_facts") or {}).get(
            "brief"
        ) or {}
        one = " ".join(str(brief.get("one_line") or "").split())
        if one:
            title = one
    if len(title) > _NEIGHBOR_TITLE_CAP:
        title = title[: _NEIGHBOR_TITLE_CAP - 1].rstrip() + "…"
    label = f"{oh} — {title}" if title else oh
    age = _row_age(ref)
    if age:
        label = f"{label} · {age}"
    return f"{label}  {chips}" if chips else label


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
    hop1: dict[tuple[str, str], list[_RingNeighbour]],
    *,
    expand: str | None = None,
    ring: RingFilter | None = None,
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
    hop1_ids = {n.ref_id for ns in hop1.values() for n in ns}
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
            if (
                ring is not None
                and ring.kinds is not None
                and (str(far_kind) not in ring.kinds)
            ):
                continue
            label = _as_seen_from_here(
                str(rel), outbound=outbound, inverses=inverses, symmetric=symmetric
            )
            groups.setdefault((str(far_kind), label), set()).add(far)
    if ring is not None and ring.spaces is not None and groups:
        space_of = _space_tags(store, {o for ids in groups.values() for o in ids})
        groups = {
            key: kept
            for key, ids in groups.items()
            if (kept := {o for o in ids if space_of.get(o, frozenset()) & ring.spaces})
        }
    if not groups:
        if expand is not None:
            raise ValueError(
                f"eye: no second-hop group {expand!r}; the second hop is empty"
            )
        return ""
    floor = "≥" if len(rows) >= _SECOND_HOP_ROW_CAP else ""
    if expand is not None:
        return _expand_second_hop(store, groups, expand, ring=ring)
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
    store: Store,
    groups: dict[tuple[str, str], set[int]],
    expand: str,
    *,
    ring: RingFilter | None = None,
) -> str:
    kind, _, label = expand.partition(":")
    ids = groups.get((kind.strip(), label.strip()))
    if not ids:
        known = ", ".join(f"{k}:{lab}" for k, lab in sorted(groups))
        raise ValueError(f"eye: no second-hop group {expand!r}; groups here: {known}")
    shown = sorted(ids)[:_SECOND_HOP_EXPAND_CAP]
    refs = store.fetch_refs_by_ids(shown)
    chips = _row_chips(store, refs, ring=ring)
    lines = [f"— second hop: {len(ids)} {kind} via {label} —"]
    lines.extend(
        f"  {_neighbor_label(refs[i], i, chips=chips.get(i, ''))}"
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
#: Public name — the machine-edge write gate
#: (:mod:`precis.store._links_ops`) anchors its "RAG would co-retrieve the
#: pair" check to this same floor, and so does the gardener relink/prune
#: band (``graph.garden.candidates.RELINK_MIN_DISTANCE``).
RECALL_MAX_DISTANCE = 0.6


def _recall(store: Store, ref_id: int, kind: str, *, ring: RingFilter | None) -> str:
    """The ``+recall`` rung: the k nearest chunks by embedding, same kind +
    ``finding``, each with its gist line. Similarity, not edges — the ring
    above says what is linked; this says what is *about* the same thing and
    was never linked (``refeye``'s module docstring draws the same line).
    A ring filter narrows this rung too: kinds intersect the same-kind +
    ``finding`` leg, ``spaces`` override the focus's own partition.
    """
    seed = store.chunks.seed_chunk_for_ref(ref_id)
    vec = store.chunks.get_chunk_vector(seed) if seed is not None else None
    if vec is None:
        return "— recall: no embedded chunk on this ref yet —"
    kinds = sorted({kind, "finding"})
    if ring is not None and ring.kinds is not None:
        kinds = sorted(set(kinds) & ring.kinds)
    # A SPACE: tag is a partition (research / repo-dev / personal): recall
    # stays inside the focus's own, filtered in the query so k fills from it —
    # or inside the space a ring filter selected instead.
    if ring is not None and ring.spaces is not None:
        space = sorted(f"SPACE:{s}" for s in ring.spaces)
    else:
        space = [str(t) for t in store.tags_for(ref_id) if t.prefix == "SPACE"]
    if not kinds:
        return "— recall (nearest by embedding) —\n  — the filter excludes this leg —"
    hits = store.chunks.search_chunks_semantic(
        query_vec=vec,
        kinds=kinds,
        tags=space or None,
        limit=_RECALL_K * 3,
        max_distance=RECALL_MAX_DISTANCE,
        exclude_ref_ids=[ref_id],
        # memory-like kinds embed their whole-ref card (ord -1), not a body
        # chunk; let it match, as `seed_chunk_for_ref` does for the seed.
        card_kinds=("card_combined",),
    )
    chips = _row_chips(
        store,
        {int(ref.id): ref for _b, ref, _d in hits},
        ring=ring,
    )
    seen: set[int] = set()
    lines = [f"— recall (nearest by embedding, {'+'.join(kinds)}, k≤{_RECALL_K}) —"]
    for block, ref, dist in hits:
        rid = int(ref.id)
        if rid in seen:
            continue  # one line per ref: its nearest chunk speaks for it
        seen.add(rid)
        gist = _cap(" ".join((block.text or "").split()), _CHUNK_SUMMARY_CAP)
        lines.append(
            f"  {_neighbor_label(ref, rid, chips=chips.get(rid, ''))}  ({1 - dist:.2f})"
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
    ring: RingFilter | None,
) -> list[str]:
    """Every neighbourhood section a non-tree eye appends at ``ext``:
    the first hop at ``fisheye+1hop`` and up, the second hop at
    ``fisheye+2hop``, recall when asked. ``ring`` narrows all three."""
    sections: list[str] = []
    if ext >= Extent.HOP1:
        hop1, refs = _first_hop(store, ref_id, ring=ring)
        first_hop_label = expand if expand is not None and ":" not in expand else None
        if first_hop_label is not None:
            sections.append(
                _expand_first_hop(store, ref_id, hop1, refs, first_hop_label, ring=ring)
            )
        elif expand is None:
            sections.append(_render_ring(store, ref_id, hop1, refs, ring=ring))
        if ext >= Extent.HOP2 and first_hop_label is None:
            sections.append(_second_hop(store, ref_id, hop1, expand=expand, ring=ring))
    if recall:
        sections.append(_recall(store, ref_id, kind, ring=ring))
    return [s for s in sections if s]
