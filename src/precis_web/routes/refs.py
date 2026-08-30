"""Refs tab(s) — browse durable ref kinds.

One generic list/detail surface served per kind via ``/refs/{kind}``.
Each browsable kind gets its own top-nav tab (see ``base.html.j2``),
so the nav doubles as the kind selector — there is exactly one route
module and one pair of templates behind every tab.

* List reads off the DB: ``search_refs_lexical`` when a query is
  present (ranked), else ``list_refs`` with the date / tag filters and
  the whitelisted sort. Pagination is offset-based.
* Detail renders the handler's own ``get`` output read-only (through
  the in-process runtime, so the rendering can't drift from MCP).

Read-only: mutations stay on verb-specific tabs (Tasks) or the Console.
Slug kinds (conv/oracle/patent/pres) and numeric kinds (memory/gripe) are
both URL-addressed by numeric ``ref_id``; detail view resolves the
canonical address (slug when present, else id) for the ``get`` call.

Pathway explorer (``pathway_detail.html.j2``): a client-side energy
diagram off ``meta.graph`` (:func:`_pathway_graph_payload`, one profile
per root→leaf path). The CHE U-slider re-levers each node client-side
(``rel_energy + n_H·U``; gated on ``has_n_h`` so a legacy graph renders
unchanged). Fork probabilities are never fabricated — a fork is labelled
only when every outgoing chemical edge carries a trusted barrier. The
catpath tier ladder (screening → neb → verify) renders a tier chip +
cross-tier toggle, siblings resolved via ``refines`` links
(:func:`_pathway_tier_sibling`).
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from precis.errors import NotFound
from precis.handlers.structure import format_calc_identity
from precis.structure import Measure, anchor_identity_verified, evaluate_measure
from precis.taproot.seniority import is_claim_hub
from precis.utils import handle_registry, mentions
from precis.utils.authors import author_names
from precis.utils.claude_agent import ClaudeAgentError
from precis_web import ask
from precis_web.deps import (
    await_dispatch,
    get_store,
    get_web_config,
    redirect_or_error,
    templates,
)
from precis_web.item_view import display_title
from precis_web.paper_ident import PAPER_IDENT_KINDS, paper_head
from precis_web.pathway_kinetics import kinetics_payload
from precis_web.routes.structure import _geom_payload

if TYPE_CHECKING:
    from precis.store.protocols import RefsByIdStore
    from precis.store.store import Store

router = APIRouter(prefix="/refs", tags=["refs"])

log = logging.getLogger(__name__)

#: Browsable ref kinds, in nav order: ``(kind, label)``. The nav in
#: ``base.html.j2`` renders one tab per entry; adding a kind here +
#: the nav loop is all it takes to expose another browser.
REF_KINDS: tuple[tuple[str, str], ...] = (
    ("memory", "Memory"),
    ("conv", "Conversations"),
    ("oracle", "Oracle"),
    ("gripe", "Gripes"),
    ("patent", "Patents"),
    ("pres", "Presentations"),
)
_REF_KIND_SET = {k for k, _ in REF_KINDS}
_REF_KIND_LABEL = dict(REF_KINDS)

#: Date-window presets → lookback delta (``None`` = no date filter).
DATE_PRESETS: tuple[tuple[str, str, timedelta | None], ...] = (
    ("any", "Any time", None),
    ("24h", "Last 24h", timedelta(hours=24)),
    ("7d", "Last 7 days", timedelta(days=7)),
    ("30d", "Last 30 days", timedelta(days=30)),
    ("90d", "Last 90 days", timedelta(days=90)),
)
_DATE_DELTA = {key: delta for key, _, delta in DATE_PRESETS}

#: Sort options exposed in the UI → ``Store.list_refs`` order_by keys.
SORT_CHOICES: tuple[tuple[str, str], ...] = (
    ("updated_desc", "Updated (newest)"),
    ("updated_asc", "Updated (oldest)"),
    ("created_desc", "Created (newest)"),
    ("title_asc", "Title A–Z"),
    ("id_desc", "ID (newest)"),
)
_SORT_KEYS = {k for k, _ in SORT_CHOICES}

_PAGE_SIZE = 50


def _require_kind(kind: str) -> None:
    # ``_REF_KIND_SET`` is the old per-kind nav list (memory / conv /
    # oracle / gripe / patent / pres). After T12.6 the detail + list
    # routes serve every kind in ``_REFS_BROWSABLE_KINDS`` (web,
    # youtube, perplexity-research, etc. — anything search lists),
    # so the gate has to use that set or live refs like /refs/youtube/N
    # 400 with "no browse tab" even though their detail page renders
    # fine.
    if kind not in _REFS_BROWSABLE_KINDS:
        raise NotFound(
            f"no browse tab for kind={kind!r}",
            next=f"browsable kinds: {sorted(_REFS_BROWSABLE_KINDS)}",
        )


def _parse_tags(raw: str | None) -> list[str]:
    """Split a comma/space separated tag filter into a clean list."""
    if not raw:
        return []
    parts = [p.strip() for chunk in raw.split(",") for p in chunk.split()]
    return [p for p in parts if p]


def _row(ref: Any) -> dict[str, Any]:
    updated = getattr(ref, "updated_at", None)
    created = getattr(ref, "created_at", None)
    refreshed = getattr(ref, "refreshed_at", None)
    auto_refresh_days = getattr(ref, "auto_refresh_days", None)
    title = ref.title or "(untitled)"
    return {
        "id": ref.id,
        "slug": ref.slug or "",
        "title": title,
        # Single-sourced Drive-wide display cap (item_view.display_title);
        # ``title`` above stays full for the detail-page header.
        "display_title": display_title(title),
        "updated": updated.strftime("%Y-%m-%d %H:%M") if updated else "",
        # Extra meta surfaced on the detail page's header strip. The
        # list templates ignore the keys they don't use, so widening
        # the row here is safe for index / consolidated callers too.
        "created": created.strftime("%Y-%m-%d %H:%M") if created else "",
        "set_by": getattr(ref, "set_by", None) or "",
        "prio": getattr(ref, "prio", None),
        # Relevance-decay window (null auto_refresh_days = permanent).
        # Surfaced together so the operator can see "permanent" vs.
        # "decays over N days since <refreshed>".
        "auto_refresh_days": auto_refresh_days,
        "refreshed": refreshed.strftime("%Y-%m-%d %H:%M") if refreshed else "",
    }


def _fmt_turn_ts(ts: Any) -> str:
    """Best-effort human timestamp for a conv turn's ``meta['ts']``.

    Turns carry ``ts`` as an ISO string (Discord bridge) or a
    datetime; tolerate both and anything else by stringifying. Empty
    when absent.
    """
    if not ts:
        return ""
    if isinstance(ts, datetime):
        return ts.strftime("%Y-%m-%d %H:%M")
    s = str(ts)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).strftime(
            "%Y-%m-%d %H:%M"
        )
    except ValueError:
        return s


#: Author dot colours for the transcript, picked deterministically by
#: author name so the same speaker keeps one colour down a thread.
_AUTHOR_DOTS: tuple[str, ...] = (
    "bg-sky-500",
    "bg-emerald-500",
    "bg-violet-500",
    "bg-amber-500",
    "bg-rose-500",
    "bg-teal-500",
)


def _author_dot(author: str) -> str:
    """Stable colour class for an author (presentation only)."""
    idx = sum(ord(c) for c in author) % len(_AUTHOR_DOTS)
    return _AUTHOR_DOTS[idx]


#: Meta keys rendered as dedicated fields on a turn. Everything else
#: in ``Block.meta`` falls into ``extra_meta`` and is shown as a
#: key/value strip so the operator sees the full per-turn record
#: (stop_reason, token counts, msg_id, source flags, …).
_TURN_SPECIAL_META: frozenset[str] = frozenset({"author", "ts", "chunk_kind"})


def _conv_turns(store: Store, ref_id: int) -> list[dict[str, Any]]:
    """Structured turns for the conversation transcript view.

    Reads body chunks (one per turn) straight off the store so the web
    renders a human-readable chat transcript — the handler's ``get``
    overview is the agent-facing card (with ``Next:`` call
    affordances), which is noise for a person reading a thread.

    Each turn carries ``chunk_kind`` (paragraph / conv_message / …)
    and ``extra_meta`` — every ``meta`` key not consumed by a
    dedicated field. The strip surfaces stop_reason / input_tokens /
    output_tokens / msg_id and any other bridge-stamped fields so a
    reader sees the complete per-turn record without needing to drop
    into the MCP get(view='last-meta').
    """
    turns: list[dict[str, Any]] = []
    for b in store.chunks.list_chunks_for_ref(ref_id):
        meta = getattr(b, "meta", None) or {}
        author = meta.get("author") or "?"
        extra = [
            (k, v)
            for k, v in sorted(meta.items())
            if k not in _TURN_SPECIAL_META and v is not None and v != ""
        ]
        turns.append(
            {
                "pos": b.ord,
                "author": author,
                "dot": _author_dot(author),
                "ts": _fmt_turn_ts(meta.get("ts")),
                "text": b.text or "",
                "chunk_kind": (
                    meta.get("chunk_kind") or getattr(b, "chunk_kind", "") or ""
                ),
                "extra_meta": extra,
            }
        )
    return turns


def _followup_discussions(store: Store, ref_id: int) -> list[dict[str, Any]]:
    """Conv threads spawned from this ref via the "ask a follow-up" box.

    Each follow-up conv is linked ``conv --derived-from--> source``
    (chunk-scoped via ``dst_pos`` when the question was asked on a
    chunk). We surface them on the source's detail page so the
    discussion is reachable from the thought it grew out of.
    """
    try:
        links = store.links_for(ref_id, direction="in", relation="derived-from")
    except Exception:
        return []
    src_ids = [lnk.src_ref_id for lnk in links]
    if not src_ids:
        return []
    refs = store.fetch_refs_by_ids(src_ids, include_deleted=False)
    rows: list[dict[str, Any]] = []
    seen: set[int] = set()
    for lnk in links:
        conv = refs.get(lnk.src_ref_id)
        if conv is None or conv.kind != "conv" or conv.id in seen:
            continue
        seen.add(conv.id)
        rows.append(
            {
                "id": conv.id,
                "title": (conv.title or "(untitled)").split("\n", 1)[0][:120],
                "url": f"/refs/conv/{conv.id}",
                "turns": store.chunks.count_chunks(conv.id),
                "chunk": lnk.dst_ord,
            }
        )
    return rows


def _job_actions(store: Store, ref: Any, tags: list[Any]) -> dict[str, Any]:
    """Context for ``/refs/job/{id}``'s actions strip — retry, transcript,
    parent — mirroring ``/tasks`` dashboard affordances:

    * **retry** — POST ``/tasks/{id}/retry`` clears the parent's
      ``child-failed:`` bubble so ``dispatch`` re-mints. Only
      ``failed``/``cancelled`` retryable (handler-enforced; gated here too
      to avoid a guaranteed error).
    * **model swap** — only when parent todo is an ``LLM:*`` planner (the
      re-minted tick can run on a different tier).
    * **transcript** — link to readable ``stream-json`` turns, if captured.
    * **parent** — the owning todo, one click away.
    """
    status: str | None = None
    for t in tags:
        s = str(t)
        if s.startswith("STATUS:"):
            status = s[len("STATUS:") :]
            break

    # A job hangs off an owner ref via ``refs.parent_id``. Retry
    # only re-dispatches through the *intent* lane (a ``kind='todo'``
    # parent); a compute-lane job owned by a build subject, or a legacy
    # orphan with no parent, can't be re-minted this way.
    parent_id = getattr(ref, "parent_id", None)
    parent_kind: str | None = None
    is_llm_planner = False
    if parent_id is not None:
        try:
            parent = store.fetch_refs_by_ids([parent_id]).get(parent_id)
        except Exception:
            parent = None
        if parent is not None:
            parent_kind = parent.kind
            if parent_kind == "todo":
                try:
                    is_llm_planner = bool((parent.meta or {}).get("llm_tier"))
                except Exception:
                    is_llm_planner = False

    meta = ref.meta or {}
    return {
        "job_id": ref.id,
        "status": status,
        "retryable": status in ("failed", "cancelled"),
        # A retry re-dispatches through the parent todo; a legacy orphan
        # (no todo parent) can't be re-minted, so don't offer the button.
        "can_retry": (
            status in ("failed", "cancelled")
            and parent_id is not None
            and parent_kind == "todo"
        ),
        "parent_id": parent_id if parent_kind == "todo" else None,
        "is_llm_planner": is_llm_planner,
        "has_transcript": bool(meta.get("transcript")),
        "job_type": meta.get("job_type"),
    }


def _youtube_meta(store: Store, ref: Any) -> dict[str, Any] | None:
    """Header context for a ``kind='youtube'`` detail page.

    The watch-page scrape (channel / thumbnail / duration) lands in
    ``cache_state.meta`` — not ``refs.meta`` — so pull the cache row to
    surface a clickable **Watch on YouTube** link and the video's
    thumbnail (a "screenshot") above the transcript body. Returns
    ``None`` only when the video id can't be recovered (so the template
    just renders the plain body).

    The thumbnail falls back to the deterministic ``i.ytimg.com`` URL
    when the og:image scrape didn't populate one, so a thumbnail shows
    even for a transcript fetched before the scrape existed.
    """
    slug = getattr(ref, "slug", None) or ""
    meta: dict[str, Any] = {}
    if slug:
        try:
            cached = store.get_cache_entry_by_slug(kind="youtube", slug=slug)
        except Exception:
            cached = None
        if cached is not None:
            meta = cached[1].meta or {}
    video_id = meta.get("video_id") or slug
    if not video_id:
        return None

    duration = ""
    if meta.get("duration_s"):
        sec = int(meta["duration_s"])
        mins, s = divmod(sec, 60)
        duration = f"{mins}m{s:02d}s"
    elif meta.get("duration_iso"):
        duration = str(meta["duration_iso"])

    return {
        "video_id": video_id,
        "watch_url": f"https://www.youtube.com/watch?v={video_id}",
        # Prefer the scraped og:image; fall back to YouTube's stable
        # per-video thumbnail endpoint so a screenshot always renders.
        "thumbnail_url": (
            meta.get("thumbnail_url")
            or f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
        ),
        "channel_name": meta.get("channel_name") or "",
        "channel_url": meta.get("channel_url") or "",
        "duration": duration,
        "published_at": meta.get("published_at") or "",
    }


#: Logbook entry ``by`` values that count as "the system did something"
#: for the quest dashboard's Happening Now callout — dispatched work
#: (agent) and measured facts (system, ``quest/logbook.py``'s
#: ``MEASURED_BY``), as opposed to a human's own note.
_QUEST_HAPPENING_BY: frozenset[str] = frozenset({"agent", "system"})


def _quest_log_row(block: Any) -> dict[str, Any]:
    """One logbook (``quest_log``) chunk → the dashboard's display shape."""
    meta = getattr(block, "meta", None) or {}
    created = getattr(block, "created_at", None)
    cost = meta.get("cost")
    return {
        "entry_type": meta.get("entry_type", "note"),
        "by": meta.get("by", "?"),
        "cost": cost,
        "stamp": created.strftime("%Y-%m-%d %H:%M") if created else "",
        "text": block.text or "",
    }


def _tag_chips(raw_tags: Any) -> list[dict[str, Any]]:
    """Shared tag-chip shape for every detail page (generic + quest).

    A closed tag (e.g. ``STATUS:active``) renders as its real
    ``PREFIX:value`` label and is inert; an open tag renders as its bare
    value and carries a × to remove. ``namespace`` is the lowercase
    "closed"/"flag"/"open" the ``Tag`` model uses (``store/types.py``) —
    not the legacy uppercase ``"OPEN"`` literal.
    """
    return [
        {
            "namespace": getattr(t, "namespace", "open"),
            "value": getattr(t, "value", ""),
            "label": (
                f"{getattr(t, 'prefix', '') or ''}:{getattr(t, 'value', '')}"
                if getattr(t, "namespace", "") == "closed"
                else getattr(t, "value", "")
            ),
            "deletable": getattr(t, "namespace", "open") == "open",
        }
        for t in raw_tags
    ]


def _quest_status_from_tags(raw_tags: Any) -> str:
    """``STATUS:<value>`` closed tag off a quest's tag list, defaulting to
    ``active`` — the same derivation the hub header used inline, factored
    out so the Lineage panel and the ``/refs/quest`` tree (each reading a
    *different* quest's tags than the one being rendered) share it."""
    status = "active"
    for t in raw_tags:
        if (
            getattr(t, "namespace", None) == "closed"
            and getattr(t, "prefix", None) == "STATUS"
        ):
            status = t.value
    return status


def _quest_headline(title: str | None, qid: int) -> str:
    """First line of a quest's striving statement (the second line, when
    present, is the "Rubric:" criteria — dropped here)."""
    lines = (title or "").split("\n", 1)
    return lines[0] if lines else f"quest {qid}"


def _quest_lineage_row(store: Store, ref: Any) -> dict[str, Any]:
    """One quest→quest ``serves`` edge's display shape — shared by the hub
    dashboard's Lineage panel (``_quest_detail``) and the ``/refs/quest``
    tree (``_quest_index``)."""
    return {
        "id": ref.id,
        "headline": _quest_headline(ref.title, ref.id),
        "status": _quest_status_from_tags(store.tags_for(ref.id)),
    }


def _quest_draft_url(store: RefsByIdStore, draft_ref_id: int) -> str:
    """``/smartdraft/<ident>`` for a draft ref id — slug when the draft has
    one (the human-legible address), else the numeric id (the reader
    route resolves both, ``_draft_ref`` in ``routes/drafts.py``)."""
    refs = store.fetch_refs_by_ids([draft_ref_id])
    dref = refs.get(draft_ref_id)
    ident = getattr(dref, "slug", None) if dref is not None else None
    return f"/smartdraft/{ident or draft_ref_id}"


def _quest_last_agentlog_id(store: Store, qid: int) -> int | None:
    """The most recent ``quest_tick`` agentlog run for this quest, or
    ``None`` if the quest has never ticked. Mirrors the "latest job by
    meta field" SQL shape in ``precis.quest.status._tick_events`` — an
    ``agentlog`` ref carries ``meta.source='quest_tick'`` +
    ``meta.parent_ref_id`` (the quest's own ref id), stamped by
    ``touch_from_env``/``open_log`` (``precis.agentlog``)."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT ref_id FROM refs "
            "WHERE kind = 'agentlog' AND deleted_at IS NULL "
            "AND meta->>'source' = 'quest_tick' "
            "AND (meta->>'parent_ref_id')::bigint = %s "
            "ORDER BY ref_id DESC LIMIT 1",
            (qid,),
        ).fetchone()
    return int(row[0]) if row else None


async def _quest_detail(request: Request, store: Store, ref: Any) -> HTMLResponse:
    """Hub dashboard for ``kind='quest'`` — the striving above the work.

    Replaces the generic ``refs/detail.html.j2`` render (which, for a
    quest, was just the striving statement + tags + a ~150-line raw
    ``pa…``/``st…`` handle dump of every ``serves`` link) with a proper
    dashboard: header (status/prio/momentum/tote), links to the dossier +
    reader-facing paper (when either exists) + the frontier/gaps panels
    below, a "happening now" recent-activity callout, the dossier
    narrative+ledger, a logbook tail, the frontier + gaps views (the same
    markdown a ``get(view=…)`` call would return, rendered inline via the
    same ``linkify_toon`` filter the generic detail body uses), and a
    servers-lite kind-count footer. Entirely read-only — no verb beyond
    the two ``get(view=…)`` reads below is dispatched.
    """
    from precis.quest import dossier as dossier_mod
    from precis.quest import frontier as frontier_mod
    from precis.quest.gaps import _live_servers, quest_momentum
    from precis.quest.logbook import LOG_KIND
    from precis.quest.tagging import quest_tag_value

    qid = int(ref.id)
    quest_tag = quest_tag_value(qid, store)
    raw_tags = store.tags_for(qid)
    status = _quest_status_from_tags(raw_tags)
    tags = _tag_chips(raw_tags)

    title_lines = (ref.title or "").split("\n", 1)
    headline = title_lines[0] if title_lines else f"quest {qid}"
    criteria = title_lines[1].strip() if len(title_lines) > 1 else ""

    live_servers = _live_servers(store, qid)

    # Lineage — the `serves` DAG one hop each way. Children are the
    # already-fetched live servers narrowed to kind='quest' (no extra
    # query); parents are the outbound edge, resolved + narrowed the
    # same way `_live_servers` narrows its inbound one (live + kind
    # check) since `links_for` alone can't tell a dangling/non-quest
    # dst from a real parent.
    lineage_children = [
        _quest_lineage_row(store, s) for s in live_servers if s.kind == "quest"
    ]
    parent_ids: list[int] = []
    seen_parent_ids: set[int] = set()
    for ln in store.links_for(qid, direction="out", relation="serves"):
        pid = ln.dst_ref_id
        if pid not in seen_parent_ids:
            seen_parent_ids.add(pid)
            parent_ids.append(pid)
    parent_refs = store.fetch_refs_by_ids(set(parent_ids)) if parent_ids else {}
    lineage_parents = [
        _quest_lineage_row(store, pref)
        for pid in parent_ids
        if (pref := parent_refs.get(pid)) is not None
        and getattr(pref, "deleted_at", None) is None
        and pref.kind == "quest"
    ]

    entries = [
        b
        for b in store.chunks.list_chunks_for_ref(qid)
        if getattr(b, "chunk_kind", None) == LOG_KIND
    ]
    momentum = quest_momentum(store, qid, servers=live_servers, entries=entries)
    tote = sum(
        float((getattr(b, "meta", None) or {}).get("cost", 0) or 0) for b in entries
    )

    # Happening now — the most recent dispatched/measured entries (by
    # agent/system); a quest with only human notes falls back to its most
    # recent entries regardless of author so the callout isn't just empty.
    dispatched = [
        b
        for b in entries
        if (getattr(b, "meta", None) or {}).get("by") in _QUEST_HAPPENING_BY
    ]
    happening_source = dispatched if dispatched else entries
    happening_now = [_quest_log_row(b) for b in reversed(happening_source[-6:])]

    log_tail = [_quest_log_row(b) for b in reversed(entries[-10:])]

    # Dossier — the internal living synthesis (a draft, ``dossier-of``).
    did = dossier_mod.dossier_ref_id(store, qid)
    dossier_url = _quest_draft_url(store, did) if did is not None else None
    narrative_text = dossier_mod.read_narrative(store, qid) if did is not None else ""
    ledger_text = dossier_mod.read_ledger(store, qid) if did is not None else ""

    # Paper — the SEPARATE reader-facing draft (``paper-of``), when one
    # exists. Nothing mints it yet (docs/backlog/paper-writing-pipeline.md);
    # the hub just links it when some other writer has.
    pid = dossier_mod.paper_ref_id(store, qid)
    paper_url = _quest_draft_url(store, pid) if pid is not None else None
    # The .docx/.pdf export endpoints still live under the classic
    # /drafts/{ident}/… path (unmoved by the smartdraft migration), so
    # derive them from the ident rather than paper_url's /smartdraft/ prefix.
    paper_ident = paper_url.rsplit("/", 1)[-1] if paper_url else None
    paper_docx_url = f"/drafts/{paper_ident}/export.docx" if paper_ident else None
    paper_pdf_url = f"/drafts/{paper_ident}/pdf" if paper_ident else None

    # Frontier + gaps — the same markdown a `get(view=…)` call returns,
    # rendered inline (the template applies the same ``linkify_toon``
    # filter the generic detail body does — no second renderer to drift).
    frontier_text, frontier_error = await await_dispatch(
        request, "get", {"kind": "quest", "id": qid, "view": "frontier"}
    )
    gaps_text, gaps_error = await await_dispatch(
        request, "get", {"kind": "quest", "id": qid, "view": "gaps"}
    )

    # Frontier scatter (Cycle C J4, + kinetics-cutover per-quest axes/axis
    # picker/viewport) — the same `Candidate`s the text frontier's markdown
    # summarises, read directly off `frontier.py`'s pure builder (a store
    # read, not a second `get(view=…)` dispatch — mirrors how
    # `_render_frontier` itself calls `quest_frontier`) so the hub can plot
    # real (x, y) points instead of re-parsing the markdown. `None` when
    # fewer than two candidates carry both axis measures — the template
    # falls back to the text-only frontier already below it.
    # Isolated like the text frontier above (which degrades to ``frontier_error``):
    # a bad struct_runs row must not 500 the whole hub — just drop the scatter.
    frontier_has_candidates = False
    frontier_scatter = None
    frontier_x: str | None = None
    frontier_y: str | None = None
    frontier_z: str | None = None
    frontier_c = True
    frontier_axis_keys: list[str] = []
    frontier_axis_counts: dict[str, int] = {}
    try:
        fr = frontier_mod.quest_frontier(store, qid)
        frontier_has_candidates = bool(
            fr.frontier or fr.dominated or fr.provisional or fr.unevaluated
        )
        objectives = fr.objectives
        default_x, default_y, default_x_label, default_y_label = (
            frontier_mod.plot_axes_for(getattr(ref, "meta", None), objectives)
        )
        # Selectable axes for the ``?fx=&fy=`` picker: any measure present
        # on >= 1 candidate (any band — a human may plot a raw run scalar
        # the quest doesn't rank on) union the declared rubric objective
        # keys (a declared-but-unmeasured-yet axis is still a valid pick,
        # it just plots nothing until a candidate lands on it). An
        # unrecognised query value is silently ignored (falls back to the
        # quest's default), never a 400 — a stale/hand-edited link degrades
        # quietly rather than erroring the whole hub.
        # Keyed by axis key, valued by how many candidates (any band —
        # provisional's merged view included) carry a value for it: the
        # pickers label each option "atom_cost (5)" so a sparse/empty axis
        # is self-explanatory before plotting it. A declared objective
        # nothing has measured yet shows "(0)" rather than vanishing.
        frontier_axis_counts = {k: 0 for k, _ in objectives}
        for c in (*fr.frontier, *fr.dominated, *fr.unevaluated):
            for k in c.measures:
                frontier_axis_counts[k] = frontier_axis_counts.get(k, 0) + 1
        for pc in fr.provisional:
            for k in pc.measures:
                frontier_axis_counts[k] = frontier_axis_counts.get(k, 0) + 1
        frontier_axis_keys = sorted(frontier_axis_counts)
        axis_key_set = set(frontier_axis_keys)
        req_fx = request.query_params.get("fx")
        req_fy = request.query_params.get("fy")
        if req_fx is not None and req_fx in axis_key_set:
            x_key, x_label = req_fx, frontier_mod.axis_label_for(req_fx)
        else:
            x_key, x_label = default_x, default_x_label
        if req_fy is not None and req_fy in axis_key_set:
            y_key, y_label = req_fy, frontier_mod.axis_label_for(req_fy)
        else:
            y_key, y_label = default_y, default_y_label
        frontier_x = x_key
        frontier_y = y_key
        # z-axis (color) picker — same "known axis key or ignore" rule as
        # fx/fy above, with one asymmetry: fx/fy fall back to a default on
        # ANY absent/bad value, but fz distinguishes "never asked" (no ``fz``
        # param at all — a bare page load) from "asked for none" (the form's
        # submitted-but-blank ``(none)`` select, an empty string). Only the
        # former gets the default colour axis — the first declared rubric
        # objective not already plotted on x/y — so the hub opens with the
        # third dimension visible, yet an explicit "(none)" pick sticks.
        # A quest with < 3 declared objectives simply has no default z.
        req_fz = request.query_params.get("fz")
        z_key: str | None = None
        z_label = ""
        if req_fz is None:
            z_key = next((k for k, _s in objectives if k not in (x_key, y_key)), None)
        elif req_fz and req_fz in axis_key_set:
            z_key = req_fz
        if z_key:
            z_label = frontier_mod.axis_label_for(z_key)
        frontier_z = z_key
        # Contour toggle (``?fc=``) — default ON (absent or anything but
        # "0"): the filled-contour underlay only draws when z is active
        # anyway, and a sparse quest's misleading field is one explicit
        # ``fc=0`` away from dismissed.
        frontier_c = request.query_params.get("fc") != "0"
        # Per-quest pinned viewport (``meta.frontier_viewport = {measure:
        # [lo, hi]}``) — unions into the plotted range so the axis doesn't
        # keep re-scaling as new points land inside a range already widened
        # by a human/agent. Malformed/absent handled by
        # `build_frontier_scatter` itself.
        raw_viewport = (getattr(ref, "meta", None) or {}).get("frontier_viewport")
        viewport = raw_viewport if isinstance(raw_viewport, dict) else None
        frontier_scatter = frontier_mod.build_frontier_scatter(
            fr.frontier + fr.dominated,
            provisional=fr.provisional,
            open_url_for=lambda c: f"/refs/structure/{c.ref_id}",
            frontier_ref_ids={c.ref_id for c in fr.frontier},
            x_measure=x_key,
            y_measure=y_key,
            x_label=x_label,
            y_label=y_label,
            viewport=viewport,
            objectives=objectives,
            z_measure=z_key,
            z_label=z_label,
            contour=frontier_c,
        )
    except Exception:
        log.warning("quest %s: frontier scatter build failed", qid, exc_info=True)

    # Latest quest_tick run — lets a human spy on what the autonomous
    # loop actually did/said last, via the existing agentlog viewer.
    last_agentlog_id = _quest_last_agentlog_id(store, qid)

    # Servers-lite — kind counts replacing the raw handle dump. Linked to
    # the kind's own browse tab when one exists (todo/paper/structure/…);
    # a kind with no ``/refs/<kind>`` tab (e.g. concept) renders as plain
    # text.
    by_kind: dict[str, int] = {}
    for s in live_servers:
        by_kind[s.kind] = by_kind.get(s.kind, 0) + 1
    # Papers get a Drive-scoped link (this quest's serving papers only, via
    # the ``quest:<id>`` tag every serves-link stamps — see
    # ``precis.quest.tagging``); every other browsable kind keeps the
    # generic ``/refs/<kind>`` tab (no equivalent tag-scoped browse exists
    # for them yet).
    quest_papers_url = "/drive?" + urlencode(
        [("submitted", "1"), ("k", "paper"), ("tag", quest_tag)]
    )
    # Exploration queue → Drive stubs: this quest's serving papers that
    # haven't been chunked yet (acquired but not ingested), so a human can
    # jump straight from "here's a gap" to "here's what to go read".
    quest_stubs_url = "/drive?" + urlencode(
        [
            ("submitted", "1"),
            ("k", "paper"),
            ("tag", quest_tag),
            ("paper_chunks", "without"),
        ]
    )
    servers_lite = [
        {
            "kind": k,
            "count": n,
            "url": (
                quest_papers_url
                if k == "paper"
                else (f"/refs/{k}" if k in _REFS_BROWSABLE_KINDS else None)
            ),
        }
        for k, n in sorted(by_kind.items(), key=lambda kv: (-kv[1], kv[0]))
    ]

    return templates.TemplateResponse(
        request,
        "refs/quest_detail.html.j2",
        {
            "active_tab": "refs:quest",
            "kind": "quest",
            "kind_label": _REF_KIND_LABEL.get("quest", "Quest"),
            "ref": _row(ref),
            "headline": headline,
            "criteria": criteria,
            "status": status,
            "tags": tags,
            "lineage_parents": lineage_parents,
            "lineage_children": lineage_children,
            "momentum": momentum,
            "tote": tote,
            "log_entry_count": len(entries),
            "dossier_url": dossier_url,
            "dossier_seeded": did is not None,
            "narrative_text": narrative_text,
            "ledger_text": ledger_text,
            "last_agentlog_id": last_agentlog_id,
            "paper_url": paper_url,
            "paper_docx_url": paper_docx_url,
            "paper_pdf_url": paper_pdf_url,
            "happening_now": happening_now,
            "log_tail": log_tail,
            "frontier_text": frontier_text,
            "frontier_error": frontier_error,
            "frontier_scatter": frontier_scatter,
            "frontier_has_candidates": frontier_has_candidates,
            "frontier_axis_keys": frontier_axis_keys,
            "frontier_x": frontier_x,
            "frontier_y": frontier_y,
            "frontier_z": frontier_z,
            "frontier_c": frontier_c,
            "frontier_axis_counts": frontier_axis_counts,
            "gaps_text": gaps_text,
            "gaps_error": gaps_error,
            "servers_lite": servers_lite,
            "servers_total": len(live_servers),
            "quest_tag": quest_tag,
            "quest_stubs_url": quest_stubs_url,
            "discussions": _followup_discussions(store, qid),
        },
    )


#: Full logbook page size — the hub itself only shows the last 10
#: entries (``log_tail`` above); this is the "see everything" view.
_QUEST_LOGBOOK_PAGE_SIZE = 50


@router.get("/quest/{qid}/logbook", response_class=HTMLResponse)
async def quest_logbook(request: Request, qid: int, page: int = 1) -> HTMLResponse:
    """Every ``quest_log`` entry for one quest, newest-first, paginated —
    the hub dashboard (``_quest_detail``) only renders the last 10."""
    from precis.quest.logbook import LOG_KIND

    store = get_store(request)
    refs = store.fetch_refs_by_ids([qid], include_deleted=False)
    ref = refs.get(qid)
    if ref is None or ref.kind != "quest":
        raise NotFound(f"quest id={qid} not found")

    title_lines = (ref.title or "").split("\n", 1)
    headline = title_lines[0] if title_lines else f"quest {qid}"

    entries = [
        b
        for b in store.chunks.list_chunks_for_ref(qid)
        if getattr(b, "chunk_kind", None) == LOG_KIND
    ]
    entries.reverse()  # newest-first, same order as the hub's tail

    page = max(page, 1)
    offset = (page - 1) * _QUEST_LOGBOOK_PAGE_SIZE
    # Over-fetch one extra row to detect "is there a next page" without a
    # separate count query — same probe as ``items.py``'s ``_recent_rows``.
    window = entries[offset : offset + _QUEST_LOGBOOK_PAGE_SIZE + 1]
    has_next = len(window) > _QUEST_LOGBOOK_PAGE_SIZE
    rows = [_quest_log_row(b) for b in window[:_QUEST_LOGBOOK_PAGE_SIZE]]

    return templates.TemplateResponse(
        request,
        "refs/quest_logbook.html.j2",
        {
            "active_tab": "refs:quest",
            "quest_id": qid,
            "headline": headline,
            "rows": rows,
            "page": page,
            "has_prev": page > 1,
            "has_next": has_next,
            "total": len(entries),
        },
    )


def _pathway_struct_row(
    structs: dict[int, Any], label: str, sid: Any
) -> dict[str, Any]:
    """One linked-structure row: ``label`` (e.g. ``"NO"``, ``"candidate"``)
    + the resolved title/url for ``sid``, or a "missing" placeholder when
    ``sid`` doesn't resolve (a soft-deleted or not-yet-fetched structure)."""
    s = structs.get(sid)
    title = getattr(s, "title", None) if s is not None else None
    return {
        "label": label,
        "ref_id": sid,
        "title": title or f"structure {sid}" + ("" if s is not None else " (missing)"),
        "url": f"/refs/structure/{sid}",
    }


def _pathway_ordered_structures(
    structs: dict[int, Any], structure_refs: dict[str, Any], state_ids: list[str]
) -> list[dict[str, Any]]:
    """Linked-structure rows in reaction order, mirroring the state list: a
    ``structure_refs`` label that's also a graph node comes first, in
    ``state_ids`` order (target path first, per ``_pathway_state_ids``); any
    label with no matching state (e.g. a slab/candidate-adjacent structure
    the graph never names) follows after, sorted alphabetically."""
    state_rank = {sid: i for i, sid in enumerate(state_ids)}
    in_state = sorted(
        (label for label in structure_refs if label in state_rank),
        key=lambda label: state_rank[label],
    )
    out_of_state = sorted(label for label in structure_refs if label not in state_rank)
    return [
        _pathway_struct_row(structs, label, structure_refs[label])
        for label in (*in_state, *out_of_state)
    ]


#: ``Measure.kind`` values the pathway-measures parser accepts — the ``op``
#: field on a ``meta.measures`` entry passes straight through as ``kind``
#: (per the proposal's "evaluator reuse layer" decision). Anything else is
#: skipped with a note rather than raising — a forward-looking op shouldn't
#: 500 the page.
_PATHWAY_MEASURE_OPS = frozenset(
    {"distance", "bond_length", "angle", "coordination", "min_distance"}
)


#: Stop the root->leaf DFS after this many paths — a legibility bound as much
#: as a DoS one (the diagram overlays one profile per path; dozens are
#: unreadable anyway) applied before the priority sort, so on truncation the
#: target path is found only if enumerated within the bound.
_PATHWAY_MAX_PATHS = 64


def _pathway_paths(
    node_ids: list[str], links: list[dict[str, Any]], target: str | None
) -> list[list[str]]:
    """Every root->leaf simple path over ``links`` (adjacency across ALL
    edge kinds — reaction + supply), server-ordered so the client draws
    one coloured profile per path rather than squashing branches onto one
    x-axis (mirrors catpath's ``viz.draw_profile``). Per-path ``visited``
    set keeps DFS cycle-safe; enumeration caps at ``_PATHWAY_MAX_PATHS``
    (real catpath graphs are tree-shaped, ~16 nodes, but a dense
    agent-authored DAG can go combinatorial) — states on un-enumerated
    paths still reach the state list via ``_pathway_detail``'s off-path
    append.

    Order: exact ``target``-leaf match first, then by length descending,
    then lexicographic leaf id. Falls back to one path over all nodes in
    array order when the graph has no roots or no path reaches a leaf
    (cyclic/degenerate — shouldn't happen, never render nothing)."""
    node_id_set = set(node_ids)
    adjacency: dict[str, list[str]] = {nid: [] for nid in node_ids}
    indeg: dict[str, int] = dict.fromkeys(node_ids, 0)
    outdeg: dict[str, int] = dict.fromkeys(node_ids, 0)
    for e in links:
        src, tgt = e["source"], e["target"]
        if src not in node_id_set or tgt not in node_id_set:
            continue
        adjacency[src].append(tgt)
        indeg[tgt] += 1
        outdeg[src] += 1

    roots = [nid for nid in node_ids if indeg[nid] == 0]
    leaves = {nid for nid in node_ids if outdeg[nid] == 0}

    paths: list[list[str]] = []

    def _dfs(node: str, path: list[str], visited: set[str]) -> None:
        if len(paths) >= _PATHWAY_MAX_PATHS:
            return
        if node in leaves:
            paths.append(list(path))
        for nxt in adjacency.get(node, []):
            if nxt in visited:
                continue
            visited.add(nxt)
            path.append(nxt)
            _dfs(nxt, path, visited)
            path.pop()
            visited.discard(nxt)

    for r in roots:
        _dfs(r, [r], {r})

    if not roots or not paths:
        return [list(node_ids)]

    def _sort_key(p: list[str]) -> tuple[int, int, str]:
        leaf = p[-1]
        is_target = 0 if (target is not None and leaf == target) else 1
        return (is_target, -len(p), leaf)

    paths.sort(key=_sort_key)
    return paths


def _pathway_graph_payload(
    graph: dict[str, Any] | None, target: str | None = None
) -> dict[str, Any] | None:
    """Trim ``meta.graph`` (a networkx ``node_link_data`` dict) to exactly what
    the client-side energy diagram needs, plus the server-enumerated root->leaf
    ``paths`` (``_pathway_paths``) the diagram now draws one profile per,
    instead of a single shared topological x-axis. Returns ``None`` when
    there's no graph to draw (an early/sparse pathway — AC4-adjacent)."""
    if not graph or not graph.get("nodes"):
        return None
    raw_nodes = graph.get("nodes", [])
    nodes = [
        {
            "id": str(n["id"]),
            "energy": n.get("energy"),
            "rel_energy": n.get("rel_energy"),
            "energy_std": n.get("energy_std"),
            "low_confidence": bool(n.get("low_confidence")),
            # CHE potential lever
            # — reservoir H atoms this node has absorbed relative to the
            # root (root = 0); null on a legacy graph catpath never
            # annotated. Always present (even null) so the client treats a
            # missing key and an explicit null identically.
            "n_H": n.get("n_H"),
        }
        for n in raw_nodes
        if n.get("id") is not None
    ]
    links = [
        {
            "source": str(e["source"]),
            "target": str(e["target"]),
            "kind": e.get("kind") or "reaction",
            "barrier": e.get("barrier"),
            "barrier_std": e.get("barrier_std"),
            "delta_e": e.get("delta_e"),
            "delta_e_std": e.get("delta_e_std"),
            "low_confidence": bool(e.get("low_confidence")),
        }
        for e in graph.get("links", [])
        if e.get("source") is not None and e.get("target") is not None
    ]
    node_ids = [n["id"] for n in nodes]
    paths = _pathway_paths(node_ids, links, str(target) if target is not None else None)
    # ``has_n_h`` gates the whole U-slider control strip in the template —
    # a legacy pathway (no node carries n_H at all) renders the diagram
    # exactly as before, zero visual change.
    has_n_h = any(n.get("n_H") is not None for n in raw_nodes)
    return {"nodes": nodes, "links": links, "paths": paths, "has_n_h": has_n_h}


def _pathway_status_banner(
    ref: Any, meta: dict[str, Any], candidate: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Context for the "no graph yet" banner rendered in place of the blank
    diagram (the orphaned-pathway-stub sweep,
    :func:`precis.quest.loop._reconcile_orphaned_pathways`, now moves a dead
    ``"computing"`` stub to ``"failed"`` instead of leaving it blank forever
    — the operator still needs to tell "still running" from "gave up" from
    "superseded by a fresher run" at a glance).

    Only meaningful for the three non-``"ready"`` statuses a pathway can
    carry with no graph yet; returns ``None`` for anything else (a legacy
    row predating ``meta.status``, or a ``"ready"`` pathway that happens to
    have no graph for some other reason — an actual anomaly, not this
    banner's job to explain)."""
    status = meta.get("status")
    if status not in ("computing", "failed", "superseded"):
        return None
    banner: dict[str, Any] = {"status": status, "candidate": candidate}
    if status == "computing":
        banner["created_at"] = getattr(ref, "created_at", None)
    elif status == "failed":
        banner["reason"] = meta.get("failed_reason") or "no reason recorded"
    else:  # superseded
        sid = meta.get("superseded_by")
        banner["superseded_by"] = sid
        banner["superseded_url"] = f"/refs/pathway/{sid}" if sid is not None else None
    return banner


def _pathway_measures(raw: Any) -> tuple[list[Measure], list[str]]:
    """Parse ``meta.measures`` into ad-hoc :class:`~precis.structure.Measure`
    objects for live per-state evaluation (``op`` -> ``kind``, ``atoms`` ->
    ``operands``, ``element`` passthrough — item 3 / AC3). An entry with an
    unrecognised ``op`` is skipped with a human-readable note rather than
    raising; ``notes`` surfaces on the page so a bad measure def is legible,
    not a silent drop."""
    measures: list[Measure] = []
    notes: list[str] = []
    for i, item in enumerate(raw or []):
        if not isinstance(item, dict):
            notes.append(f"measure #{i}: not an object, skipped")
            continue
        op = item.get("op")
        name = str(item.get("name") or op or f"measure {i}")
        if op not in _PATHWAY_MEASURE_OPS:
            notes.append(f"{name}: unknown op {op!r}, skipped")
            continue
        measures.append(
            Measure(
                kind=op,
                operands=[str(a) for a in (item.get("atoms") or [])],
                name=name,
                element=item.get("element"),
            )
        )
    return measures, notes


def _pathway_state_ids(
    diagram: dict[str, Any] | None,
    graph: dict[str, Any] | None,
    structure_refs: dict[str, Any],
) -> list[str]:
    """The state universe for the sidebar/stepper, reaction-ordered rather
    than raw JSON order: ``diagram["paths"]`` flattened in server order
    (target path first) with dupes dropped on first occurrence, then any
    remaining graph node id (on no path at all) appended in node-array
    order — so a state with no linked geometry, or no path membership,
    still appears (AC4: "no geometry linked"), never silently dropped. Falls
    back to the linked-structure keys alone for a graph-less, structures-only
    pathway."""
    if graph and graph.get("nodes"):
        node_ids = [str(n["id"]) for n in graph["nodes"] if n.get("id") is not None]
        seen: set[str] = set()
        ordered: list[str] = []
        for path in (diagram or {}).get("paths") or []:
            for nid in path:
                if nid not in seen:
                    seen.add(nid)
                    ordered.append(nid)
        for nid in node_ids:
            if nid not in seen:
                seen.add(nid)
                ordered.append(nid)
        return ordered
    return sorted(str(k) for k in structure_refs)


#: Diagram per-path palette, mirrored 1:1 from the client's own
#: ``PATH_COLORS`` in ``pathway_detail.html.j2`` (``renderDiagram``) — the
#: state-list section headers (item C) tint by the same index so a branch's
#: header and its diagram profile read as the same colour.
_PATH_COLORS: tuple[str, ...] = (
    "#4a90d9",
    "#f97316",
    "#16a34a",
    "#9333ea",
    "#0d9488",
    "#db2777",
)

#: A warning names its state as the text before this literal marker (e.g.
#: ``"NO@top seed=0 INFEASIBLE: site swap"``) — the split point
#: ``_pathway_state_warnings`` keys on.
_PATHWAY_WARNING_STATE_SEP = " seed="

#: Substrings that mark a per-state warning as a hard problem (drawn red,
#: mirroring ``low_confidence``) rather than merely informational (amber) —
#: item B.
_PATHWAY_BAD_WARNING_MARKERS: tuple[str, ...] = ("INFEASIBLE", "wrong-site", "detached")


def _pathway_state_warnings(
    warnings: list[str], state_ids: list[str]
) -> dict[str, list[dict[str, str]]]:
    """Per-state warning badges (item B): a warning names its own state as
    the text before ``" seed="`` (``"NO@top seed=0 INFEASIBLE: …"`` ->
    ``"NO@top"``); it's only mapped onto that state when the prefix is an
    EXACT match against a known state id — a warning with no ``" seed="``
    marker, or whose prefix names nothing on this graph, stays general-only
    (still shown in the unchanged Energetics warnings list, just not
    attached to a row). Severity is ``"bad"`` (INFEASIBLE / wrong-site /
    detached — a hard problem) or ``"info"`` (e.g. "RESEATED ok") otherwise.
    """
    state_id_set = set(state_ids)
    out: dict[str, list[dict[str, str]]] = {}
    for w in warnings:
        text = str(w)
        if _PATHWAY_WARNING_STATE_SEP not in text:
            continue
        prefix = text.split(_PATHWAY_WARNING_STATE_SEP, 1)[0]
        if prefix not in state_id_set:
            continue
        severity = (
            "bad"
            if any(marker in text for marker in _PATHWAY_BAD_WARNING_MARKERS)
            else "info"
        )
        out.setdefault(prefix, []).append({"text": text, "severity": severity})
    return out


def _pathway_owner_of(paths: list[list[str]]) -> dict[str, int]:
    """First-path-wins owner index per state id, mirroring the client's own
    ``xOf``/``ownerPath`` assignment in ``renderDiagram``: the earliest
    (highest-priority, target-first — ``_pathway_paths``'s own order) path
    naming a state owns it, so a shared prefix belongs to the target path
    and a branch only "starts" where it actually diverges."""
    owner: dict[str, int] = {}
    for pi, path in enumerate(paths):
        for nid in path:
            owner.setdefault(nid, pi)
    return owner


def _pathway_fragments(node_id: str) -> frozenset[str]:
    """A state id's constituent species, split on ``+`` — ``"N+O"`` ->
    ``{"N", "O"}``; a bare id like ``"NO@top"`` is its own one-fragment set.
    The vocabulary a supply edge's added/dropped diff (below) is computed
    over."""
    return frozenset(node_id.split("+"))


def _pathway_fragment_diff(
    src_id: str, tgt_id: str
) -> tuple[frozenset[str], frozenset[str]]:
    """``(added, dropped)`` fragments crossing a supply edge ``src -> tgt``
    (item C's transition annotation): a fragment named on the target but not
    the source came FROM the reservoir; one named on the source but not the
    target was PARKED back into it. ``"N+O" -> "N+H"``: added ``{"H"}``,
    dropped ``{"O"}``."""
    src_frags, tgt_frags = _pathway_fragments(src_id), _pathway_fragments(tgt_id)
    return tgt_frags - src_frags, src_frags - tgt_frags


def _pathway_supply_sibling_leaf(
    src_id: str,
    dropped: frozenset[str],
    exclude_tgt: str,
    links: list[dict[str, Any]],
    owner_of: dict[str, int],
    paths: list[list[str]],
) -> str | None:
    """Where a dropped fragment "continues": the leaf of the owner path of
    another supply edge out of the SAME source whose own target's fragments
    cover every dropped one (``"N+O" -> "O+H"`` covers a dropped ``{"O"}``
    off ``"N+O" -> "N+H"`` -> the ``"O+H"`` branch's leaf, e.g. ``"H2O"``).
    ``None`` when no such sibling resolves — the caller falls back to the
    bare "parked in reservoir" wording rather than a broken link."""
    if not dropped:
        return None
    for e in links:
        if e.get("kind") != "supply" or e.get("source") != src_id:
            continue
        tgt = e.get("target")
        if tgt is None or tgt == exclude_tgt:
            continue
        if not dropped <= _pathway_fragments(str(tgt)):
            continue
        pi = owner_of.get(str(tgt))
        if pi is None or pi >= len(paths) or not paths[pi]:
            continue
        return paths[pi][-1]
    return None


def _pathway_transition_annotation(
    edge: dict[str, Any] | None,
    links: list[dict[str, Any]],
    owner_of: dict[str, int],
    paths: list[list[str]],
) -> str | None:
    """The small transition note under a state-list row, for the edge INTO
    that state within its owner path (item C): a reaction edge's barrier
    (``"Ea=… eV"``), or a supply edge's fragment traffic — what was added
    from the reservoir, and (when fragments were dropped) either the
    sibling branch they continue in or the bare "parked in reservoir"
    fallback. ``None`` for a root state (no incoming edge) or a
    barrier-less reaction edge — nothing worth annotating."""
    if not edge:
        return None
    if (edge.get("kind") or "reaction") == "supply":
        src_id, tgt_id = str(edge["source"]), str(edge["target"])
        added, dropped = _pathway_fragment_diff(src_id, tgt_id)
        parts: list[str] = []
        if added:
            parts.append(f"+{'+'.join(sorted(added))}* from reservoir")
        if dropped:
            label = "+".join(sorted(dropped))
            leaf = _pathway_supply_sibling_leaf(
                src_id, dropped, tgt_id, links, owner_of, paths
            )
            parts.append(
                f"{label}* parked — continues in → {leaf}"
                if leaf
                else f"{label}* parked in reservoir"
            )
        return " · ".join(parts) if parts else None
    barrier = edge.get("barrier")
    if barrier is None:
        return None
    return f"Ea={round(float(barrier), 3)} eV"


def _pathway_state_sections(
    diagram: dict[str, Any] | None, state_ids: list[str]
) -> list[dict[str, Any]]:
    """Group ``state_ids`` into per-owner-path sections for the states panel
    (item C), replacing the old flat list: section 0 is the target's own
    path (server-ordered first, ``_pathway_paths``); each later section is a
    branch, tinted with the diagram's own per-path colour (``_PATH_COLORS``
    — same index the client's ``PATH_COLORS`` uses) and carries a "branches
    from <state>" subtitle naming the last state it shares with an earlier
    section. Each row carries the transition annotation for the edge
    feeding it within this owner path (``_pathway_transition_annotation``).
    States that own no diagram path at all (``_pathway_state_ids``'s
    off-path append) land in one trailing, colourless, subtitle-less
    section rather than being dropped."""
    paths = (diagram or {}).get("paths") or []
    links = (diagram or {}).get("links") or []
    owner_of = _pathway_owner_of(paths)
    link_by_pair = {(e["source"], e["target"]): e for e in links}

    sections: list[dict[str, Any]] = []
    for pi, path in enumerate(paths):
        branch_from = None
        for nid in path:
            owner = owner_of.get(nid)
            if owner is not None and owner < pi:
                branch_from = nid
        rows: list[dict[str, Any]] = []
        for idx, nid in enumerate(path):
            if owner_of.get(nid) != pi:
                continue
            edge = link_by_pair.get((path[idx - 1], nid)) if idx > 0 else None
            rows.append(
                {
                    "id": nid,
                    "annotation": _pathway_transition_annotation(
                        edge, links, owner_of, paths
                    ),
                }
            )
        if not rows:
            continue
        sections.append(
            {
                "leaf": path[-1],
                "color": _PATH_COLORS[pi % len(_PATH_COLORS)],
                "subtitle": (
                    f"branches from {branch_from}"
                    if pi > 0 and branch_from is not None
                    else None
                ),
                "rows": rows,
            }
        )

    covered = {row["id"] for sect in sections for row in sect["rows"]}
    leftover = [sid for sid in state_ids if sid not in covered]
    if leftover:
        sections.append(
            {
                "leaf": None,
                "color": None,
                "subtitle": None,
                "rows": [{"id": sid, "annotation": None} for sid in leftover],
            }
        )
    return sections


def _pathway_state_geoms_and_measures(
    store: Store,
    state_ids: list[str],
    structure_refs: dict[str, Any],
    measures: list[Measure],
) -> tuple[dict[str, dict[str, Any] | None], list[dict[str, Any]]]:
    """Per-state 3D geometry payloads (``None`` = "no geometry linked", AC4)
    plus every measure's live per-state value + the identity-drift guard
    (item 3 / AC3): ``anchor_identity_verified`` per state, folded to one
    ``verified`` flag per measure (False if unverified in ANY state — never
    silently trusted). Only states carrying geometry are evaluated at all
    (AC4: measures only evaluate states that have geometry); a structure ref
    that fails to load (missing, or a store that doesn't support it) degrades
    the same as "no geometry linked" rather than 500ing the page.

    Internally keyed by the measure's *list index*, not its display name —
    two measures can legitimately share a name (or both fall back to the
    same unnamed op), and a name-keyed dict would collapse them onto one
    shared per-state bucket, silently overwriting the first's values with
    the second's. The index is only an internal bookkeeping key; each
    ``measures_out`` entry still carries its own ``name`` for display."""
    geoms: dict[str, dict[str, Any] | None] = {}
    per_state: dict[int, dict[str, dict[str, Any]]] = {
        i: {} for i in range(len(measures))
    }
    verified: dict[int, bool] = dict.fromkeys(range(len(measures)), True)

    for state_id in state_ids:
        sid = structure_refs.get(state_id)
        scene = None
        if sid is not None:
            try:
                scene, _handles = store.structure_load(sid)
            except Exception:
                log.warning(
                    "pathway state %r: structure_load(%r) failed",
                    state_id,
                    sid,
                    exc_info=True,
                )
                scene = None
        if scene is None or not scene.atoms:
            geoms[state_id] = None
            continue
        geoms[state_id] = _geom_payload(scene, state_id)
        for i, m in enumerate(measures):
            value, verdict = evaluate_measure(scene, m)
            per_state[i][state_id] = {
                "value": value.get("value"),
                "unit": value.get("unit"),
                "error": value.get("error"),
                "verdict": verdict,
            }
            if not anchor_identity_verified(scene, m):
                verified[i] = False

    measures_out: list[dict[str, Any]] = []
    for i, m in enumerate(measures):
        vals = per_state[i]
        geom_state_ids = [s for s in state_ids if geoms.get(s) is not None]
        trace_ok = bool(geom_state_ids) and all(
            vals.get(s, {}).get("value") is not None for s in geom_state_ids
        )
        measures_out.append(
            {
                "name": m.name or m.kind,
                "op": m.kind,
                "atoms": m.operands,
                "element": m.element,
                "verified": verified[i],
                "trace_ok": trace_ok,
                "per_state": vals,
            }
        )
    return geoms, measures_out


def _pathway_state_calc_identities(
    store: Store, state_ids: list[str], structure_refs: dict[str, Any]
) -> dict[str, str]:
    """One ``calc:`` line per state (gripe 161576 remainder) — the same
    ``format_calc_identity`` label ``view='atom'`` shows on the structure
    handler (``precis.handlers.structure``), with the same run selection as
    its no-``run=`` fallback (``_atom_view_calc_row``): the latest
    ``struct_runs`` row at each structure's CURRENT version
    (``refs.meta->>'version'``, default 0) — not the globally-latest row,
    which could describe a superseded geometry. Batched: one query over
    every state's structure id (never N), via the same raw-SQL seam
    ``_pathway_run_jobs`` uses for a struct_runs read the store's own
    helpers don't expose. A state with no linked structure,
    or a structure with no run row yet, is simply absent from the returned
    dict (never invented) — the template checks membership."""
    sids = sorted({sid for sid in structure_refs.values() if sid is not None})
    if not sids:
        return {}
    try:
        with store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT DISTINCT ON (sr.ref_id) sr.ref_id, sr.provenance, "
                "sr.model, sr.params, sr.method "
                "FROM struct_runs sr JOIN refs r ON r.ref_id = sr.ref_id "
                "WHERE sr.ref_id = ANY(%s) "
                "AND sr.on_version = COALESCE((r.meta->>'version')::int, 0) "
                "ORDER BY sr.ref_id, sr.id DESC",
                (sids,),
            ).fetchall()
    except Exception:
        log.warning("pathway state calc-identity query failed", exc_info=True)
        return {}
    by_ref_id: dict[int, str] = {}
    for ref_id, provenance, model, params, method in rows:
        label = format_calc_identity(
            {
                "provenance": provenance,
                "model": model,
                "params": params,
                "method": method,
            }
        )
        if label:
            by_ref_id[int(ref_id)] = label
    out: dict[str, str] = {}
    for state_id in state_ids:
        sid = structure_refs.get(state_id)
        if sid is not None and sid in by_ref_id:
            out[state_id] = by_ref_id[sid]
    return out


#: The candidate-structure / pathway slug naming convention
#: (``precis.quest.compute._candidate_slug`` -> ``q<quest_id>cand-<digest>``,
#: and ``dispatch_autocatpath``'s own ``pslug = f"{candidate.slug}-rx-{key}"``)
#: is the only place a pathway's owning quest is recoverable from — neither
#: the candidate structure nor the pathway carries an explicit quest-id tag
#: or link. Item D's provenance strip recovers it from either slug.
_QUEST_CAND_SLUG_RE = re.compile(r"^q(\d+)cand-")


def _pathway_quest_id(ref: Any, candidate_struct: Any | None) -> int | None:
    """The owning quest's id, recovered from the ``q<id>cand-<digest>``
    slug convention — checked on this pathway's OWN slug first (it inherits
    the candidate's prefix, ``dispatch_autocatpath``), then the candidate
    structure's own slug (covers a pathway whose slug predates the
    convention or was hand-authored). ``None`` when neither matches — a
    pathway with no recoverable quest (ad-hoc / manually created)."""
    for slug in (
        getattr(ref, "slug", None),
        getattr(candidate_struct, "slug", None) if candidate_struct else None,
    ):
        if not slug:
            continue
        m = _QUEST_CAND_SLUG_RE.match(slug)
        if m:
            return int(m.group(1))
    return None


def _pathway_run_jobs(store: Store, ref_id: int) -> list[dict[str, Any]]:
    """The (up to 10, most-recent-first) ``kind='job'`` refs that produced
    this pathway — the explore/aggregate tail (``precis_pathway.
    _dispatch_common.finish``) and each seed job (``seed_job.
    _provenance_meta``) stamp their OWN meta with ``pathway_ref`` (an int)
    on completion; the seed jobs are where the ``run_log`` chunks live. Raw
    SQL off ``store.pool`` (same seam ``_pathway_candidate_stepper`` uses),
    guarded broadly so a store that can't run it (or a fake whose cursor
    never parses SQL) degrades to no run-job links, not a 500."""
    try:
        with store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT ref_id, meta->>'job_type' FROM refs "
                "WHERE kind = 'job' AND deleted_at IS NULL "
                "AND meta->>'pathway_ref' = %s "
                "ORDER BY ref_id DESC LIMIT 10",
                (str(ref_id),),
            ).fetchall()
    except Exception:
        log.warning("pathway %s: run-jobs query failed", ref_id, exc_info=True)
        return []
    return [
        {"id": int(r[0]), "label": r[1] or "job", "url": f"/refs/job/{int(r[0])}"}
        for r in rows
    ]


def _pathway_provenance(
    store: Store,
    ref: Any,
    candidate: dict[str, Any] | None,
    candidate_struct: Any | None,
) -> dict[str, Any] | None:
    """Context for the provenance strip (item D): the candidate structure
    link (already resolved elsewhere on the page — reused here), the owning
    quest (``_pathway_quest_id``), its dossier draft (``dossier-of`` edge,
    ``precis.quest.dossier``), the full logbook page, the node the compute
    ran on (``meta.ran_on``), and the job(s) that produced it
    (``_pathway_run_jobs``). Every piece is independently optional — a
    candidate-less/quest-less pathway (ad-hoc or hand-authored) still
    renders the strip with whatever resolves, never a 500; ``None`` only
    when NOTHING resolves at all (nothing to show)."""
    qid = _pathway_quest_id(ref, candidate_struct)
    dossier_url = None
    if qid is not None:
        try:
            from precis.quest import dossier as dossier_mod

            did = dossier_mod.dossier_ref_id(store, qid)
            if did is not None:
                dossier_url = _quest_draft_url(store, did)
        except Exception:
            log.warning(
                "pathway %s: dossier lookup failed for quest %s",
                ref.id,
                qid,
                exc_info=True,
            )
    ran_on = (ref.meta or {}).get("ran_on")
    run_jobs = _pathway_run_jobs(store, ref.id)
    if candidate is None and qid is None and not ran_on and not run_jobs:
        return None
    return {
        "candidate": candidate,
        "quest_id": qid,
        "quest_url": f"/refs/quest/{qid}" if qid is not None else None,
        "logbook_url": f"/refs/quest/{qid}/logbook" if qid is not None else None,
        "dossier_url": dossier_url,
        "ran_on": ran_on,
        "run_jobs": run_jobs,
    }


# ── tier-ladder UX (screening -> neb -> verify) ───────────────────────────
#
# The catpath tier ladder (docs, ``precis.quest.catalyst_seed``): a candidate
# is (optionally) run through progressively higher-fidelity passes —
# ``screening`` (thermodynamics only, no barrier), ``neb`` (a parked-reference
# barrier), ``verify`` (a coadsorbed-reference barrier that supersedes the
# parked one). A pathway ref stamps its own rung onto ``meta.tier``; when a
# verify pathway lands it ``refines``-links its now-superseded parked
# sibling (``precis.quest.compute._link_refines``). This section renders
# that ladder on the pathway detail page: a tier chip (item 1), a cross-tier
# toggle + barrier delta (item 2), and — on a verify pathway specifically —
# a ghost overlay of the parked sibling's own profile (item 3).

#: Ladder rank, low->high fidelity — the sort key the toggle orders its two
#: entries by (so a lower-fidelity tier always renders first, left of a
#: higher one, regardless of which one this page happens to be).
_TIER_RANK: dict[str, int] = {"screening": 0, "neb": 1, "verify": 2}

#: The tier a pathway with neither of these to key on falls to; also
#: :func:`precis.quest.compute._pathway_tier`'s own default (today's
#: straight-to-NEB shape — the ladder-off behaviour this must not regress).
_TIER_DEFAULT = "neb"

#: Toggle-control word per tier (item 2's own worked example, "view: screen |
#: verified"). Every tier gets its OWN word — a screening pathway next to a
#: neb sibling once rendered "screen | screen" (two identical, undistinguishable
#: controls), so the parked ``neb`` rung reads as "neb" rather than sharing
#: "screen" with the thermodynamics-only rung.
_TIER_TOGGLE_WORD: dict[str, str] = {
    "screening": "screen",
    "neb": "neb",
    "verify": "verified",
}

#: Preference order when the fallback (no ``refines`` link either way) has
#: to pick ONE sibling out of possibly several other-tier pathways for the
#: same candidate — the highest-fidelity one is the most informative
#: comparison, so it wins.
_PATHWAY_TIER_SIBLING_PREFERENCE: tuple[str, ...] = ("verify", "neb", "screening")


def _pathway_tier(meta: dict[str, Any]) -> str:
    """The tier-ladder rung a pathway belongs to (item 1) — ``meta.tier``
    when the dispatcher stamped it, else inferred from catpath's own
    verbatim ``meta.results`` (``results.screening`` / ``results.template``)
    for a pathway dispatched before the stamp existed. Mirrors
    :func:`precis.quest.compute._pathway_tier`'s own contract without
    importing that (heavier, compute-side) module into the web route; a
    legacy pathway with neither signal defaults to ``"neb"``, unchanged."""
    tier = meta.get("tier")
    if tier in _TIER_RANK:
        return str(tier)
    results = meta.get("results")
    results = results if isinstance(results, dict) else {}
    if results.get("screening") is True:
        return "screening"
    if results.get("template") == "coadsorbed":
        return "verify"
    return _TIER_DEFAULT


#: Job-meta / pathway-meta spellings that carry the rate-limiting barrier —
#: mirrors :data:`precis.quest.compute._AUTOCATPATH_BARRIER_KEYS` (the
#: pathway's own top-level ``rate_Ea`` first, then any of these under
#: ``meta.results`` for a shape that stashed it there instead).
_PATHWAY_BARRIER_KEYS: tuple[str, ...] = ("barrier", "rate_Ea", "rate_ea", "ea")


def _pathway_barrier_figure(meta: dict[str, Any]) -> float | None:
    """This pathway's own rate-limiting barrier (eV), or ``None`` — the
    delta header's (item 2) input on each side of the toggle."""
    v = meta.get("rate_Ea")
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    results = meta.get("results")
    if isinstance(results, dict):
        for k in _PATHWAY_BARRIER_KEYS:
            rv = results.get(k)
            if isinstance(rv, (int, float)) and not isinstance(rv, bool):
                return float(rv)
    return None


def _pathway_tier_sibling(
    store: Store, ref: Any, this_tier: str, candidate_ref_id: int | None
) -> dict[str, Any] | None:
    """The other-tier pathway for the SAME candidate (item 2): the linked
    ``refines`` sibling in either direction when one exists (a verify
    pathway's own outgoing edge names its parked sibling; a parked
    pathway's incoming edge names the verify pathway that supersedes it —
    ``precis.quest.compute._link_refines``), else the LATEST pathway of
    each other tier sharing this one's ``meta.candidate_ref``, preferring
    verify > neb > screening when more than one is available (the
    highest-fidelity comparison is the most informative one). Returns
    ``{"ref_id", "tier", "meta"}`` or ``None`` — no candidate, no store
    support for the fallback query, or truly no sibling at any tier."""

    def _resolve(other_id: int) -> dict[str, Any] | None:
        if other_id == ref.id:
            return None
        other = store.fetch_refs_by_ids([other_id]).get(other_id)
        if other is None:
            return None
        other_meta = other.meta or {}
        return {
            "ref_id": other_id,
            "tier": _pathway_tier(other_meta),
            "meta": other_meta,
        }

    try:
        out_links = store.links_for(ref.id, direction="out", relation="refines")
    except Exception:
        out_links = []
    for lnk in out_links:
        dst = getattr(lnk, "dst_ref_id", None)
        if dst is None:
            continue
        found = _resolve(int(dst))
        if found is not None:
            return found

    try:
        in_links = store.links_for(ref.id, direction="in", relation="refines")
    except Exception:
        in_links = []
    for lnk in in_links:
        src = getattr(lnk, "src_ref_id", None)
        if src is None:
            continue
        found = _resolve(int(src))
        if found is not None:
            return found

    if candidate_ref_id is None:
        return None
    try:
        with store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT ref_id, meta->>'tier' FROM refs "
                "WHERE kind = 'pathway' AND deleted_at IS NULL "
                "AND meta->>'candidate_ref' = %s AND ref_id != %s "
                "ORDER BY ref_id DESC",
                (str(candidate_ref_id), ref.id),
            ).fetchall()
    except Exception:
        log.warning("pathway %s: tier-sibling query failed", ref.id, exc_info=True)
        return None
    latest_by_tier: dict[str, int] = {}
    for r in rows:
        t = r[1] if r[1] in _TIER_RANK else _TIER_DEFAULT
        latest_by_tier.setdefault(t, int(r[0]))  # rows are ref_id DESC -> latest first
    for pref in _PATHWAY_TIER_SIBLING_PREFERENCE:
        if pref != this_tier and pref in latest_by_tier:
            return _resolve(latest_by_tier[pref])
    return None


def _pathway_tier_toggle(
    ref_id: int,
    this_tier: str,
    sibling: dict[str, Any] | None,
    this_meta: dict[str, Any],
) -> dict[str, Any] | None:
    """Cross-tier toggle + delta context (item 2): a "view: neb |
    verified"-style control (ordered low->high fidelity, ``_TIER_RANK``,
    each tier under its own ``_TIER_TOGGLE_WORD``) with the currently-viewed
    tier as plain text and the sibling as a link to its page — plus, only
    when exactly one side is verify-tier and BOTH carry a barrier figure, a
    one-line "verified − neb: +0.12 eV" delta.
    ``None`` when there's no sibling at all (``_pathway_tier_sibling``)."""
    if sibling is None:
        return None
    entries = sorted(
        (
            {"tier": this_tier, "ref_id": ref_id, "current": True},
            {"tier": sibling["tier"], "ref_id": sibling["ref_id"], "current": False},
        ),
        key=lambda e: _TIER_RANK.get(str(e["tier"]), 1),
    )
    for e in entries:
        tier = str(e["tier"])
        e["label"] = _TIER_TOGGLE_WORD.get(tier, tier)
        e["url"] = None if e["current"] else f"/refs/pathway/{e['ref_id']}"

    delta_text = None
    tiers = {this_tier, sibling["tier"]}
    if "verify" in tiers and len(tiers) == 2:
        this_barrier = _pathway_barrier_figure(this_meta)
        sib_barrier = _pathway_barrier_figure(sibling["meta"] or {})
        if this_barrier is not None and sib_barrier is not None:
            verify_barrier = this_barrier if this_tier == "verify" else sib_barrier
            lower_tier = sibling["tier"] if this_tier == "verify" else this_tier
            lower_barrier = sib_barrier if this_tier == "verify" else this_barrier
            lower_word = _TIER_TOGGLE_WORD.get(str(lower_tier), str(lower_tier))
            delta = verify_barrier - lower_barrier
            delta_text = f"verified − {lower_word}: {delta:+.2f} eV"

    return {"entries": entries, "delta_text": delta_text}


def _pathway_fragment_multiset(node_id: str) -> Counter[str]:
    """A state id's constituent species as a MULTISET (``"H+H"`` ->
    ``{"H": 2}``) — the ghost overlay's join (below) needs multiplicity,
    unlike :func:`_pathway_fragments`'s frozenset (which the supply-edge
    diff, item C, deliberately dedupes)."""
    return Counter(node_id.split("+"))


def _pathway_parked_maps_to_coadsorbed(parked_id: str, coadsorbed_id: str) -> bool:
    """True when a parked(neb)-tier state id maps onto a verify(coadsorbed)-
    tier one (item 3's ghost-overlay join): an exact id match, or the
    coadsorbed id's multiset is the parked id's multiset plus EXACTLY one
    extra fragment (the spectator) — e.g. ``"NH+O"`` maps onto ``"NH"``
    (drop spectator ``"O"``), but not onto ``"NH2"`` (not a superset) nor a
    coadsorbed id with two-or-more extra fragments (not a single-spectator
    difference)."""
    if parked_id == coadsorbed_id:
        return True
    parked = _pathway_fragment_multiset(parked_id)
    coadsorbed = _pathway_fragment_multiset(coadsorbed_id)
    if sum(coadsorbed.values()) != sum(parked.values()) + 1:
        return False
    if sum((parked - coadsorbed).values()) != 0:  # every parked fragment covered
        return False
    return sum((coadsorbed - parked).values()) == 1  # exactly one spectator extra


def _pathway_ghost_series(
    diagram: dict[str, Any] | None, tier_sibling: dict[str, Any] | None
) -> list[dict[str, Any]] | None:
    """The parked(neb)-tier sibling's own energy profile, mapped onto THIS
    verify pathway's own state ids (item 3): one point per diagram node that
    resolves a parked match (``_pathway_parked_maps_to_coadsorbed``),
    carrying the parked node's OWN ``rel_energy``/``n_H`` verbatim — no
    interpolation, no fabrication (an unmapped state simply isn't ghosted).
    ``None`` when there's no NEB sibling specifically (a screening-tier
    sibling carries no per-state barrier graph worth ghosting), neither side
    has a graph, or fewer than 2 states map (too little to draw a line)."""
    if diagram is None or tier_sibling is None or tier_sibling["tier"] != "neb":
        return None
    sib_graph = (tier_sibling["meta"] or {}).get("graph")
    if not sib_graph or not sib_graph.get("nodes"):
        return None
    parked_nodes = [n for n in sib_graph["nodes"] if n.get("id") is not None]
    points: list[dict[str, Any]] = []
    for node in diagram["nodes"]:
        coadsorbed_id = node["id"]
        match = next(
            (
                pn
                for pn in parked_nodes
                if _pathway_parked_maps_to_coadsorbed(str(pn["id"]), coadsorbed_id)
            ),
            None,
        )
        if match is None or match.get("rel_energy") is None:
            continue
        points.append(
            {
                "state_id": coadsorbed_id,
                "rel_energy": match["rel_energy"],
                "n_H": match.get("n_H"),
            }
        )
    if len(points) < 2:
        return None
    return points


def _pathway_candidate_stepper(
    store: Store, ref_id: int, substrate: Any, target: Any
) -> dict[str, Any] | None:
    """Sibling-candidate rank/N context for the stepper (item E): every
    OTHER ``pathway`` ref sharing this one's ``results.substrate`` +
    ``results.target``, ranked by ``rate_Ea`` ascending (nulls last),
    capped at 100. There's no store-level "pathways for this reaction"
    query yet, so this reads raw SQL off ``store.pool`` (the same seam
    ``precis.quest.dossier``/``_quest_last_agentlog_id`` already use) —
    guarded broadly so a store that can't run it (or a fake whose cursor
    never parses SQL, ``tests/precis_web/conftest.py``) degrades to no
    stepper, exactly like a real store with no sibling candidates. Returns
    ``None`` unless this ref itself is among the results AND at least one
    sibling exists (a lone result is nothing to step through)."""
    if not substrate or not target:
        return None
    try:
        with store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT ref_id, meta->>'rate_Ea', meta->>'span' FROM refs "
                "WHERE kind = 'pathway' AND deleted_at IS NULL "
                "AND meta->'results'->>'substrate' = %s "
                "AND meta->'results'->>'target' = %s "
                "ORDER BY (meta->>'rate_Ea')::float ASC NULLS LAST, ref_id "
                "LIMIT 100",
                (substrate, target),
            ).fetchall()
    except Exception:
        log.warning(
            "pathway %s: sibling-candidates query failed", ref_id, exc_info=True
        )
        return None
    if len(rows) < 2:
        return None
    siblings = [
        {
            "ref_id": int(r[0]),
            "rate_ea": float(r[1]) if r[1] is not None else None,
            "span": float(r[2]) if r[2] is not None else None,
        }
        for r in rows
    ]
    rank = next((i for i, s in enumerate(siblings) if s["ref_id"] == ref_id), None)
    if rank is None:
        return None
    rank += 1
    return {
        "rank": rank,
        "total": len(siblings),
        "siblings": siblings,
        "prev_ref_id": siblings[rank - 2]["ref_id"] if rank > 1 else None,
        "next_ref_id": siblings[rank]["ref_id"] if rank < len(siblings) else None,
    }


async def _pathway_detail(request: Request, store: Store, ref: Any) -> HTMLResponse:
    """Detail page for ``kind='pathway'`` — a autocatpath reaction-energetics
    network (candidate structure → adsorbate/intermediate structures →
    computed barriers).

    ``pathway`` is an EXTERNAL plugin kind (the autocatpath bridge): its
    handler isn't loaded in every process (dev sessions, some workers),
    so — unlike most other kinds on this page — this never dispatches a
    ``get()`` verb. Everything renders off stored data already on the
    ``Store``: ``ref.meta`` (candidate/structure refs, the computed
    ``results`` network, config), the single ``pathway_body`` chunk
    (a short markdown methods section), and a batched
    ``fetch_refs_by_ids`` to resolve the linked structures' titles.
    Every ``meta`` key is optional — a sparse/early-slice pathway is
    expected, not an error.
    """
    meta = ref.meta or {}

    # Body — exactly one ``pathway_body`` chunk. Rendered the same way
    # the generic detail body renders a markdown-ish chunk (linkify_toon
    # in the template) — no second markdown renderer to drift.
    body_text = ""
    for b in store.chunks.list_chunks_for_ref(ref.id):
        if getattr(b, "chunk_kind", None) == "pathway_body":
            body_text = b.text or ""
            break

    # Linked structures — the candidate (base catalyst) plus every
    # adsorbate/intermediate structure ``structure_refs`` names. One
    # batched fetch resolves titles for all of them; a link that doesn't
    # resolve (deleted / not yet fetched) still renders, marked missing.
    candidate_ref_id = meta.get("candidate_ref")
    structure_refs: dict[str, Any] = meta.get("structure_refs") or {}
    want_ids = {sid for sid in structure_refs.values() if sid is not None}
    if candidate_ref_id is not None:
        want_ids.add(candidate_ref_id)
    structs = store.fetch_refs_by_ids(list(want_ids)) if want_ids else {}

    candidate = (
        _pathway_struct_row(structs, "candidate", candidate_ref_id)
        if candidate_ref_id is not None
        else None
    )

    # Energetics summary — defensive reads off ``meta['results']`` (the
    # computed network); every field is optional.
    results = meta.get("results") or {}
    warnings_raw = results.get("warnings") or []
    if not isinstance(warnings_raw, list):
        warnings_raw = [warnings_raw]
    nodes = results.get("nodes")
    edges = results.get("edges")
    warnings_list: list[str] = [str(w) for w in warnings_raw]
    results_summary = {
        "substrate": results.get("substrate"),
        "target": results.get("target"),
        "backend": results.get("backend"),
        "energy_reference": results.get("energy_reference"),
        "relaxed_lattice_A": results.get("relaxed_lattice_A"),
        "n_nodes": len(nodes) if isinstance(nodes, (list, dict)) else None,
        "n_edges": len(edges) if isinstance(edges, (list, dict)) else None,
        "warnings": warnings_list,
    }

    # Kinetics panel — the catpath report's microkinetics panel, off the
    # raw ``kinetics.solve`` record ``precis_pathway.runner.run_kinetics``
    # folded into ``meta.results``. ``kinetics`` is the trimmed payload the
    # vendored panel script renders (None -> panel omitted);
    # ``kinetics_error`` is the runner's did-not-run reason, shown instead.
    try:
        kinetics = kinetics_payload(results)
    except Exception:
        # A record shape the trim didn't anticipate must cost the panel,
        # never the page.
        log.warning("pathway %s: kinetics payload failed", ref.id, exc_info=True)
        kinetics = None
    kinetics_error = results.get("kinetics_error")

    # CHE potential lever — the
    # explorer's U-slider readout strip. Every field is optional; on a
    # legacy pathway all five stay None (the slider itself is hidden by
    # ``diagram.has_n_h``, computed below, regardless of these).
    results_electro = {
        "U_L": results.get("U_L"),
        "U_opt": results.get("U_opt"),
        "span_at_UL": results.get("span_at_UL"),
        "span_at_Uopt": results.get("span_at_Uopt"),
        "P_side": results.get("P_side"),
        "T": results.get("T"),
    }

    # Interactive explorer — the
    # clickable energy diagram (item 1), the per-state 3D cell viewer (item 2),
    # and per-state measures (item 3). All three degrade gracefully: no graph
    # -> no diagram; no/partial structure_refs -> per-state "no geometry
    # linked" (AC4); an unrecognised measure op -> skipped with a note.
    graph = meta.get("graph")
    diagram = _pathway_graph_payload(graph, target=results.get("target"))
    measure_defs, measure_notes = _pathway_measures(meta.get("measures"))
    state_ids = _pathway_state_ids(diagram, graph, structure_refs)
    structures = _pathway_ordered_structures(structs, structure_refs, state_ids)
    state_geoms, measures_summary = _pathway_state_geoms_and_measures(
        store, state_ids, structure_refs, measure_defs
    )
    # gripe 161576 remainder: which calculator produced each state's
    # energetics — batched over every state's structure id, never N queries.
    state_calc = _pathway_state_calc_identities(store, state_ids, structure_refs)
    n_states_with_geom = sum(1 for g in state_geoms.values() if g is not None)
    # A state's ``rel_energy`` may be recorded as explicitly null (computed
    # but not yet available, distinct from "no graph node at all") — the
    # state list surfaces that as its own "no energy yet" badge alongside
    # the geometry one, and the JS diagram excludes these from level/hump/
    # trace plotting rather than coercing null to 0 (item 3 hardening).
    states_missing_energy = {
        str(n["id"])
        for n in (diagram["nodes"] if diagram else [])
        if n.get("rel_energy") is None
    }
    # Branch-grouped state list (item C) + per-state warning badges (item B)
    # — both replace/extend the old flat state list purely additively; the
    # underlying ``state_ids`` order (stepper prev/next, JS payload) is
    # unchanged.
    state_sections = _pathway_state_sections(diagram, state_ids)
    state_warnings = _pathway_state_warnings(warnings_list, state_ids)

    # Provenance strip (item D) + candidate stepper (item E) — both
    # optional, both degrade to "omit" rather than a 500 on a candidate-
    # less/quest-less/sibling-less pathway.
    candidate_struct = (
        structs.get(candidate_ref_id) if candidate_ref_id is not None else None
    )
    provenance = _pathway_provenance(store, ref, candidate, candidate_struct)
    stepper = _pathway_candidate_stepper(
        store, ref.id, results.get("substrate"), results.get("target")
    )

    # Tier ladder (screening -> neb -> verify): the chip (item 1), the
    # cross-tier toggle + barrier delta (item 2), and — verify pathways only
    # — the parked sibling's ghost overlay (item 3). All optional; a legacy
    # or ad-hoc pathway (no candidate, no sibling) still renders, just
    # without them.
    tier = _pathway_tier(meta)
    tier_sibling = _pathway_tier_sibling(store, ref, tier, candidate_ref_id)
    tier_toggle = _pathway_tier_toggle(ref.id, tier, tier_sibling, meta)
    ghost_series = _pathway_ghost_series(diagram, tier_sibling)

    # No graph yet -> a status-aware banner (computing/failed/superseded)
    # instead of just the bare empty-diagram message.
    status_banner = (
        _pathway_status_banner(ref, meta, candidate) if not diagram else None
    )

    return templates.TemplateResponse(
        request,
        "refs/pathway_detail.html.j2",
        {
            "active_tab": "refs:pathway",
            "kind": "pathway",
            "kind_label": _REF_KIND_LABEL.get("pathway", "Pathway"),
            "ref": _row(ref),
            "status": meta.get("status"),
            "rate_ea": meta.get("rate_Ea"),
            "n_structures": meta.get("n_structures"),
            "slice_num": meta.get("slice"),
            "produced_by": meta.get("produced_by"),
            "autocatpath_version": meta.get("autocatpath_version"),
            "config_snapshot_yaml": meta.get("config_snapshot_yaml"),
            "has_graph": bool(meta.get("graph")),
            "status_banner": status_banner,
            "candidate": candidate,
            "structures": structures,
            "body_text": body_text,
            "results_summary": results_summary,
            "results_electro": results_electro,
            "kinetics": kinetics,
            "kinetics_error": kinetics_error,
            "diagram": diagram,
            "state_ids": state_ids,
            "state_sections": state_sections,
            "state_geoms": state_geoms,
            "state_calc": state_calc,
            "state_warnings": state_warnings,
            "n_states_with_geom": n_states_with_geom,
            "states_missing_energy": states_missing_energy,
            "measures": measures_summary,
            "measure_notes": measure_notes,
            "provenance": provenance,
            "stepper": stepper,
            "tier": tier,
            "tier_toggle": tier_toggle,
            "ghost_series": ghost_series,
        },
    )


#: The kinds the Refs tab pre-checks by default — note-like, browsable,
#: low-friction. The other checkbox-eligible kinds stay unchecked
#: unless the operator opts in (via ``?all=1`` or by tickering them
#: manually). Order pinned for stable rendering.
_DEFAULT_REFS_KINDS: tuple[str, ...] = ("memory", "conv", "gripe", "pres")

#: Every kind the consolidated Refs page knows how to render. Kept as
#: a static list — extending it is a one-liner when a new browsable
#: kind ships. We don't trust the hub's full ``kinds`` set here because
#: it includes non-browsable kinds (calc / random / math) whose
#: ``list_refs`` would either error or render meaningless.
_REFS_BROWSABLE_KINDS: tuple[str, ...] = (
    "memory",
    "conv",
    "gripe",
    "pres",
    "oracle",
    "paper",
    "patent",
    "todo",
    "job",
    "finding",
    "citation",
    "anki",
    "perplexity-research",
    "perplexity-reasoning",
    "web",
    "youtube",
    "websearch",
    "message",
    # Quest (the striving/lab-notebook kind) + its candidate `structure`s.
    # QuestHandler.get(id=N) renders the append-only logbook (the lab
    # notebook) and StructureHandler.get renders the candidate scene through
    # the generic detail template; without these the quest page + every
    # candidate link 400 with "no browse tab" even though they render fine.
    "quest",
    "structure",
    # Autocatpath's reaction-energetics ref (candidate structure → adsorbate/
    # intermediate structures → computed barriers). An EXTERNAL plugin
    # kind — its handler isn't loaded in every process — so its detail
    # page (``_pathway_detail``) renders entirely off stored data rather
    # than dispatching ``get()`` the way the generic template does.
    "pathway",
    # Machine-detected ops/health rows (non-embedded). The /alerts list
    # links each row to /refs/alert/<id>; without this the detail page
    # 400s ("no browse tab for kind='alert'"). AlertHandler.get(id=N)
    # renders fine through the generic detail template.
    "alert",
    # Cached generators / utility kinds — they still store refs in the
    # DB so detail pages work; list pages render whatever the kind's
    # ``list_refs`` returns (empty for the on-demand kinds when the
    # cache is cold). Added 2026-06-16 after live 400s on
    # /refs/math/* and /refs/finding/* from hover-preview links.
    "math",
    "calc",
    "skill",
    "tag",
    "provenance",
    "random",
)

_PER_KIND_LIMIT = 20  # cap rows per kind so 19-kind search stays readable

#: Cache-backed kinds (``CacheBackedHandler`` subclasses) whose ``get``
#: verb *fetches on a miss* — a paid, slow upstream call for the paid
#: tiers. The read-only detail page must pass ``no_fetch=True`` so
#: rendering an existing ref serves the stored body instead of silently
#: re-running the fetch (a ~$0.50 / 2–10 min perplexity-research call, a
#: billed Sonar query) on every page view. Keep in sync with the
#: ``CacheBackedHandler`` subclasses.
_CACHE_BACKED_KINDS: frozenset[str] = frozenset(
    {
        "math",
        "news",
        "orcid",
        "perplexity-reasoning",
        "perplexity-research",
        "semanticscholar",
        "web",
        "websearch",
        "wikipedia",
        "youtube",
    }
)

#: Kinds whose refs do **not** live in the ``refs`` table, so the
#: consolidated browser can't reach them through ``list_refs`` /
#: ``search_refs_lexical`` (both return nothing — confirmed in prod:
#: zero rows for either kind). They ARE searchable through their own
#: ``search`` verb — ``skill`` over the on-disk skill files, ``tag``
#: over the tag vocabulary — so the consolidated view dispatches that
#: verb and renders its markdown result instead of a row grid.
_HANDLER_SEARCHED_KINDS: frozenset[str] = frozenset({"skill", "tag"})

#: Browsable kinds that can only ever render empty in the consolidated
#: view: ``random`` mints on demand and ``provenance`` is a report over
#: other refs — neither has ``refs`` rows *or* a ``search`` verb. They
#: stay in ``_REFS_BROWSABLE_KINDS`` (detail routes may still target
#: them) but are dropped from the browser's checkboxes so the page never
#: offers a control that returns nothing by construction.
_CONSOLIDATED_HIDDEN_KINDS: frozenset[str] = frozenset({"random", "provenance"})

#: The kinds the consolidated browser offers as checkboxes / searches:
#: every browsable kind minus the always-empty ones.
_CONSOLIDATED_KINDS: tuple[str, ...] = tuple(
    k for k in _REFS_BROWSABLE_KINDS if k not in _CONSOLIDATED_HIDDEN_KINDS
)


# ---- References extraction (MVP for #188) ---------------------------
#
# Scan a body for the same kind:ref shapes the linkifier picks up
# (prefixed ``kind:slug``, bare paper cite_keys, bare discord conv
# handles). Resolve each in a single batched query and shape an
# expansion for inline rendering below the body.


def _extract_handles(body: str) -> list[tuple[str, str, str | None]]:
    """Every kind:ref handle in ``body`` as ``(kind, id, chunk)`` triples.

    Thin wrapper over the shared ``mentions.extract_handles`` — the
    grammar + dedup live there so the read-time References panel and the
    write-time autolinker can't drift apart.
    """
    return mentions.extract_handles(body)


def _expand_handle(
    store: Store, kind: str, ref_id: str, chunk: str | None
) -> dict[str, Any]:
    """Resolve one ``(kind, id, chunk)`` triple to a display row.

    Returns a row carrying:
      ``handle`` — what to print as the cite handle
      ``url`` — click-through URL (the resolver path)
      ``title`` — best-effort title (paper cite, memory id, etc.)
      ``preview`` — short body preview when available
      ``status`` — ``"resolved"`` / ``"missing"`` / ``"deleted"``
    """
    raw_handle = f"{kind}:{ref_id}" + (chunk or "")
    url = f"/r/{kind}/{ref_id}" + (f"?chunk={chunk[1:]}" if chunk else "")
    # Numeric-id-or-slug resolution is single-sourced in the shared
    # mentions module (same two-step the write-time autolinker uses).
    ref = mentions.resolve_handle_ref(store, ref_id, include_deleted=True)
    if ref is None:
        return {
            "handle": raw_handle,
            "url": url,
            "title": "(not found)",
            "preview": "",
            "status": "missing",
            "kind": kind,
        }
    if getattr(ref, "deleted_at", None) is not None:
        return {
            "handle": raw_handle,
            "url": url,
            "title": (getattr(ref, "title", "") or "(untitled)").split("\n", 1)[0][
                :120
            ],
            "preview": "(deleted)",
            "status": "deleted",
            "kind": kind,
        }
    title = (getattr(ref, "title", "") or "(untitled)").split("\n", 1)[0][:160]
    preview = ""
    # For chunk-addressed handles, fetch the actual chunk text.
    if chunk and chunk.startswith("~") and chunk[1:].isdigit():
        ord_pos = int(chunk[1:])
        try:
            blocks = store.chunks.list_chunks_for_ref(ref.id)
            for b in blocks:
                if getattr(b, "ord", -1) == ord_pos:
                    preview = (b.text or "")[:400].rstrip()
                    if len(b.text or "") > 400:
                        preview += "…"
                    break
        except Exception:
            pass
    has_chunks = False
    if not preview:
        # Fall back to the first block (or the title-derived hint).
        try:
            blocks = store.chunks.list_chunks_for_ref(ref.id)
            if blocks:
                has_chunks = True
                preview = (blocks[0].text or "")[:400].rstrip()
                if len(blocks[0].text or "") > 400:
                    preview += "…"
        except Exception:
            pass
    else:
        # We hit the chunk-addressed path above which means chunks exist.
        has_chunks = True
    # Status taxonomy for verification badges (#191):
    #   resolved → ref exists and has chunks (the typical successful case)
    #   stub     → ref exists, no chunks yet (paper awaiting fetcher)
    #   missing  → ref id doesn't resolve
    #   deleted  → ref exists but soft-deleted
    status = "resolved" if has_chunks else "stub"
    # Citation metadata for BibTeX / Markdown export — only meaningful
    # for paper kind, but the dict shape is uniform so the template
    # doesn't have to branch.
    citation: dict[str, Any] = {}
    if kind == "paper":
        slug = getattr(ref, "slug", None) or ""
        year = getattr(ref, "year", None)
        # Citation-form names, tolerant of every stored author shape
        # (``{name}`` from ingest, ``{family, given}`` from the editor).
        author_list = author_names(getattr(ref, "authors", None), order="sortable")
        # Try to pull DOI off ref.meta if the handler stored it there
        # (papers ingested from Crossref do).
        meta = getattr(ref, "meta", None) or {}
        doi = meta.get("doi") if isinstance(meta, dict) else None
        citation = {
            "cite_key": slug,
            "authors": author_list,
            "year": year,
            "doi": doi,
            "url": (f"https://doi.org/{doi}" if doi else None),
        }

    # The shared paper identity header (year · title / venue · first … last),
    # so a paper handle in a memory/finding body reads the same here as it
    # does on hover. ``held`` = has body chunks → sky, else amber (stub).
    # ``.as_dict()`` keeps the row JSON-serialisable — the References panel
    # dumps ``references | tojson`` for its copy button, which a dataclass
    # would break.
    head = (
        paper_head(ref, held=has_chunks).as_dict()
        if kind in PAPER_IDENT_KINDS
        else None
    )
    return {
        "handle": raw_handle,
        "url": url,
        "title": title,
        "preview": preview,
        "status": status,
        "kind": kind,
        "slug": getattr(ref, "slug", None) or "",
        "citation": citation,
        "head": head,
    }


@router.get("", response_class=HTMLResponse)
async def consolidated(
    request: Request,
    q: str | None = None,
    kinds: str | None = None,
    all: int = 0,
) -> HTMLResponse:
    """Consolidated cross-kind ref browser with kind checkboxes.

    Replaces the old per-kind nav tabs for memory / conv / gripe / pres
    — see ``T12.6`` in the session notes. Each kind has a checkbox;
    when ``?all=1`` is set, every browsable kind lights regardless of
    the ``kinds`` query param. The 🔍 loupe in the nav posts here with
    ``?all=1`` so a global query hits everything we have local.

    Per-kind detail (``/refs/{kind}/{ref_id}``) and the per-kind list
    pages (``/refs/{kind}``) keep working — they're the long-form
    affordances for pagination, date filters, sort. The consolidated
    view is the casual "I half-remember something" surface.
    """
    if all:
        selected: list[str] = list(_CONSOLIDATED_KINDS)
    elif kinds:
        # Tolerate trailing commas / whitespace / unknown kinds.
        requested = {k.strip() for k in kinds.split(",") if k.strip()}
        selected = [k for k in _CONSOLIDATED_KINDS if k in requested]
        # Preserve the operator's ordering for kinds we didn't recognise
        # so a future-added kind shows up when its checkbox is added.
        for k in requested:
            if k not in selected and k not in _CONSOLIDATED_KINDS:
                selected.append(k)
    else:
        selected = list(_DEFAULT_REFS_KINDS)

    store = get_store(request)
    query = (q or "").strip()
    by_kind: dict[str, list[dict[str, object]]] = {}
    #: Handler-searched kinds (skill / tag) have no ``refs`` rows, so
    #: they contribute a rendered-markdown block from their own ``search``
    #: verb rather than a row grid.
    by_kind_md: dict[str, str] = {}
    for kind in selected:
        if kind in _HANDLER_SEARCHED_KINDS:
            # No refs rows to list — dispatch the kind's own search verb
            # (skill files / tag vocabulary) and render its markdown.
            # An empty query lists where the verb supports it (the skill
            # index); a verb that requires q= on empty input just yields
            # an error we skip, so the section drops out cleanly.
            args: dict[str, Any] = {"kind": kind, "page_size": _PER_KIND_LIMIT}
            if query:
                args["q"] = query
            try:
                body, is_error = await await_dispatch(request, "search", args)
            except Exception:
                continue
            if is_error or not (body or "").strip():
                continue
            by_kind_md[kind] = body
            continue
        try:
            if query:
                hits = store.search_refs_lexical(
                    q=query, kind=kind, limit=_PER_KIND_LIMIT
                )
                refs = [ref for ref, _ in hits]
            else:
                refs = store.list_refs(kind=kind, limit=_PER_KIND_LIMIT)
        except Exception:
            # Unsupported / unregistered kind on this process — skip the
            # whole bucket rather than 500 the page.
            continue
        if not refs:
            continue
        rows: list[dict[str, object]] = []
        for r in refs:
            rows.append(
                {
                    "id": r.id,
                    "display_title": display_title(getattr(r, "title", ""))
                    or "(untitled)",
                    "url": _consolidated_ref_url(kind, r.id),
                }
            )
        by_kind[kind] = rows

    return templates.TemplateResponse(
        request,
        "refs/consolidated.html.j2",
        {
            "active_tab": "refs",
            "q": query,
            "selected": set(selected),
            "all_browsable": list(_CONSOLIDATED_KINDS),
            "default_kinds": list(_DEFAULT_REFS_KINDS),
            "by_kind": by_kind,
            "by_kind_md": by_kind_md,
            "all_lit": bool(all),
            "total": sum(len(v) for v in by_kind.values()),
        },
    )


#: Per-kind URL shape for the native detail viewer in consolidated view.
_CONSOLIDATED_KIND_URLS: dict[str, str] = {
    "paper": "/papers/{id}",
    "todo": "/tasks?focus={id}",
    "job": "/tasks?focus={id}",
}


def _consolidated_ref_url(kind: str, ref_id: int) -> str:
    template = _CONSOLIDATED_KIND_URLS.get(kind, "/refs/{kind}/{id}")
    return template.format(kind=kind, id=ref_id)


async def _quest_index(request: Request, store: Store) -> HTMLResponse:
    """Tree view for ``kind='quest'`` — the ``serves`` DAG among quests.

    Replaces the generic flat ``refs/index.html.j2`` list (which had no
    notion of which quest serves which) with a forest render: a quest is a
    *root* iff it has no outbound ``serves`` edge to another live quest;
    every other live quest hangs under each live quest it serves (a DAG —
    a sub-quest serving two parents renders under both). Outbound is
    derived as the inverse of the inbound map (any id that shows up as
    someone's child has an outbound edge) rather than a second per-quest
    query — quest counts are tiny, but there's no reason to double the
    round-trips.

    A visited-set threaded down each root-to-node path guards against a
    backwards edge (two quests serving each other, or a longer cycle)
    turning into infinite recursion or a quest listing itself as its own
    server (gripe 161912) — a child id already on the current path is
    simply dropped from that branch.
    """
    refs = store.list_refs(kind="quest", order_by="updated_desc", limit=500)
    live_ids = {r.id for r in refs}
    ref_by_id = {r.id: r for r in refs}
    statuses = {r.id: _quest_status_from_tags(store.tags_for(r.id)) for r in refs}

    children: dict[int, list[int]] = {}
    has_parent: set[int] = set()
    for r in refs:
        links = store.links_for(r.id, direction="in", relation="serves")
        kids = sorted({ln.src_ref_id for ln in links} & live_ids)
        if kids:
            children[r.id] = kids
            has_parent.update(kids)

    def sort_key(qid: int) -> tuple[int, float, float]:
        r = ref_by_id[qid]
        prio = getattr(r, "prio", None)
        updated = getattr(r, "updated_at", None)
        return (
            0 if statuses[qid] == "active" else 1,
            float(prio) if prio is not None else float("inf"),
            -(updated.timestamp() if updated else 0.0),
        )

    rendered: set[int] = set()

    def build_node(qid: int, ancestors: frozenset[int]) -> dict[str, Any]:
        r = ref_by_id[qid]
        rendered.add(qid)
        path = ancestors | {qid}
        kid_ids = sorted(
            (k for k in children.get(qid, []) if k not in path), key=sort_key
        )
        updated = getattr(r, "updated_at", None)
        return {
            "id": qid,
            "headline": _quest_headline(r.title, qid),
            "status": statuses[qid],
            "prio": getattr(r, "prio", None),
            "updated": updated.strftime("%Y-%m-%d %H:%M") if updated else "",
            "children": [build_node(k, path) for k in kid_ids],
        }

    root_ids = sorted((r.id for r in refs if r.id not in has_parent), key=sort_key)
    forest = [build_node(rid, frozenset()) for rid in root_ids]
    # A cycle with no outside parent (every member is in `has_parent`, so
    # none qualifies as a root) would otherwise vanish from the page —
    # promote its members to fallback roots so bad data stays visible.
    while orphaned := sorted(live_ids - rendered, key=sort_key):
        forest.append(build_node(orphaned[0], frozenset()))

    return templates.TemplateResponse(
        request,
        "refs/quest_index.html.j2",
        {
            "active_tab": "refs:quest",
            "kind_label": _REF_KIND_LABEL.get("quest", "Quest"),
            "forest": forest,
        },
    )


#: Per-kind lists folded into a Drive kind-facet preset (WS1b decision
#: D2) — Oracle's "roll the dice" mint-a-new-reading affordance and
#: Patents' OPS remote-search live in the MCP/CLI surface, not a web
#: route, so there's no standalone UI feature to keep here; only the
#: *list* retires. The detail readers (below) are unaffected.
_FOLDED_TO_DRIVE: frozenset[str] = frozenset({"oracle", "patent"})


@router.get("/{kind}", response_class=HTMLResponse, response_model=None)
async def index(
    request: Request,
    kind: str,
    q: str | None = None,
    tag: str | None = None,
    since: str = "any",
    sort: str = "updated_desc",
    page: int = 1,
) -> HTMLResponse | RedirectResponse:
    """List / search one ref kind with date + tag filters and sort."""
    if kind in _FOLDED_TO_DRIVE:
        params: list[tuple[str, str]] = [("k", kind), ("submitted", "1")]
        if q and q.strip():
            params.append(("q", q.strip()))
        return RedirectResponse(url="/drive?" + urlencode(params))
    _require_kind(kind)
    store = get_store(request)

    # Quests get a dedicated tree view (the `serves` DAG) instead of the
    # generic flat list — see `_quest_index`.
    if kind == "quest":
        return await _quest_index(request, store)

    tags = _parse_tags(tag)
    since = since if since in _DATE_DELTA else "any"
    sort = sort if sort in _SORT_KEYS else "updated_desc"
    page = max(1, page)
    offset = (page - 1) * _PAGE_SIZE

    query = (q or "").strip()
    if query:
        # Ranked title search; date / sort don't apply to a relevance
        # ordering, so they're shown but inert while a query is active.
        hits = store.search_refs_lexical(
            q=query, kind=kind, tags=tags or None, limit=_PAGE_SIZE
        )
        refs = [ref for ref, _score in hits]
        has_next = False
    else:
        updated_after: datetime | None = None
        delta = _DATE_DELTA.get(since)
        if delta is not None:
            updated_after = datetime.now(UTC) - delta
        refs = store.list_refs(
            kind=kind,
            tags=tags or None,
            updated_after=updated_after,
            order_by=sort,
            limit=_PAGE_SIZE + 1,  # one extra row probes "has next page"
            offset=offset,
        )
        has_next = len(refs) > _PAGE_SIZE
        refs = refs[:_PAGE_SIZE]

    return templates.TemplateResponse(
        request,
        "refs/index.html.j2",
        {
            "active_tab": f"refs:{kind}",
            "kind": kind,
            "kind_label": _REF_KIND_LABEL.get(kind, kind.replace("-", " ").title()),
            "rows": [_row(r) for r in refs],
            "q": query,
            "tag": tag or "",
            "since": since,
            "sort": sort,
            "page": page,
            "has_next": has_next,
            "date_presets": [(k, label) for k, label, _ in DATE_PRESETS],
            "sort_choices": SORT_CHOICES,
        },
    )


@router.get("/{kind}/{ref_id}", response_class=HTMLResponse, response_model=None)
async def detail(
    request: Request, kind: str, ref_id: int
) -> HTMLResponse | RedirectResponse:
    """Read-only detail: the handler's own ``get`` output for this ref."""
    _require_kind(kind)
    store = get_store(request)
    refs = store.fetch_refs_by_ids([ref_id], include_deleted=False)
    ref = refs.get(ref_id)
    if ref is None or ref.kind != kind:
        # Distinguish "deleted" from "never existed": a soft-deleted ref (e.g.
        # a pathway's candidate structure that was later removed) is a dangling
        # link, not a typo. Render a tombstone with an Undelete affordance
        # (404, not the PrecisError->400 a bare NotFound would give) so the
        # link resolves to something actionable rather than a raw error.
        deleted = store.fetch_refs_by_ids([ref_id], include_deleted=True).get(ref_id)
        if deleted is not None and deleted.kind == kind:
            return templates.TemplateResponse(
                request,
                "refs/tombstone.html.j2",
                {
                    "active_tab": f"refs:{kind}",
                    "kind": kind,
                    "kind_label": _REF_KIND_LABEL.get(
                        kind, kind.replace("-", " ").title()
                    ),
                    "ref": _row(deleted),
                },
                status_code=404,
            )
        raise NotFound(f"{kind} id={ref_id} not found")

    # Structures have a dedicated interactive 3D viewer at /structure/{slug};
    # the generic handler-card render is just ASCII. Send humans to the viewer.
    if kind == "structure" and ref.slug:
        return RedirectResponse(url=f"/structure/{ref.slug}", status_code=303)

    # A finding that is a live TAPROOT:claim hub has ONE canonical view — the
    # rich /claim/<head> evidence page (originators/corroborators/grounding/
    # "Used by"). The generic handler-card render here is the legacy duplicate;
    # redirect so every /refs/finding/<id> link (item_view rows, chunk-
    # connection tables, bare [fi<id>] anchors outside the claims-reader) lands
    # on the same page as the smartdraft ◆ diamond — one view, not two. The
    # ~12% of findings that AREN'T hubs (citation-pending markers, quality
    # checks) have no claim page and keep the generic detail below. Temporary
    # (307), never cached: a finding's hub status can change (tag/untag).
    if kind == "finding" and is_claim_hub(store, ref.id):
        head = handle_registry.format_handle("finding", ref.id)
        return RedirectResponse(url=f"/claim/{head}", status_code=307)

    # Conversations render as a human-readable chat transcript (one
    # turn per body chunk) rather than the handler's agent-facing
    # overview card — a person clicking a thread wants the turns, not
    # the `Next:` call affordances meant for the LLM.
    if kind == "conv":
        # Follow-up threads stamp the source handle in ref.meta so the
        # transcript can offer a "continue this discussion" box that
        # routes the next question back to the same source.
        conv_meta = ref.meta or {}
        followup_source = conv_meta.get("followup_source")
        return templates.TemplateResponse(
            request,
            "refs/conv_detail.html.j2",
            {
                "active_tab": f"refs:{kind}",
                "kind": kind,
                "kind_label": _REF_KIND_LABEL.get(kind, kind.replace("-", " ").title()),
                "ref": _row(ref),
                "turns": _conv_turns(store, ref.id),
                "followup_source": followup_source,
                "followup_source_url": (
                    _source_detail_url(
                        str(conv_meta.get("followup_kind") or ""),
                        conv_meta.get("followup_ref_id"),
                    )
                    if followup_source
                    else None
                ),
            },
        )

    # Quests render as a hub dashboard — header (status/prio/momentum/
    # tote) + links to the dossier/paper/frontier/gaps + a happening-now
    # callout + logbook tail + servers-lite — rather than the handler's
    # bare striving+logbook card the generic template renders for every
    # other numeric-ref kind. See ``_quest_detail``.
    if kind == "quest":
        return await _quest_detail(request, store, ref)

    # Pathways render as a dedicated candidate→structures→energetics
    # page rather than the generic handler-card render — see
    # ``_pathway_detail``. ``pathway`` is an EXTERNAL plugin kind (the
    # autocatpath bridge) so this never dispatches the handler's own
    # ``get()``; everything comes off the stored ref meta + chunk.
    if kind == "pathway":
        return await _pathway_detail(request, store, ref)

    # Slug kinds (oracle/patent/pres) address get() by slug; numeric
    # kinds (memory/gripe) by id. Prefer the slug when present.
    handle: str | int = ref.slug if ref.slug else ref.id
    get_args: dict[str, Any] = {"kind": kind, "id": handle}
    # This detail page is a read-only view. For cache-backed kinds a plain
    # get() re-fetches on a cache miss — and addressing by slug reliably
    # misses for query-keyed kinds (perplexity/websearch), so a page view
    # would re-run the paid upstream call. no_fetch=True serves the stored
    # body and never spends.
    if kind in _CACHE_BACKED_KINDS:
        get_args["no_fetch"] = True
    body, is_error = await await_dispatch(request, "get", get_args)

    # Disabled-but-cached fallback: when the handler is currently
    # registered-but-disabled (math without WOLFRAM_APP_ID, web without
    # outbound HTTP, etc.) but the ref already exists with cached body
    # chunks, render the cached body directly rather than showing the
    # operator a wall of "[error:Unsupported]". The cache is still
    # valuable even when fresh fetches can't run — that's why we keep
    # it. Tag the response so the template can show a quiet banner.
    body_disabled_notice: str | None = None
    if is_error and "disabled in this build" in (body or ""):
        cached_chunks = list(store.chunks.list_chunks_for_ref(ref.id))
        if cached_chunks:
            cached_text = "\n\n".join(
                (b.text or "").strip() for b in cached_chunks if b.text
            )
            if cached_text:
                body = cached_text
                is_error = False
                body_disabled_notice = (
                    f"kind {kind!r} is currently disabled in this build; "
                    "showing the cached body. Fresh fetches will resume "
                    "once the required env (e.g. WOLFRAM_APP_ID) is set."
                )

    # Patent body text lives in body chunks; the handler's overview
    # only renders the bibliographic header + abstract excerpt. Pull
    # the chunks so the detail view can show the full text (description
    # + claims) as one row per chunk — what's actually in the corpus.
    chunks: list[dict[str, Any]] = []
    if kind == "patent":
        for b in store.chunks.list_chunks_for_ref(ref.id):
            chunks.append(
                {
                    "pos": b.ord,
                    "chunk_kind": getattr(b, "chunk_kind", "paragraph"),
                    "slug": b.slug or "",
                    "text": b.text or "",
                }
            )

    # Tag editor — every browsable kind gets the same chip strip.
    # Closed-vocab tags (STATUS:*, LLM:*, DREAM:*) appear but the
    # template doesn't offer a × on them; per-ref removal of a
    # structural tag goes through the standard tag() verb explicitly.
    raw_tags = store.tags_for(ref.id)
    tags = _tag_chips(raw_tags)

    # References panel (MVP — memory views only, where dreams live).
    # Walk the body for ref handles, resolve each, build a list to
    # render below the body. Cheap reads — at most ~20 handles per
    # memory typical, batched into ``fetch_refs_by_ids``.
    references: list[dict[str, Any]] = []
    footnotes: dict[tuple[str, str, str | None], int] | None = None
    if kind == "memory" and not is_error and body:
        handles = _extract_handles(body)
        handle_to_num: dict[tuple[str, str, str | None], int] = {}
        for n, (ref_kind, ref_ident, chunk) in enumerate(handles, 1):
            handle_to_num[(ref_kind, ref_ident, chunk)] = n
            row = _expand_handle(store, ref_kind, ref_ident, chunk)
            row["number"] = n
            references.append(row)
        # Hand the numbering to ``linkify_refs`` (via the template) so the
        # inline ``[N]`` footnote markers are emitted *inside* its escaping
        # pass — appended right after each handle's hover anchor. The body
        # stays plain text; we no longer splice raw ``<a>`` HTML into it
        # (which autoescape would now neutralise into visible markup).
        footnotes = handle_to_num

    # Job detail gets an actions strip instead of the dream-memory
    # "Ask & think" box: a failed/cancelled job is a thing you *unstick*
    # (retry → re-mint) or *read* (transcript / parent intent), not a
    # thought you interrogate. The generic Discussion affordance ran a
    # slow agentic pass whose answer landed in a side conv thread — no
    # help for "what went wrong / how do I fix it". See
    # ``templates/refs/detail.html.j2``.
    job_actions: dict[str, Any] | None = None
    if kind == "job":
        job_actions = _job_actions(store, ref, raw_tags)

    # YouTube detail pages get a header card with a clickable watch link
    # and the video thumbnail (a "screenshot") above the transcript.
    youtube_meta = _youtube_meta(store, ref) if kind == "youtube" else None

    return templates.TemplateResponse(
        request,
        "refs/detail.html.j2",
        {
            "active_tab": f"refs:{kind}",
            "kind": kind,
            "kind_label": _REF_KIND_LABEL.get(kind, kind.replace("-", " ").title()),
            "ref": _row(ref),
            "body": body,
            "footnotes": footnotes,
            "is_error": is_error,
            "chunks": chunks,
            "tags": tags,
            "body_disabled_notice": body_disabled_notice,
            "references": references,
            "job_actions": job_actions,
            "youtube_meta": youtube_meta,
            # The generic "Ask & think" Discussion box is a dream-memory
            # affordance; a job wants the actions strip, not an agentic
            # side-thread. Suppress it for jobs.
            "discussions": (
                None if kind == "job" else _followup_discussions(store, ref.id)
            ),
        },
    )


def _split_tag_input(raw: str) -> list[str]:
    """Split a comma/space-separated tag input into a clean list."""
    if not raw:
        return []
    parts = [p.strip() for chunk in raw.split(",") for p in chunk.split()]
    return [p for p in parts if p]


@router.post("/{kind}/{ref_id}/tags")
async def edit_tags(
    request: Request,
    kind: str,
    ref_id: int,
    add: str = Form(""),
    remove: str = Form(""),
) -> Response:
    """Add or remove tags on a browsable ref via the ``tag`` verb.

    Same shape as ``/tasks/{id}/tags`` — ``add`` is a comma/space-
    separated string the operator typed; ``remove`` is a single
    ``namespace:value`` from a chip's × button. Both flow through
    the handler so tag-vocabulary validation stays single-sourced.
    """
    _require_kind(kind)
    add_list = _split_tag_input(add)
    remove_list = _split_tag_input(remove)
    redirect_url = f"/refs/{kind}/{ref_id}"
    if not add_list and not remove_list:
        return RedirectResponse(url=redirect_url, status_code=303)
    args: dict[str, Any] = {"kind": kind, "id": ref_id}
    if add_list:
        args["add"] = add_list
    if remove_list:
        args["remove"] = remove_list
    return await redirect_or_error(
        request, "tag", args, redirect=redirect_url, error_title="Tag error"
    )


@router.post("/{kind}/{ref_id}/undelete")
async def undelete(request: Request, kind: str, ref_id: int) -> Response:
    """Restore a soft-deleted ref — the Undelete button on the tombstone page.

    Clears ``deleted_at`` (idempotent — a no-op if the ref is already live)
    and redirects back to the detail URL, which now resolves: a restored
    structure 303-hops on to its ``/structure/{slug}`` viewer.
    """
    _require_kind(kind)
    store = get_store(request)
    # Restore only a ref that actually IS this kind — ``restore_ref`` keys on
    # ref_id alone, so without this guard ``/refs/<any-kind>/<id>/undelete``
    # would resurrect whatever <id> is, kind-unchecked. Mirrors the tombstone
    # render's own ``deleted.kind == kind`` gate.
    ref = store.fetch_refs_by_ids([ref_id], include_deleted=True).get(ref_id)
    if ref is None or ref.kind != kind:
        raise NotFound(f"{kind} id={ref_id} not found")
    store.restore_ref(ref_id)
    return RedirectResponse(url=f"/refs/{kind}/{ref_id}", status_code=303)


# ---- Ask a follow-up question about a thought -----------------------
#
# A textbox + button on each detail page. The question is captured as
# a turn in a ``conv`` thread (one per source[, chunk]); an agentic
# ``claude -p`` pass (the dreaming dispatch — SOUL prompt + MCP precis
# tools) answers, and the answer is appended as the next turn. The conv
# is linked ``derived-from`` the source so the discussion is reachable
# from the thought. All DB writes go through the put / link verbs.


def _source_detail_url(kind: str, ref_id: Any) -> str:
    """Best detail URL for a source ref (papers have their own viewer)."""
    if ref_id is None:
        return "/refs"
    if kind == "paper":
        return f"/papers/{ref_id}"
    return f"/refs/{kind}/{ref_id}"


async def _run_followup(
    request: Request,
    *,
    source_kind: str,
    source_ref_id: int,
    chunk_pos: int | None,
    question: str,
) -> Response:
    """Capture a question, think about it, append the answer to a conv.

    Shared by the source-page ``/ask`` route and the conv-page
    ``/continue`` route — both resolve to the same conv slug, so a
    discussion accumulates turns in one thread.
    """
    question = (question or "").strip()
    store = get_store(request)
    refs = store.fetch_refs_by_ids([source_ref_id], include_deleted=False)
    source = refs.get(source_ref_id)
    if source is None or source.kind != source_kind:
        raise NotFound(f"{source_kind} id={source_ref_id} not found")

    back_url = _source_detail_url(source_kind, source_ref_id)
    if not question:
        return RedirectResponse(url=back_url, status_code=303)

    slug = ask.followup_slug(source_kind, source_ref_id, chunk_pos)
    handle = ask.source_handle(source_kind, source_ref_id, chunk_pos)
    source_title = (source.title or "").split("\n", 1)[0][:120] or handle
    asker = get_web_config(request).owner

    def _err(title: str, detail: str) -> Response:
        return templates.TemplateResponse(
            request,
            "error.html.j2",
            {"title": title, "detail": detail, "status": 400},
            status_code=400,
        )

    # 1. Append the human question (mints the thread on first ask,
    #    stamping the source handle in ref_meta for the continue box).
    _, is_error = await await_dispatch(
        request,
        "put",
        {
            "kind": "conv",
            "id": slug,
            "text": question,
            "author": asker,
            "title": f"Follow-up · {source_title}",
            "ref_meta": {
                "followup_source": handle,
                "followup_kind": source_kind,
                "followup_ref_id": source_ref_id,
                "followup_chunk": chunk_pos,
            },
        },
    )
    if is_error:
        return _err("Follow-up error", "could not record the question")

    conv = store.get_ref(kind="conv", id=slug)
    if conv is None:
        return _err("Follow-up error", "conv thread missing after put")
    conv_url = f"/refs/conv/{conv.id}"

    # 2. Link the discussion back to its source (idempotent — re-running
    #    is a no-op via the links unique tuple). Chunk-scoped via ~N.
    await await_dispatch(
        request,
        "link",
        {"kind": "conv", "id": slug, "target": handle, "rel": "derived-from"},
    )

    # 3. Build the prompt: source body + chunk-in-focus + the discussion
    #    so far (every turn except the question we just appended).
    src_handle: str | int = source.slug if source.slug else source.id
    src_body, src_err = await await_dispatch(
        request, "get", {"kind": source_kind, "id": src_handle}
    )
    if src_err:
        src_body = source.title or ""
    focus_text: str | None = None
    if chunk_pos is not None:
        for b in store.chunks.list_chunks_for_ref(
            source_ref_id, pos_range=(chunk_pos, chunk_pos)
        ):
            focus_text = b.text or ""
            break
    all_turns = store.chunks.list_chunks_for_ref(conv.id)
    prior = [
        ((b.meta or {}).get("author") or "?", b.text or "") for b in all_turns[:-1]
    ]
    prompt = ask.build_prompt(
        source_kind=source_kind,
        source_handle_str=handle,
        source_title=source_title,
        source_body=src_body,
        focus_text=focus_text,
        prior_turns=prior,
        question=question,
    )

    # 4. Think. The subprocess can take tens of seconds — run it off the
    #    event loop so concurrent tabs / healthz stay responsive.
    answer_author = ask.ANSWERER
    answer_meta: dict[str, Any] = {}
    try:
        result = await asyncio.to_thread(
            ask.generate_answer, prompt, store=store, conv_ref_id=conv.id
        )
        answer = (result.final_text or "").strip() or "(the model returned no text)"
        answer_meta = {
            k: v
            for k, v in {
                "model": getattr(result, "model", None),
                "cost_usd": result.cost_usd,
                "duration_s": round(result.duration_s, 1),
                "turns": result.turns_used,
            }.items()
            if v is not None
        }
    except ClaudeAgentError as exc:
        answer = f"⚠️ thinking failed: {exc}"
        answer_author = "system"

    # 5. Append the answer as the next turn, then land on the transcript.
    await await_dispatch(
        request,
        "put",
        {
            "kind": "conv",
            "id": slug,
            "text": answer,
            "author": answer_author,
            "meta": answer_meta,
        },
    )
    return RedirectResponse(url=conv_url, status_code=303)


@router.post("/{kind}/{ref_id}/ask")
async def ask_followup(
    request: Request,
    kind: str,
    ref_id: int,
    question: str = Form(""),
    chunk: str = Form(""),
) -> Response:
    """Ask a follow-up about a ref (or a specific chunk via ``chunk=N``)."""
    _require_kind(kind)
    chunk_pos: int | None = None
    if chunk.strip():
        try:
            chunk_pos = int(chunk.strip())
        except ValueError:
            chunk_pos = None
    return await _run_followup(
        request,
        source_kind=kind,
        source_ref_id=ref_id,
        chunk_pos=chunk_pos,
        question=question,
    )


@router.post("/conv/{conv_ref_id}/continue")
async def continue_followup(
    request: Request,
    conv_ref_id: int,
    question: str = Form(""),
) -> Response:
    """Continue a follow-up thread — resolve its source from ref.meta."""
    store = get_store(request)
    refs = store.fetch_refs_by_ids([conv_ref_id], include_deleted=False)
    conv = refs.get(conv_ref_id)
    if conv is None or conv.kind != "conv":
        raise NotFound(f"conv id={conv_ref_id} not found")
    meta = conv.meta or {}
    source_kind = meta.get("followup_kind")
    source_ref_id = meta.get("followup_ref_id")
    if not source_kind or source_ref_id is None:
        raise NotFound(
            f"conv id={conv_ref_id} is not a follow-up thread (no source in meta)"
        )
    chunk_raw = meta.get("followup_chunk")
    chunk_pos = int(chunk_raw) if isinstance(chunk_raw, int) else None
    return await _run_followup(
        request,
        source_kind=str(source_kind),
        source_ref_id=int(source_ref_id),
        chunk_pos=chunk_pos,
        question=question,
    )
