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
  A structure's incoming quest-log citations stay as individual entries,
  grouped by stored section or entry type, so the candidate exposes its
  reasoning instead of collapsing it into a single quest row.

- **Skill eyes** (``sk:<slug>``): a skill is file-backed, not refs-backed, so
  it has no numeric pk for ``handle_registry.parse``'s decimal grammar
  (keeps ``skill`` on its existing slug addressing rather than
  folding it into the registry — see that module's docstring). A skill eye
  is dispatched on its ``sk:`` prefix ahead of the decimal parse and renders
  straight from the skill corpus (``handlers.skill``'s own accessors —
  ``_load_skill`` / ``_skill_title``, the same ones ``SkillHandler.get``
  uses). No *spatial* neighborhood (a file has no corpus position), but it
  does have a **link ring**: the ``[[slug]]`` wikilinks, derived from the
  shared :class:`~precis.skill_index.SkillGraph` rather than stored edges
  (gr572706 ruling — outbound ``see-also``, inbound ``see-also-by``).
  ``toc``/``none`` collapse to a one-line bookmark, ``summary``…``fisheye``
  render the verbatim body, ``fisheye+1hop`` appends the wikilink ring and
  ``fisheye+2hop`` the second hop, both in the link-kind shape.

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

from precis._result_budget import size_cell
from precis.errors import BadInput, NotFound
from precis.store.types import _CLOSED_VOCAB, Tag
from precis.utils import handle_registry
from precis.utils.entity_courtesy import (
    courtesy_lines,
    entity_handles,
    safe_entity_states,
)
from precis.utils.link_tag_ops import link_reason_suffix
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


def _scope_masks(ref: Any) -> bool:
    """True when ``ref`` renders as an opaque neighbour on this surface —
    a conv outside the bound read scope or a ref of a gated kind
    (``precis.utils.surface_scope``). The ring prints the placeholder,
    never the title, keywords or excerpt; applied at every hop
    (gr555998 follow-up, ``PRECIS_CONV_READ_SCOPE`` /
    ``PRECIS_KINDS_DISABLED``)."""
    from precis.utils.surface_scope import masked_ref_placeholder

    return masked_ref_placeholder(ref) is not None


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
    """A narrowing on a link-kind eye's rings — by neighbour kind, by the
    ``SPACE:`` tag axis and/or by arbitrary tags
    (``fisheye-everywhere.md`` in-scope 1, gr605669), so e.g. repo-dev and
    research neighbourhoods — or one task's scratch — can be shown apart.
    ``None`` on an axis leaves it open; ``spaces`` drops every neighbour
    carrying no ``SPACE:`` tag (the axis is a partition); ``tags`` drops
    every neighbour not carrying *all* the listed tags (the AND of
    ``search(tags=[…])`` — the ``scratch-for:td<N>`` owner tag that
    partitions ``SPACE:scratch`` by task). The focus itself always
    renders; the tree eyes (reading order, not link edges) have no ring
    to narrow, and a skill eye's wikilink ring narrows only on
    ``kinds`` — a file carries no ``SPACE:`` or open tags, so either of
    those filters empties it.
    """

    kinds: frozenset[str] | None = None
    spaces: frozenset[str] | None = None
    tags: frozenset[str] | None = None

    @classmethod
    def parse(cls, raw: Any) -> RingFilter | None:
        """``ring=``'s wire form → a :class:`RingFilter`: ``None``, an
        existing ``RingFilter``, or a dict ``{"kinds": <csv|list>,
        "space": <csv|list>, "tags": <csv|list>}`` (``space`` values may
        carry or omit the ``SPACE:`` prefix; ``tags`` entries are tag
        spellings like ``scratch-for:td575121``, canonicalised by
        :meth:`Tag.parse` — open tags fold lowercase as stored). Raises
        ``ValueError`` on a non-dict or an unknown kind — the same error
        channel as an unknown rung."""
        if raw is None or isinstance(raw, cls):
            return raw
        if not isinstance(raw, dict):
            raise ValueError(
                f"eye: ring= takes a dict like {{'kinds': 'memory', "
                f"'space': 'repo-dev'}}, got {raw!r}"
            )
        kinds = _csv_set(raw.get("kinds"))
        spaces = _csv_set(raw.get("space"))
        tags = _csv_set(raw.get("tags"))
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
        normed_tags = (
            frozenset(_canon_tag(v) for v in tags) if tags is not None else None
        )
        return cls(kinds=kinds, spaces=normed, tags=normed_tags)


def _canon_tag(value: str) -> str:
    """A tag spelling → the canonical string a ref's tags are compared
    against: ``SPACE:scratch`` stays ``SPACE:scratch``; an open tag like
    ``scratch-for:TD575121`` folds to ``scratch-for:td575121``, the form
    :meth:`Tag.open` stores."""
    t = Tag.parse(str(value))
    if t.namespace == "closed":
        return f"{t.prefix}:{t.value}"
    return t.value


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
    doc eye renders (first hop, second hop, recall) by neighbour kind,
    ``SPACE:`` tag and/or arbitrary tags (:class:`RingFilter`)."""
    ext, recall = parse_extent(extent)
    filt = RingFilter.parse(ring)
    if q is not None and (ext < Extent.HOP1 or (ext < Extent.HOP2 and ":" in q)):
        raise ValueError(
            "eye: q='<label>' lists one first-hop group at fisheye+1hop; "
            "q='<kind>:<label>' expands a second-hop group at fisheye+2hop"
        )
    if handle.startswith(_SKILL_HANDLE_PREFIX):
        return _render_skill_eye(handle, ext, expand=q, ring=filt)
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
        ext = Extent.parse(rung)
    except (KeyError, ValueError) as e:
        raise ValueError(f"eye: unknown extent {extent!r}") from e
    return _apply_bench_switches(ext, recall)


def _apply_bench_switches(ext: Extent, recall: bool) -> tuple[Extent, bool]:
    """The memory-ablation switches (``precis.bench.switches``) on a parsed
    extent. Every eye render funnels through :func:`parse_extent`, so this
    is the one place the ladder's layers are cut: a ``kwd`` / ``summary``
    rung that is switched off is refused (the harness must not read a rung
    the arm removed), any ``fisheye*`` rung collapses to ``verbatim`` — so
    :func:`_rings` never fetches a hop — and ``+recall`` is dropped, so
    :func:`_recall` never runs its embedding search. All-on changes nothing.
    """
    from precis.bench import switches as bench_switches

    sw = bench_switches.current()
    if sw.all_on:
        return ext, recall
    if (ext is Extent.TOC and not sw.kwd) or (ext is Extent.SUMMARY and not sw.summary):
        raise BadInput(
            f"eye: the {ext.label!r} rung is switched off for this server "
            f"({bench_switches.ENV_VAR})",
            next="extent='verbatim' reads the node whole",
        )
    if not sw.fisheye and ext > Extent.FULL:
        ext = Extent.FULL
    return ext, recall and sw.recall


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


# ── skill kind: file-backed, verbatim + the wikilink ring ─────────────

#: The labels a skill eye's ring renders. A skill carries no stored edges
#: (the gr572706 ruling: no link rows for file-backed kinds) — its ring is
#: derived from ``[[slug]]`` wikilinks via the shared
#: :class:`~precis.skill_index.SkillGraph`: ``see-also`` the links the file
#: wrote, ``see-also-by`` the skills linking in. The ingest gate
#: hard-fails a dangling wikilink, so every row below resolves.
_SKILL_OUT_LABEL = "see-also"
_SKILL_IN_LABEL = "see-also-by"


def _skill_link_groups(slug: str) -> dict[str, tuple[str, ...]]:
    """``{label: (slugs,)}`` — ``slug``'s wikilink edges by direction.

    Under a read connection a ``scope: write`` neighbour drops out, same
    as the served footer's ``linked:`` line (gr512233): following the
    pointer would only serve the stub.
    """
    from precis.handlers.skill import _get_skill_graph, _skill_is_write_only
    from precis.runtime.scope import current_scope

    graph = _get_skill_graph()
    groups = {
        _SKILL_OUT_LABEL: tuple(graph.outbound.get(slug, ())),
        _SKILL_IN_LABEL: tuple(graph.inbound.get(slug, ())),
    }
    if current_scope() == "read":
        groups = {
            label: tuple(s for s in slugs if not _skill_is_write_only(s))
            for label, slugs in groups.items()
        }
    return groups


def _skill_row(slug: str) -> str:
    """One ring row: ``sk:<slug> — <title>`` (title capped like any
    neighbour's)."""
    from precis.handlers.skill import _skill_title

    title = _cap(" ".join((_skill_title(slug) or "").split()), _NEIGHBOR_TITLE_CAP)
    handle = f"{_SKILL_HANDLE_PREFIX}{slug}"
    return f"{handle} — {title}" if title else handle


def _skill_first_hop(slug: str, groups: dict[str, tuple[str, ...]]) -> str:
    """``— linked (1 hop) —`` over the wikilink groups in the link-kind
    shape — a single-member group inline, a larger one a capped block
    with a ``… +N more`` line naming the ``q='<label>'`` expansion call."""
    lines = ["— linked (1 hop) —"]
    for label in (_SKILL_OUT_LABEL, _SKILL_IN_LABEL):
        slugs = groups.get(label, ())
        if not slugs:
            continue
        if len(slugs) == 1:
            lines.append(f"  {label}: {_skill_row(slugs[0])}")
            continue
        lines.append(f"  {label} ({len(slugs)}):")
        lines.extend(f"    {_skill_row(s)}" for s in slugs[:_NEIGHBOR_GROUP_CAP])
        if len(slugs) > _NEIGHBOR_GROUP_CAP:
            lines.append(
                f"    … +{len(slugs) - _NEIGHBOR_GROUP_CAP} more · "
                f"get(kind='skill', id={slug!r}, "
                f"extent='fisheye+1hop', q={label!r})"
            )
    return "\n".join(lines) if len(lines) > 1 else ""


def _skill_expand_first_hop(label: str, groups: dict[str, tuple[str, ...]]) -> str:
    """One first-hop label's members, uncapped — the skill eye's
    ``q='<label>'`` expansion (:func:`_expand_first_hop`'s shape)."""
    slugs = groups.get(label, ())
    if not slugs:
        known = ", ".join(lab for lab, s in groups.items() if s) or "none"
        raise ValueError(f"eye: no first-hop group {label!r}; groups here: {known}")
    lines = [f"— linked (1 hop) · {label} ({len(slugs)}) —"]
    lines.extend(f"    {_skill_row(s)}" for s in slugs)
    return "\n".join(lines)


def _skill_second_hop(
    slug: str, groups: dict[str, tuple[str, ...]]
) -> dict[str, tuple[str, ...]]:
    """``{label: (slugs,)}`` — what the first-hop neighbours link to, the
    same labels read from the neighbour's side, minus the focus and the
    ring itself (:func:`_second_hop`'s grouping)."""
    first = {s for slugs in groups.values() for s in slugs}
    out: dict[str, set[str]] = {_SKILL_OUT_LABEL: set(), _SKILL_IN_LABEL: set()}
    for neighbour in first:
        for label, slugs in _skill_link_groups(neighbour).items():
            out[label].update(s for s in slugs if s != slug and s not in first)
    return {label: tuple(sorted(ids)) for label, ids in out.items() if ids}


def _skill_render_second_hop(second: dict[str, tuple[str, ...]], n_first: int) -> str:
    """The second hop as counts per label, in the link-kind shape."""
    lines = [
        f"— second hop ({n_first} neighbours out; counts — "
        "expand one with q='skill:<label>') —"
    ]
    for label in (_SKILL_OUT_LABEL, _SKILL_IN_LABEL):
        if slugs := second.get(label):
            lines.append(f"  {len(slugs)} skill via {label}")
    return "\n".join(lines)


def _skill_expand_second_hop(expand: str, second: dict[str, tuple[str, ...]]) -> str:
    """One second-hop group's members — ``q='skill:<label>'``, the
    ``<kind>:<label>`` grammar the link kinds use."""
    kind, _, label = expand.partition(":")
    slugs = second.get(label.strip()) if kind.strip() == "skill" else ()
    if not slugs:
        known = (
            ", ".join(f"skill:{lab}" for lab, s in sorted(second.items()) if s)
            or "none"
        )
        raise ValueError(f"eye: no second-hop group {expand!r}; groups here: {known}")
    lines = [f"— second hop: {len(slugs)} skill via {label.strip()} —"]
    lines.extend(f"    {_skill_row(s)}" for s in slugs)
    return "\n".join(lines)


def _render_skill_eye(
    handle: str,
    ext: Extent,
    *,
    expand: str | None = None,
    ring: RingFilter | None = None,
) -> str:
    """A skill eye (``sk:<slug>``): file-backed, so it renders straight from
    the skill corpus via ``handlers.skill``'s own accessors — the same ones
    ``SkillHandler.get`` uses — rather than a store round trip.

    ``toc``/``none`` render a one-line bookmark, ``summary``…``fisheye``
    the verbatim body (capped like any other eye): a skill has no corpus
    position to be a spatial neighbour of. Its *link* ring is derived,
    not stored — the ``[[slug]]`` wikilinks, outbound plus inbound, from
    the shared :class:`SkillGraph` (gr572706). ``fisheye+1hop`` appends
    it; ``fisheye+2hop`` adds the second hop as counts. ``q='<label>'``
    expands one first-hop group, ``q='skill:<label>'`` one second-hop
    group — the same grammar a link-kind eye takes. Wikilinks in the
    body render expanded like the whole-skill serve."""
    from precis.handlers.skill import _expand_wikilinks, _load_skill, _skill_title

    slug = handle[len(_SKILL_HANDLE_PREFIX) :]
    text = _load_skill(slug)
    if text is None:
        raise ValueError(f"eye: no live skill for {handle!r}")
    title = _skill_title(slug) or slug
    head = f"{handle} [skill] {title}".rstrip()
    if ext <= Extent.TOC:
        return f"· {head}"
    block = f"{head}\n{_cap(_expand_wikilinks(text), _VERBATIM_CAP)}"
    if ext < Extent.HOP1:
        return block

    # ring= narrows like on any eye: a kinds filter without 'skill'
    # empties the ring; a space or tags filter always does — a file
    # carries no tags.
    if ring is not None and (
        (ring.kinds is not None and "skill" not in ring.kinds)
        or ring.spaces is not None
        or ring.tags is not None
    ):
        groups: dict[str, tuple[str, ...]] = {}
    else:
        groups = {
            label: slugs for label, slugs in _skill_link_groups(slug).items() if slugs
        }

    sections: list[str] = []
    first_hop_label = expand if expand is not None and ":" not in expand else None
    if first_hop_label is not None:
        sections.append(_skill_expand_first_hop(first_hop_label, groups))
    elif expand is None:
        first = _skill_first_hop(slug, groups)
        sections.append(
            first or "no linked skills — no [[slug]] edges in either direction"
        )
    if ext >= Extent.HOP2 and first_hop_label is None:
        second = _skill_second_hop(slug, groups)
        if expand is not None:
            sections.append(_skill_expand_second_hop(expand, second))
        elif second:
            sections.append(
                _skill_render_second_hop(second, sum(len(s) for s in groups.values()))
            )
    return "\n\n".join([block, *sections])


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
    sz = size_cell(sum(len((b.text or "").encode("utf-8")) for b in bucket))
    row = f"  · {lead}{span}  {_cap(label, _CHUNK_SUMMARY_CAP)}"
    return f"{row} · {sz}" if sz else row


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
                body = _cap(b.text, _VERBATIM_CAP)
                total = len((b.text or "").strip())
                if total > _VERBATIM_CAP:
                    # The center is the orientation read, capped like every
                    # eye body — name the call that returns it whole, or a
                    # long block's head passes for the block (the marker the
                    # note eye already prints).
                    body += "\n" + _cut_marker(
                        kind, _chunk_handle(kind, b), _VERBATIM_CAP, total
                    )
                lines.append(f"▸ {h} [{b.chunk_kind}]\n{body}")
        else:
            sz = size_cell(len((b.text or "").encode("utf-8")))
            row = f"  · {h}  {_chunk_summary(b)}"
            lines.append(f"{row} · {sz}" if sz else row)
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
    sections = _rings(
        store, ref_id, kind, ext, recall=recall, expand=expand, ring=ring, handle=handle
    )
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


#: Kinds whose ``get`` has a bounded ``view='body'`` — the call the cut
#: marker names for them instead of the bare get, which is no promised
#: route to the whole body (a gripe card clips the body outright; a todo
#: card's comments+links can push it over the fitter's cap — gr554136).
_BODY_VIEW_KINDS: frozenset[str] = frozenset({"gripe", "todo"})


def _cut_marker(kind: str, handle: str, shown: int, total: int) -> str:
    """The line closing a body the eye cut at ``_VERBATIM_CAP``: the call that
    returns it whole, and for a memory the way to stop it recurring."""
    call = (
        f"get(kind={kind!r}, id={handle!r}, view='body')"
        if kind in _BODY_VIEW_KINDS
        else f"get(kind={kind!r}, id={handle!r})"
    )
    line = f"… cut: {shown} of {total} chars — full body: {call}"
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


def _todo_state(store: Store, ref: Any) -> str:
    """``status: <STATUS> · prio: <N>`` for a todo's focal card. Prio is the
    effective sort value: a NULL column rides as the default 5 — the same
    ``COALESCE(prio, 5)`` every todo sort applies — so the line always names
    a number (gr568413), rendered with its ladder name when it has one
    (``5 (normal)``; ``handlers._prio_tag.prio_display``). Neither field
    rides the chip row: ``STATUS:open`` drops there as context-restating
    (the default), and prio is a sort column, never a stored tag —
    ``PRIO:`` spellings split into the column at write time
    (``handlers._prio_tag``)."""
    from precis.handlers._prio_tag import prio_display
    from precis.todo import views

    ref_id = int(ref.id)
    status = views._status_of_many(store, [ref_id]).get(ref_id, "open")
    prio = getattr(ref, "prio", None)
    return f"status: {status} · prio: {prio_display(prio if prio is not None else 5)}"


#: Open memory gripes the focal memory's ``under review:`` line names.
_UNDER_REVIEW_CAP = 3


def _under_review(store: Store, ref_id: int) -> str:
    """``under review: gr12 <title> · gr9 <title>`` — the open gripes that
    raise a concern about this memory (gr657257), so a reader sees the
    gardener has a complaint queued against it. Empty when there are none."""
    from precis.utils.memory_hubs import open_memory_gripes

    gripes = open_memory_gripes(store, [ref_id]).get(ref_id, [])
    if not gripes:
        return ""
    shown = [
        f"gr{gid} {_cap(' '.join(title.split()), 80)}".rstrip()
        for gid, title in gripes[:_UNDER_REVIEW_CAP]
    ]
    more = len(gripes) - len(shown)
    tail = f" · +{more} more" if more > 0 else ""
    return f"under review: {' · '.join(shown)}{tail}"


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
    body *is* the note and the links are its point. A todo's focal card also
    carries its workflow state line (:func:`_todo_state`) — STATUS and prio
    are the fields a task handoff reads first, and neither rides the chip
    row. A quest log chunk focuses its entry text, not the quest's gist;
    its rings still belong to the owning quest."""
    ref = _resolve_ref(store, handle)
    if ref is None or getattr(ref, "retired_at", None) is not None:
        raise ValueError(f"eye: no live {kind} ref for {handle!r}")
    if kind == "conv":
        # The ladder is a conv read like any other (gr555998 follow-up):
        # every conv handle — record ``co<N>`` or chunk ``cc<N>`` —
        # funnels through the note eye, so the bound read scope gates
        # the focus once here, for every rung and every door (handler
        # ``eye``, the ``/eye`` route, the working-set composer). The
        # ``Denied`` propagates beside the eye's ``ValueError``s.
        from precis.handlers.conversation import deny_out_of_scope_conv

        deny_out_of_scope_conv(ref)
    chips = _head_chips(store, ref, kind)
    if ext <= Extent.TOC and not recall:
        return f"· {_head(ref, kind, chips)}"
    cap = _SUMMARY_CAP if ext <= Extent.SUMMARY else _VERBATIM_CAP
    parsed = handle_registry.parse(handle)
    if kind == "quest" and parsed is not None and parsed[1]:
        text = store.chunks.chunk_text_by_id(parsed[2]) or ""
        body, total = _cap(text, cap), len(text)
    else:
        body, total = _note_body(store, ref, kind, cap=cap)
    if cap == _VERBATIM_CAP and total > _VERBATIM_CAP:
        marker = _cut_marker(
            kind, handle_registry.format_handle(kind, int(ref.id)), _VERBATIM_CAP, total
        )
        body = f"{body}\n{marker}"
    lines = [_head(ref, kind, chips)]
    if kind == "todo":
        lines.append(_todo_state(store, ref))
    if kind == "memory" and (review := _under_review(store, int(ref.id))):
        lines.append(review)
    if body:
        lines.append(body)
    block = "\n".join(lines)
    sections = _rings(
        store,
        int(ref.id),
        kind,
        ext,
        recall=recall,
        expand=expand,
        ring=ring,
        body=body if kind == "memory" else "",
        handle=handle,
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
    ``edges`` counts every collapsed edge, ref-level ones included.
    ``suspect`` marks a row whose folded edges include one whose stored
    ``asserted_fingerprint`` no longer matches the target's sha
    (:func:`_suspect_edge_keys`, gr568170)."""

    ref_id: int
    edges: int = 1
    passages: tuple[tuple[int, int | None], ...] = ()
    log_entry: tuple[int, str] | None = None
    suspect: bool = False
    #: Distinct non-empty ``meta.reason`` values across the folded edges,
    #: first-seen order (gr626896 — an edge's why displays where it lists).
    reasons: tuple[str, ...] = ()


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
    states: dict[str, str] | None = None,
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
    _entity_row_chips(store, refs, chips, states)
    # Row sizes from one grouped read on the ref_body_bytes side table
    # (migration 0266) — a ref with no row yet renders no size; never a
    # body_byte_counts fallback here, a ring isn't page-bounded.
    sizes = {
        rid: size_cell(n)
        for rid, n in store.chunks.stored_body_byte_counts(
            [n.ref_id for ns in hop1.values() for n in ns if n.ref_id in refs]
        ).items()
    }
    return _render_first_hop(
        hop1,
        refs,
        chips=chips,
        sizes=sizes,
        hub_members=notes,
        more_hint=_more_hint_factory(store, ref_id, hop1),
        hub_handle=handle_registry.format_handle("memory", ref_id)
        if notes is not None
        else None,
    )


def _entity_handles_for_refs(refs: dict[int, Any]) -> dict[int, str]:
    """Record handles whose identity this surface is allowed to show."""
    return {
        rid: handle
        for rid, ref in refs.items()
        if not _scope_masks(ref)
        and (handle := handle_registry.try_format(str(ref.kind), rid)) is not None
    }


def _entity_row_chips(
    store: Store,
    refs: dict[int, Any],
    chips: dict[int, str],
    states: dict[str, str] | None,
) -> None:
    """Add current state to ring rows; skill/markdown titles are already labels."""
    handles = _entity_handles_for_refs(refs)
    if states is None:
        states = safe_entity_states(store, handles.values())
    for rid, handle in handles.items():
        state = states.get(handle, "")
        if not state or state == " ".join(str(refs[rid].title or "").split()):
            continue
        old = chips.get(rid, "")
        if state not in old:
            chips[rid] = f"{old} · {state}" if old else state


def _row_chips(
    store: Store, refs: Any, *, ring: RingFilter | None = None
) -> dict[int, str]:
    """``{ref id: chips}`` for a page of neighbour rows — one
    ``ref_tags_bulk`` read (:func:`precis.utils.tag_chips.chips_bulk`),
    the ring's ``SPACE:`` values and ``tags`` entries dropped as
    context-restating."""
    from precis.utils import tag_chips

    return tag_chips.chips_bulk(
        store,
        {int(i): str(getattr(r, "kind", "") or "") for i, r in refs.items()},
        ring_spaces=ring.spaces if ring is not None else None,
        ring_tags=ring.tags if ring is not None else None,
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
    masked = {o for o in shown if _scope_masks(refs[o])}
    firsts = _first_body_lines(
        store,
        [
            o
            for o in shown
            if o not in masked
            and not (getattr(refs[o], "meta", None) or {}).get("hook")
        ],
    )
    notes: dict[int, str] = {}
    for oid in shown:
        if oid in masked:
            # A masked member carries no hook — its body first line
            # would be an excerpt leak.
            notes[oid] = ""
            continue
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


def _tag_sets(store: Store, ref_ids: Any) -> dict[int, frozenset[str]]:
    """``{ref id: its live tags as canonical strings}`` —
    ``SPACE:scratch``, ``scratch-for:td575121``, ``pinned`` — for many
    refs in one ``ref_tags_bulk`` read; a ref carrying none maps to
    ``frozenset()``. Both ring tag dimensions read this: ``space``
    selects on the ``SPACE:``-prefixed entries (the partition), ``tags``
    requires every listed tag in the set."""
    ids = {int(i) for i in ref_ids}
    if not ids:
        return {}
    tagged = store.ref_tags_bulk(list(ids))
    return {
        rid: frozenset(v if ns in ("OPEN", "FLAG") else f"{ns}:{v}" for ns, v in pairs)
        for rid, pairs in tagged.items()
    }


def _ring_kept(
    store: Store,
    ring: RingFilter,
    refs: dict[int, Any],
    pairs: list[tuple[int, int | None, Any]],
) -> list[tuple[int, int | None, Any]]:
    """``pairs`` (``(neighbour ref, far chunk id, meta.reason)`` per edge)
    narrowed by ``ring``: kind from the ref row, tags in one bulk read
    (only when the filter uses a tag dimension)."""
    out = pairs
    if ring.kinds is not None:
        out = [p for p in out if str(getattr(refs[p[0]], "kind", "")) in ring.kinds]
    if ring.spaces is not None or ring.tags is not None:
        tag_of = _tag_sets(store, {o for o, _c, _w in out})
        if ring.spaces is not None:
            want = {f"SPACE:{v}" for v in ring.spaces}
            out = [p for p in out if tag_of.get(p[0], frozenset()) & want]
        if ring.tags is not None:
            out = [p for p in out if ring.tags <= tag_of.get(p[0], frozenset())]
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


def _suspect_edge_keys(store: Store, ref_id: int) -> set[tuple[int, int | None]]:
    """``{(other ref, other chunk)}`` for the focus's *suspect* edges —
    ones carrying an ``asserted_fingerprint`` (stamped at link write for
    memory-sourced edges, gr568170) that no longer equals the target's
    current ``precis_target_sha``. Derived lazily here, so a target edit
    never propagates a write back onto its incident edges. The far end
    from this side is the dst of an outbound edge, the src of an inbound
    one — an inbound edge's fingerprint asserts about the focus itself,
    so it shows suspect here too. A read failure reads as no suspects,
    never a broken render."""
    try:
        with store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT src_ref_id, src_chunk_id, dst_ref_id, dst_chunk_id "
                "FROM links "
                "WHERE (src_ref_id = %s OR dst_ref_id = %s) "
                "  AND meta ? 'asserted_fingerprint' "
                "  AND meta->>'asserted_fingerprint' IS DISTINCT FROM "
                "      precis_target_sha("
                "        CASE WHEN dst_chunk_id IS NULL THEN 'ref' "
                "             ELSE 'chunk' END, "
                "        coalesce(dst_chunk_id, dst_ref_id))",
                (ref_id, ref_id),
            ).fetchall()
    except Exception:
        log.debug("suspect-edge read failed", exc_info=True)
        return set()
    out: set[tuple[int, int | None]] = set()
    for src, sck, dst, dck in rows:
        if int(src) == ref_id:
            out.add((int(dst), int(dck) if dck is not None else None))
        else:
            out.add((int(src), int(sck) if sck is not None else None))
    return out


def _collapse_pairs(
    pairs: list[tuple[int, int | None, Any]],
    ords: dict[int, int],
    suspect: set[tuple[int, int | None]]
    | frozenset[tuple[int, int | None]] = frozenset(),
) -> list[_RingNeighbour]:
    """``(ref, far chunk, meta.reason)`` per edge → one
    :class:`_RingNeighbour` per ref — first-seen order, every edge
    counted, far-end chunks deduped (gr524665: a claim hub's
    ``paper --corroborates--> hub`` fan-in grounds at N chunks
    of one source and must render as one row, not N identical rows). A row
    is ``suspect`` when any folded edge's ``(ref, chunk)`` is in
    ``suspect``; its ``reasons`` are the folded edges' distinct non-empty
    ``meta.reason`` values, first-seen order (gr626896)."""
    acc: dict[int, tuple[int, list[int], bool, list[str]]] = {}
    order: list[int] = []
    for oid, ck, why in pairs:
        slot = acc.get(oid)
        if slot is None:
            acc[oid] = slot = (0, [], False, [])
            order.append(oid)
        edges, chunks, sus, reasons = slot
        acc[oid] = (edges + 1, chunks, sus or (oid, ck) in suspect, reasons)
        if ck is not None and ck not in chunks:
            chunks.append(ck)
        if isinstance(why, str) and why.strip() and why not in reasons:
            reasons.append(why)
    return [
        _RingNeighbour(
            oid,
            edges=acc[oid][0],
            passages=tuple((c, ords.get(c)) for c in acc[oid][1]),
            suspect=acc[oid][2],
            reasons=tuple(acc[oid][3]),
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
        {o for pairs in by_label.values() for o, _c, _why in pairs}
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
        store,
        (c for pairs in live.values() for _o, c, _why in pairs if c is not None),
    )
    suspect = _suspect_edge_keys(store, ref_id) if live else set()
    entries = _quest_log_entries(store, ref_id, live, refs)
    out: dict[tuple[str, str], list[_RingNeighbour]] = {}
    for key, pairs in live.items():
        ordinary = []
        seen: set[int] = set()
        for oid, cid, why in pairs:
            entry = (
                entries.get(cid) if cid is not None and key[1] == "cited-by" else None
            )
            if entry is None:
                ordinary.append((oid, cid, why))
                continue
            if cid in seen:
                continue
            assert cid is not None
            seen.add(cid)
            ord_, section, preview = entry
            label = f"quest log — {section}" if section else "quest log"
            out.setdefault((key[0], label), []).append(
                _RingNeighbour(
                    oid,
                    passages=((cid, ord_),),
                    log_entry=(ord_, preview),
                    reasons=(why,) if isinstance(why, str) and why.strip() else (),
                )
            )
        if ordinary:
            out.setdefault(key, []).extend(_collapse_pairs(ordinary, ords, suspect))
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


def _quest_log_entries(
    store: Store,
    ref_id: int,
    pairs: dict[tuple[str, str], list[tuple[int, int | None, Any]]],
    refs: dict[int, Any],
) -> dict[int, tuple[int, str, str]]:
    """A structure's incoming log entries, keyed by the linked chunk.

    Keep the stored section (or entry type) verbatim; free prose never
    supplies a category. Read only linked chunks, after ring and scope
    filtering, and use the ordinary ring if this optional read fails.
    """
    ids = {
        cid
        for (_group, label), neighbours in pairs.items()
        if label == "cited-by"
        for oid, cid, _why in neighbours
        if cid is not None
        and str(getattr(refs[oid], "kind", "")) == "quest"
        and not _scope_masks(refs[oid])
    }
    if not ids:
        return {}
    focus = store.fetch_refs_by_ids([ref_id]).get(ref_id)
    if focus is None or focus.kind != "structure":
        return {}
    try:
        with store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT chunk_id, ord, meta, text FROM chunks "
                "WHERE chunk_id = ANY(%s) AND chunk_kind = 'quest_log'",
                (list(ids),),
            ).fetchall()
    except Exception:
        log.debug("quest log ring read failed", exc_info=True)
        return {}
    out = {}
    for cid, ord_, meta, text in rows:
        meta = meta or {}
        section = next(
            (
                value.strip()
                for key in ("section", "entry_type")
                if isinstance(value := meta.get(key), str) and value.strip()
            ),
            "",
        )
        out[int(cid)] = (
            int(ord_),
            section,
            _cap(_preview_text(text), _CHUNK_SUMMARY_CAP),
        )
    return out


#: ``cap`` for the ring's :meth:`Store.neighbourhood` read: past any real
#: ref's link count (the ring caps per label at render time), inside int4.
_NBH_UNCAPPED = 1_000_000


def _neighbourhood_edges(
    store: Store, ref_id: int
) -> list[tuple[int, int, Any, int | None, int | None, Any]]:
    """``ref_id``'s live first-hop edges as stored ``(src, dst, relation,
    src_chunk_id, dst_chunk_id, reason)``, oldest link first, read through
    :meth:`Store.neighbourhood` with ``link_ids=True`` so a chunk-grounded
    edge keeps the chunk it grounds at (gr524665) and its ``link_id``
    resolves the edge's ``meta.reason`` (gr626896) in one bulk read.

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
    reason_of = _edge_reasons_by_id(
        store,
        [int(e["link_id"]) for e in nbh["edges"] if e.get("link_id") is not None],
    )
    inverses, _symmetric = _relation_reading(store)
    edges: list[tuple[int, int, Any, int | None, int | None, Any]] = []
    for e in reversed(nbh["edges"]):
        sck = e.get("src_chunk_id")
        dck = e.get("dst_chunk_id")
        reason = reason_of.get(int(e["link_id"])) if e.get("link_id") else None
        if e["dir"] == "out":
            edges.append((ref_id, int(far_of[e["dst"]]), e["rel"], sck, dck, reason))
        else:
            rel = inverses.get(e["rel"], e["rel"])
            edges.append((int(far_of[e["src"]]), ref_id, rel, sck, dck, reason))
    return edges


def _edge_reasons_by_id(store: Store, link_ids: list[int]) -> dict[int, Any]:
    """``{link_id: meta['reason']}`` for the listed edges in one query —
    the value the ring appends to the neighbour row. A read failure
    reads as no reasons, never a broken render."""
    if not link_ids:
        return {}
    try:
        with store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT link_id, meta->>'reason' FROM links "
                "WHERE link_id = ANY(%s) AND meta ? 'reason'",
                (link_ids,),
            ).fetchall()
    except Exception:
        log.debug("link reason read failed", exc_info=True)
        return {}
    return {int(r[0]): r[1] for r in rows}


def _live(refs: dict[int, Any], oid: int) -> bool:
    r = refs.get(oid)
    return r is not None and getattr(r, "retired_at", None) is None


def _bucket_edges(
    store: Store,
    ref_id: int,
    edges: list[tuple[int, int, Any, int | None, int | None, Any]],
) -> dict[tuple[str, str], list[tuple[int, int | None, Any]]]:
    """Group ``ref_id``'s ``(src, dst, relation, src_chunk, dst_chunk,
    reason)`` edges by ``(ring group, label as seen from here)``, listing
    the other endpoint of each as ``(ref, far chunk id, meta.reason)`` —
    the chunk the edge grounds at on the neighbour's side, and the why the
    row appends (``None`` when the edge carries none). Dead ends included;
    the caller filters by liveness and collapses."""
    inverses, symmetric = _relation_reading(store)
    by_label: dict[tuple[str, str], list[tuple[int, int | None, Any]]] = {}
    for src, dst, rel, sck, dck, reason in edges:
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
            (other, int(other_chunk) if other_chunk is not None else None, reason)
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
    edges: dict[int, list[tuple[int, int, Any, Any, Any, Any]]] = {
        r: [] for r in ref_ids
    }
    for src, dst, rel, sck, dck in rows:
        for end in {int(src), int(dst)} & wanted:
            edges[end].append((int(src), int(dst), rel, sck, dck, None))
    buckets = {r: _bucket_edges(store, r, e) for r, e in edges.items()}
    others = {
        o for b in buckets.values() for pairs in b.values() for o, _c, _why in pairs
    }
    refs = store.fetch_refs_by_ids(others | {int(src) for src, _d, _r, _s, _k in rows})
    shape: dict[int, tuple[int, int, int]] = {}
    for r, by_label in buckets.items():
        # inbound edges from a retired source are unremovable from this end
        # (the source is Gone), so they are not this ref's dead links
        removable = {dst for src, dst, _rel, _s, _k, _w in edges[r] if src == r}
        orphaned = {
            src
            for src, dst, _rel, _s, _k, _w in edges[r]
            if dst == r and src not in removable and not _live(refs, src)
        }
        # an outbound ``supersedes`` edge to a retired ref is the merge record
        # precis-memory-help prescribes (link supersedes, then delete the old
        # node), not a dead end to repoint
        tombstones = {
            dst
            for src, dst, rel, _s, _k, _w in edges[r]
            if src == r and rel == "supersedes" and not _live(refs, dst)
        }
        live_groups = [
            {o for o, _c, _why in pairs if _live(refs, o)}
            for (_g, label), pairs in by_label.items()
            if not (r in hubs and label == _HUB_MEMBER_LABEL)
        ]
        live_all = [
            {o for o, _c, _why in pairs if _live(refs, o)}
            for pairs in by_label.values()
        ]
        every = {o for pairs in by_label.values() for o, _c, _why in pairs}
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
    states: dict[str, str] | None = None,
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
    _entity_row_chips(store, refs, chips, states)
    sizes = {
        rid: size_cell(n)
        for rid, n in store.chunks.stored_body_byte_counts(
            [n.ref_id for n in ns if n.ref_id in refs]
        ).items()
    }
    lines = [f"— linked (1 hop) · {label} ({len(ns)}) —"]
    lines.extend(
        f"    {_ring_row(refs[n.ref_id], n, chips=chips.get(n.ref_id, ''), size=sizes.get(n.ref_id, ''))}"
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


def _ring_row(ref: Any, n: _RingNeighbour, *, chips: str = "", size: str = "") -> str:
    """One ring row: the neighbour label plus its collapsed-edge tail."""
    from precis.utils.surface_scope import masked_ref_placeholder

    placeholder = masked_ref_placeholder(ref)
    if placeholder is not None:
        # The placeholder carries no passage handles either — a masked
        # neighbour leaks nothing but its existence as a neighbour.
        return placeholder
    if n.log_entry is not None:
        ord_, preview = n.log_entry
        handle = handle_registry.format_handle("quest", n.passages[0][0], chunk=True)
        return f"{handle} ~{ord_} — {preview}"
    row = _neighbor_label(ref, n.ref_id, chips=chips, size=size) + _passage_suffix(
        n, str(getattr(ref, "kind", "") or "")
    )
    # gr568170: a folded edge whose target moved since it was asserted.
    if n.suspect:
        row += "  · suspect"
    # gr626896: the folded edges' why — ``meta.reason='.'``
    # (reviewed-obvious) prints as a bare period.
    return row + link_reason_suffix(" · ".join(n.reasons))


def _render_first_hop(
    by_label: dict[tuple[str, str], list[_RingNeighbour]],
    refs: dict[int, Any],
    chips: dict[int, str] | None = None,
    sizes: dict[int, str] | None = None,
    hub_members: dict[int, str] | None = None,
    more_hint: Any = None,
    hub_handle: str | None = None,
) -> str:
    chips = chips or {}
    sizes = sizes or {}
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
            by_id = {n.ref_id: n for n in live_ids}
            listed = [o for o in hub_members if o in live]
            if len(live_ids) == 1:
                oid = live_ids[0].ref_id
                hook = hub_members.get(oid, "")
                row = _neighbor_label(
                    refs[oid], oid, chips=chips.get(oid, ""), size=sizes.get(oid, "")
                )
                if by_id[oid].suspect:
                    row += "  · suspect"
                row += link_reason_suffix(" · ".join(by_id[oid].reasons))
                lines.append(
                    f"  {label}: {row} — {hook}" if hook else f"  {label}: {row}"
                )
            else:
                lines.append(f"  {label} ({len(live_ids)}):")
                for oid in listed:
                    hook = hub_members.get(oid, "")
                    row = _neighbor_label(
                        refs[oid],
                        oid,
                        chips=chips.get(oid, ""),
                        size=sizes.get(oid, ""),
                    )
                    if by_id[oid].suspect:
                        row += "  · suspect"
                    row += link_reason_suffix(" · ".join(by_id[oid].reasons))
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
                f"{_ring_row(refs[n.ref_id], n, chips=chips.get(n.ref_id, ''), size=sizes.get(n.ref_id, ''))}"
            )
            continue
        lines.append(f"  {label} ({len(live_ids)}):")
        for n in live_ids[:_NEIGHBOR_GROUP_CAP]:
            lines.append(
                f"    {_ring_row(refs[n.ref_id], n, chips=chips.get(n.ref_id, ''), size=sizes.get(n.ref_id, ''))}"
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


def _neighbor_label(ref: Any, ref_id: int, *, chips: str = "", size: str = "") -> str:
    from precis.utils.surface_scope import masked_ref_placeholder

    placeholder = masked_ref_placeholder(ref)
    if placeholder is not None:
        return placeholder
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
    if size:
        label = f"{label} · {size}"
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
    body_handles: list[str] | None = None,
    states: dict[str, str] | None = None,
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
    group, but ``more()`` only ever paged an over-long body (removed with
    the transport pager) and had no notion of a named group, so the
    expansion is the same call with a filter — decided 2026-10-02.
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
    if (
        ring is not None
        and (ring.spaces is not None or ring.tags is not None)
        and groups
    ):
        tag_of = _tag_sets(store, {o for ids in groups.values() for o in ids})
        if ring.spaces is not None:
            want = {f"SPACE:{v}" for v in ring.spaces}
            groups = {
                key: kept
                for key, ids in groups.items()
                if (kept := {o for o in ids if tag_of.get(o, frozenset()) & want})
            }
        if ring.tags is not None:
            groups = {
                key: kept
                for key, ids in groups.items()
                if (kept := {o for o in ids if ring.tags <= tag_of.get(o, frozenset())})
            }
    if not groups:
        if expand is not None:
            raise ValueError(
                f"eye: no second-hop group {expand!r}; the second hop is empty"
            )
        return ""
    floor = "≥" if len(rows) >= _SECOND_HOP_ROW_CAP else ""
    if expand is not None:
        return _expand_second_hop(
            store, groups, expand, ring=ring, body_handles=body_handles, states=states
        )
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
    body_handles: list[str] | None = None,
    states: dict[str, str] | None = None,
) -> str:
    kind, _, label = expand.partition(":")
    ids = groups.get((kind.strip(), label.strip()))
    if not ids:
        known = ", ".join(f"{k}:{lab}" for k, lab in sorted(groups))
        raise ValueError(f"eye: no second-hop group {expand!r}; groups here: {known}")
    shown = sorted(ids)[:_SECOND_HOP_EXPAND_CAP]
    refs = store.fetch_refs_by_ids(shown)
    chips = _row_chips(store, refs, ring=ring)
    # In an expanded second hop these are the only neighbour rows printed.
    # Resolve them together with the focal body's handles and return the same
    # snapshot to _rings for its body courtesy lines.
    if states is None:
        states = {}
    states.update(
        safe_entity_states(
            store,
            [*(body_handles or []), *_entity_handles_for_refs(refs).values()],
        )
    )
    _entity_row_chips(store, refs, chips, states)
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


def _linked_ref_ids(store: Store, ref_id: int) -> set[int]:
    """Every ref a live edge joins to ``ref_id`` — either direction, any
    relation (gr555999). ``+recall`` skips them: they are already
    structural neighbours the link rungs render, so a semantic hit on one
    would restate the edge instead of surfacing an unlinked near-match."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT src_ref_id, dst_ref_id FROM links "
            "WHERE src_ref_id = %s OR dst_ref_id = %s",
            (ref_id, ref_id),
        ).fetchall()
    linked = {int(r) for row in rows for r in row}
    linked.discard(ref_id)
    return linked


def _recall_promote(ref_id: int, kind: str, hits: list[Any]) -> str:
    """The ``+recall`` rung's promote affordance (gr555999): the rung lists
    only unlinked neighbours, so every hit carries one ready-to-run
    ``link(...)`` — ``rel='related-to'`` by default; a sharper rel
    (``derived-from``, ``entails``, ``qualifies``, …) is better when one
    applies. The agent decides and calls ``link`` itself — nothing here
    writes an edge."""
    from precis.utils.next_block import render_next_section

    focus = handle_registry.try_format(kind, ref_id) or str(ref_id)
    calls: list[tuple[str, str]] = []
    for ref in hits:
        rid = int(ref.id)
        hkind = str(getattr(ref, "kind", "") or kind)
        handle = handle_registry.try_format(hkind, rid)
        ident = handle or str(getattr(ref, "slug", None) or rid)
        calls.append(
            (
                f"link(kind={kind!r}, id='{focus}', "
                f"target='{hkind}:{ident}', rel='related-to')",
                f"promote {handle or ident} to a real edge",
            )
        )
    calls.append(
        (
            "get(kind='skill', id='precis-relations')",
            "a sharper rel than 'related-to' when one applies "
            "(derived-from, entails, qualifies, …)",
        )
    )
    return render_next_section(calls)


def _recall(store: Store, ref_id: int, kind: str, *, ring: RingFilter | None) -> str:
    """The ``+recall`` rung: the k nearest chunks by embedding, same kind +
    ``finding``, each with its gist line. Similarity, not edges — the ring
    above says what is linked; this says what is *about* the same thing and
    was never linked (``refeye``'s module docstring draws the same line),
    so refs already linked to the focus are excluded in the query and each
    hit carries a promote-to-``link`` call (:func:`_recall_promote`,
    gr555999).
    A ring filter narrows this rung too: kinds intersect the same-kind +
    ``finding`` leg, ``spaces`` override the focus's own partition, and
    ``tags`` AND onto the tag clause — ``scratch-for:td<N>`` scopes the
    hits to one task's scratch.
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
    want_tags = [
        *space,
        *(sorted(ring.tags) if ring is not None and ring.tags is not None else []),
    ]
    hits = store.chunks.search_chunks_semantic(
        query_vec=vec,
        kinds=kinds,
        tags=want_tags or None,
        limit=_RECALL_K * 3,
        max_distance=RECALL_MAX_DISTANCE,
        # Refs the focus already links to or from (any rel) are structural
        # neighbours the ring rungs show — exclude them in the query so the
        # cap refills from the next-ranked unlinked candidates (gr555999).
        exclude_ref_ids=[ref_id, *_linked_ref_ids(store, ref_id)],
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
    kept: list[Any] = []
    lines = [f"— recall (nearest by embedding, {'+'.join(kinds)}, k≤{_RECALL_K}) —"]
    for block, ref, dist in hits:
        rid = int(ref.id)
        if rid in seen:
            continue  # one line per ref: its nearest chunk speaks for it
        if _scope_masks(ref):
            # A masked hit prints neither label nor gist — an
            # out-of-scope conv's chunk excerpt would carry the
            # conversation's text.
            continue
        seen.add(rid)
        kept.append(ref)
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
    return "\n".join(lines) + _recall_promote(ref_id, kind, kept)


def _rings(
    store: Store,
    ref_id: int,
    kind: str,
    ext: Extent,
    *,
    recall: bool,
    expand: str | None,
    ring: RingFilter | None,
    body: str = "",
    handle: str | None = None,
) -> list[str]:
    """Every neighbourhood section a non-tree eye appends at ``ext``:
    the first hop at ``fisheye+1hop`` and up, the second hop at
    ``fisheye+2hop``, recall when asked. ``ring`` narrows all three.
    An empty requested hop names the explicit recall call; never run the
    extra search without the caller's ``+recall`` opt-in. Todo trees use
    ``refs.parent_id`` rather than links, but still fill the first hop —
    and the ``— tree —`` ring renders even when empty, as an explicit
    lone-leaf line (gr568413).
    """
    sections: list[str] = []
    tree = ""
    if kind == "todo" and ext >= Extent.HOP1:
        from precis.todo.work_view import render_tree_ring

        tree = render_tree_ring(store, ref_id)
    hop1, refs = (
        _first_hop(store, ref_id, ring=ring) if ext >= Extent.HOP1 else ({}, {})
    )
    handles = entity_handles(body)
    neighbour_handles = _entity_handles_for_refs(refs).values()
    # Expansion renders only second-hop rows, discovered by _second_hop.
    # Defer this one batch until those ids are available.
    defer_states = (
        ext >= Extent.HOP2 and bool(hop1) and expand is not None and ":" in expand
    )
    states = (
        {}
        if defer_states
        else safe_entity_states(store, [*handles, *neighbour_handles])
    )
    if ext >= Extent.HOP1:
        first_hop_label = expand if expand is not None and ":" not in expand else None
        if first_hop_label is not None:
            sections.append(
                _expand_first_hop(
                    store, ref_id, hop1, refs, first_hop_label, ring=ring, states=states
                )
            )
        elif expand is None:
            sections.append(
                _render_ring(store, ref_id, hop1, refs, ring=ring, states=states)
            )
        if ext >= Extent.HOP2 and first_hop_label is None:
            sections.append(
                _second_hop(
                    store,
                    ref_id,
                    hop1,
                    expand=expand,
                    ring=ring,
                    body_handles=handles,
                    states=states,
                )
            )
        if expand is None and not sections[-1] and (ext >= Extent.HOP2 or not tree):
            hops = 2 if ext >= Extent.HOP2 else 1
            message = (
                f"no structural neighbours at {hops} hop{'s' if hops == 2 else ''}"
            )
            from precis.bench import switches as bench_switches

            # With the recall layer switched off the "wider" call would
            # advertise a rung the server will not run.
            if not recall and bench_switches.current().recall:
                focus = handle or handle_registry.format_handle(kind, ref_id)
                filters: dict[str, list[str]] = {}
                if ring is not None:
                    if ring.kinds is not None:
                        filters["kinds"] = sorted(ring.kinds)
                    if ring.spaces is not None:
                        filters["space"] = sorted(ring.spaces)
                    if ring.tags is not None:
                        filters["tags"] = sorted(ring.tags)
                args = f", args={{'ring': {filters!r}}}" if filters else ""
                message += (
                    f"; wider: get(kind={kind!r}, id={focus!r}, "
                    f"extent='fisheye+{hops}hop+recall'{args})"
                )
            sections[-1] = message
    courtesy = courtesy_lines(handles, states, cap=_NEIGHBOR_GROUP_CAP)
    if courtesy:
        sections.insert(0, courtesy)
    if recall:
        sections.append(_recall(store, ref_id, kind, ring=ring))
    if kind == "todo" and ext >= Extent.HOP1:
        # The ``— tree —`` ring is promised on every todo eye at ≥1hop
        # (gr568413): a lone leaf gets an explicit empty-ring line, so a
        # missing ring can't be mistaken for an unimplemented one.
        sections.append(tree or "— tree —\nlone leaf — no parent, peers or children")
    return [s for s in sections if s]
