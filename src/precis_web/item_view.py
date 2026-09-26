"""Per-kind item presenter — the ``ItemPresenter`` contract behind the
unified Drive surface's cross-kind rows.

One presenter renders a cross-kind search hit into a row view-model for
the unified ``/items`` list: a name, the matching-chunk preview, a
richer hover peek, an optional thumbnail, kind-specific actions, and the
click-through URL. The default covers *every* kind through the generic
``/refs/<kind>/<id>`` detail route; a kind with a richer reader overrides
``open_url`` via :data:`_OPEN_URL_OVERRIDES`.

The full method contract from the proposal is now present
(``name``/``open_url``/``preview``/``title_meta``/``chunk_full``/
``thumbnail``/``state``/``actions``/``links``), each with a generic default so every
kind renders without a subclass. **Not yet promoted to
``@abstractmethod``** — that check-time-totality guarantee (per the
proposal's decisions log) requires a dedicated presenter for every
source/artifact kind, which is a separate per-kind pass (tracked in
``OPEN-ITEMS.md``), not a mechanical follow-on to this module. A kind
needing a richer peek registers a subclass in
:data:`_PRESENTER_CLASSES` — seeded with ``youtube``'s thumbnail, then
grown with one presenter per design/artifact kind (``se``/``structure``/
``pcb``/``component``/``material``/``figure``/``mermaid``) so retiring
their standalone list routes (``/se``, ``/structure``, ``/pcb``, …) into
Drive chips loses no information a row can cheaply carry — "cheaply"
meaning off ``ref.meta`` or a lookup batched once per page by the caller
(``routes/items.py``), never a per-row query or file read.
"""

from __future__ import annotations

import re
from typing import Any

from precis.utils.authors import author_names
from precis_web.paper_links import (
    arxiv_pdf_url,
    doi_url,
    libkey_url,
    scholar_url,
    uol_url,
)
from precis_web.timefmt import utc_date

#: Max characters of the matching chunk shown as the row preview.
_PREVIEW_CHARS = 140

#: Max characters of the richer hover-popover peek.
_HOVER_CHARS = 600

_WS_RE = re.compile(r"\s+")

#: Max characters of a title shown in a list / Drive row. Generous
#: enough that a normal title (paper, memory heading, gripe subject)
#: passes through untouched — truncation only bites the kinds whose
#: title *is* the body (websearch query, citation claim, digest bodies),
#: exactly where a Drive row would otherwise blow out. Detail pages never
#: use this; they render ``ref.title`` in full.
DISPLAY_TITLE_LIMIT = 160


def display_title(title: str | None, *, limit: int = DISPLAY_TITLE_LIMIT) -> str:
    """Single-line, length-capped label for a ref in list / Drive views.

    Storage keeps the whole title (the original query / claim / heading);
    this is the *display* side of that split. Internal newlines — a title
    that is really a whole document body — collapse to one line, then the
    result is truncated to ``limit`` with an ellipsis. Returns ``""`` for
    an empty title so each caller supplies its own fallback label.

    Escaping is left to the template (Jinja autoescape), same as every
    other title field on these pages — the return is plain text.
    """
    one_line = _WS_RE.sub(" ", title or "").strip()
    if len(one_line) > limit:
        one_line = one_line[: limit - 1].rstrip() + "…"
    return one_line


#: Kinds with a richer detail view than the generic ``/refs`` browser.
#: ``{id}`` / ``{slug}`` are filled from the ref. Every other kind falls
#: back to ``/refs/<kind>/<id>`` (which exists for all kinds), so the map
#: only needs the exceptions — grow it as kinds gain dedicated readers.
_OPEN_URL_OVERRIDES: dict[str, str] = {
    "paper": "/papers/{id}",
    "draft": "/smartdraft/{id}",
    "datasheet": "/datasheets/{id}",
    "cad": "/cad/{slug}",
    "se": "/se/{slug}",
    "structure": "/structure/{slug}",
    "figure": "/figure/{slug}",
    "mermaid": "/mermaid/{slug}",
    # Work-facet rows (Drive's "Work" chip row): a quest opens its hub
    # dashboard, a todo drills into just its own subtree on /todo (never
    # the full 5000-row tree). Mirrors the folder-child map in
    # ``routes/drive.py`` (``_READER_URL``) so both row builders agree.
    "quest": "/refs/quest/{id}",
    "todo": "/todo?focus={id}",
}

#: Kinds whose ingest runs a fetch→PDF→chunk pipeline, so the
#: stub-vs-ingested distinction is meaningful. Other kinds (web,
#: wikipedia, perplexity, …) are always chunked on arrival — a "chunks"
#: badge on every row would be noise, so state markers are scoped here.
_PIPELINE_KINDS: frozenset[str] = frozenset(
    {"paper", "patent", "datasheet", "cfp", "pres"}
)

#: Badge color per ``stub_rank`` Tier-2 LLM band label (``refs.meta.
#: llm_label`` — see ``workers/stub_rank.py``). ``core`` is the warmest
#: (directly serves a current interest), ``off`` the coldest (unrelated).
_LLM_LABEL_BADGE_CLS: dict[str, str] = {
    "core": "bg-emerald-100 text-emerald-700",
    "adjacent": "bg-sky-100 text-sky-700",
    "explore": "bg-amber-100 text-amber-700",
    "off": "bg-slate-200 text-slate-500",
}

#: Badge color per `pathway` ``meta.status`` — the Drive row's own compute-
#: status badge (mirrors the detail page's header chip,
#: ``refs/pathway_detail.html.j2``). A status not in this map (legacy row
#: predating ``meta.status``) gets no badge.
_PATHWAY_STATUS_BADGE_CLS: dict[str, str] = {
    "ready": "bg-emerald-100 text-emerald-700",
    "computing": "bg-amber-100 text-amber-700",
    "failed": "bg-rose-100 text-rose-700",
    "superseded": "bg-slate-200 text-slate-500",
}

#: Namespaces hidden from the per-row tag chips — machine/control tags
#: the operator doesn't browse by.
_TAG_HIDE_NS: frozenset[str] = frozenset(
    {"STATUS", "DREAM", "PRIO", "SRC", "CACHE", "EMBED", "LLM", "ROLE3", "CLASSIFY"}
)

#: Flag values shown as toggle buttons, not repeated as tag chips.
_TAG_HIDE_VALUES: frozenset[str] = frozenset({"read-later", "must-read", "skim"})


def _display_tags(raw: list[tuple[str, str]] | None) -> list[dict[str, str]]:
    """Per-row tag chips: what this item was tagged with, minus the
    machine namespaces and the reading-intent flags (which have buttons).
    ``OPEN`` tags render bare; others as ``namespace:value``; each links
    to the ``/tags/refs`` pivot.
    """
    out: list[dict[str, str]] = []
    for ns, val in raw or []:
        if ns in _TAG_HIDE_NS:
            continue
        if ns == "OPEN" and val in _TAG_HIDE_VALUES:
            continue
        label = val if ns == "OPEN" else f"{ns}:{val}"
        out.append({"label": label, "href": f"/tags/refs?namespace={ns}&value={val}"})
    return out


class ItemPresenter:
    """Default renderer for one kind's search hit → row view-model."""

    def __init__(self, kind: str) -> None:
        self.kind = kind

    def name(self, ref: Any) -> str:
        return display_title(getattr(ref, "title", None)) or (
            f"{self.kind} #{getattr(ref, 'id', '?')}"
        )

    def open_url(self, ref: Any) -> str:
        tmpl = _OPEN_URL_OVERRIDES.get(self.kind)
        if tmpl:
            return tmpl.format(
                id=getattr(ref, "id", ""),
                slug=getattr(ref, "slug", None) or getattr(ref, "id", ""),
            )
        return f"/refs/{self.kind}/{getattr(ref, 'id', '')}"

    def preview(self, block: Any, summary: str | None = None) -> str:
        """Row preview text: the matching chunk's ``llm-v1`` gloss when
        one was batched for this hit (``summary``), else the truncated
        chunk text itself. Both share the same length-cap rule."""
        gloss = (summary or "").strip()
        text = gloss if gloss else (getattr(block, "text", None) or "").strip()
        if len(text) <= _PREVIEW_CHARS:
            return text
        return text[: _PREVIEW_CHARS - 1].rstrip() + "…"

    def title_meta(self, ref: Any) -> dict[str, Any]:
        """Full (uncapped) title + journal/authors/year for the
        title-hover popover, plus the Meta tab's human-verification stamp
        (``refs.human_verified_at``/``_by``) — the ``/drive`` search/browse
        row's ✓ mark (gr351830), same columns every other paper-link
        surface reads (``ref`` here is the search hit's full batched
        ``refs`` row already; no extra query)."""
        meta = getattr(ref, "meta", None) or {}
        return {
            "title": _WS_RE.sub(" ", getattr(ref, "title", None) or "").strip(),
            "journal": (meta.get("journal") or "").strip() or None,
            "authors": author_names(getattr(ref, "authors", None)),
            "year": getattr(ref, "year", None),
            "reviewed_at": utc_date(getattr(ref, "human_verified_at", None)) or None,
            "reviewed_by": getattr(ref, "human_verified_by", None) or None,
        }

    def chunk_full(self, block: Any) -> str:
        """The fuller matching-chunk text for the chunk-hover popover —
        chunk only, no abstract."""
        text = (getattr(block, "text", None) or "").strip()
        if len(text) <= _HOVER_CHARS:
            return text
        return text[: _HOVER_CHARS - 1].rstrip() + "…"

    def thumbnail(self, ref: Any) -> str | None:
        """Cached-still image URL, or ``None`` when there isn't one.

        Visual-kind thumbnails (structure/cad/pcb) are a deferred render
        + cache pass (see the proposal's open question); the default is
        always ``None`` here. A kind that already has a cheap image (e.g.
        the youtube per-video screenshot) overrides this."""
        return None

    def actions(self, ref: Any) -> list[dict[str, str]]:
        """Universal per-row quick actions — move-to-folder, delete/unfile,
        tag — rendered on every ``/drive`` row (WS1a). Kind-specific
        actions beyond these (e.g. papers-needed's "re-chase stub", cad's
        "apply proposal") still have this seam to extend without leaking
        back onto a bespoke page; a subclass overriding this should
        ``return [*super().actions(ref), {...}]`` to keep the universal set.

        Each action is a small dict the ``/drive`` row template renders as
        an inline form posting to ``routes/drive.py``'s generic per-ref
        write routes (``/drive/move``, ``/drive/item/<kind>/<id>/delete``,
        ``/drive/item/<kind>/<id>/tag``) — every write still rides the
        seven-verb dispatch, no direct SQL. A handler that doesn't support
        a verb (e.g. a kind with no ``delete``) surfaces the rejection via
        the same ``redirect_or_error`` error page every other write route
        uses — a clean stop, not a crash.
        """
        ref_id = getattr(ref, "id", None)
        if ref_id is None:
            return []
        ident = getattr(ref, "slug", None) or str(ref_id)
        return [
            {"type": "move", "kind": self.kind, "id": ident, "label": "move"},
            {"type": "delete", "kind": self.kind, "id": ident, "label": "delete"},
            {"type": "tag", "kind": self.kind, "id": ident, "label": "tag"},
        ]

    def state(
        self,
        ref: Any,
        *,
        has_chunks: bool,
        design: dict[str, Any] | None = None,
    ) -> list[dict[str, str]]:
        """Pipeline-state badges for the row (paper-family kinds only, plus
        `pathway`'s own compute-status badge).

        ``stub`` — a corpus doc still awaiting the fetcher (no PDF yet);
        ``chunks`` — ingested, has body chunks (searchable); a fourth
        badge (the ``stub_rank`` pass's Tier-2 LLM band label — see
        ``workers/stub_rank.py``) appears when ``ref.meta.llm_label`` is
        set, regardless of the stub/chunks state. Mirrors the Papers-tab
        vocabulary. Non-pipeline, non-`pathway` kinds get no badges.

        `pathway`'s badge reads straight off ``ref.meta.status`` — already
        loaded for every Drive row (``routes/drive.py``'s ``_CHILD_COLS`` /
        the search-hit ``ref``), so this is free: no extra per-row query.
        Surfaces the same ``computing``/``failed``/``superseded`` states the
        detail page's banner does (the orphaned-pathway-stub sweep,
        :func:`precis.quest.loop._reconcile_orphaned_pathways`).

        ``design`` is this ref's slice of a batched per-page lookup
        (``routes/items.py``'s ``_design_facts_bulk``) — ``None`` for every
        kind but ``se``/``structure``/``component``, which override this
        method to render it; the base implementation ignores it.
        """
        if self.kind == "pathway":
            status = (getattr(ref, "meta", None) or {}).get("status")
            if not isinstance(status, str) or status not in _PATHWAY_STATUS_BADGE_CLS:
                return []
            cls = _PATHWAY_STATUS_BADGE_CLS[status]
            return [{"label": status, "cls": cls, "title": f"pathway status: {status}"}]
        if self.kind not in _PIPELINE_KINDS:
            return []
        badges: list[dict[str, str]] = []
        if getattr(ref, "pdf_sha256", None) is None and not has_chunks:
            badges.append(
                {
                    "label": "stub",
                    "cls": "bg-slate-200 text-slate-500",
                    "title": "awaiting fetch — no PDF yet",
                }
            )
        if getattr(ref, "pdf_sha256", None) is not None:
            badges.append(
                {
                    "label": "pdf",
                    "cls": "bg-emerald-100 text-emerald-700",
                    "title": "PDF stored",
                }
            )
        if has_chunks:
            badges.append(
                {
                    "label": "chunks",
                    "cls": "bg-sky-100 text-sky-700",
                    "title": "ingested — has body chunks",
                }
            )
        meta = getattr(ref, "meta", None) or {}
        llm_label = meta.get("llm_label")
        if llm_label in _LLM_LABEL_BADGE_CLS:
            badges.append(
                {
                    "label": llm_label,
                    "cls": _LLM_LABEL_BADGE_CLS[llm_label],
                    "title": meta.get("llm_reason") or llm_label,
                }
            )
        return badges

    def links(self, identifier: str | None) -> list[dict[str, Any]]:
        """Off-site "go find/get it" links from a paper's external
        identifier. Two tiers, in walk order:

        * **download** (``download=True``) — a one-click full-text PDF: the
          LibKey library link for a DOI (skips the Primo keyword-search
          hop, resolving straight to the full-text-file), and the arXiv PDF
          for a preprint. The Drive row marks these ``data-download`` so the
          "Open all downloads" button walks exactly this set.
        * **search** — the publisher/arXiv abstract page, the Primo
          discovery search, and Google Scholar: where to *find* a copy when
          there's no direct PDF.

        Empty when there's no identifier (non-paper rows). Only DOIs get a
        LibKey link and only ``arxiv:`` ids get an arXiv PDF, so a given row
        carries at most one download tier."""
        if not identifier:
            return []
        out: list[dict[str, Any]] = []
        pub = doi_url(identifier)
        if pub:
            is_arxiv = identifier.startswith("arxiv:")
            out.append(
                {
                    "label": "arXiv" if is_arxiv else "DOI",
                    "href": pub,
                }
            )
            # Copy-to-clipboard entry (``clip``, no href) — the bare id for
            # pasting into a library/ILL search, right beside the DOI link.
            # Key is ``clip``, NOT ``copy``: Jinja's ``l.copy`` resolves the
            # dict *method* (truthy on every link), which turned every find:
            # link into a copy button. Strip only the known scheme prefixes:
            # a DOI itself may legally contain ``:``.
            bare = identifier
            for prefix in ("doi:", "arxiv:"):
                if bare.startswith(prefix):
                    bare = bare[len(prefix) :]
                    break
            out.append(
                {
                    "label": "⧉",
                    "clip": bare,
                    "title": f"copy {'arXiv id' if is_arxiv else 'DOI'}: {bare}",
                }
            )
        lk = libkey_url(identifier)
        if lk:
            out.append({"label": "LibKey ↓", "href": lk, "download": True})
        ax = arxiv_pdf_url(identifier)
        if ax:
            out.append({"label": "arXiv ↓", "href": ax, "download": True})
        u = uol_url(identifier)
        if u:
            out.append({"label": "UoL", "href": u})
        s = scholar_url(identifier)
        if s:
            out.append({"label": "Scholar", "href": s})
        return out


class YoutubePresenter(ItemPresenter):
    """A video already has a free thumbnail — YouTube's stable per-video
    still. Mirrors the fallback in ``routes/refs.py``'s ``_youtube_meta``
    (``video_id`` defaults to the ref's slug when there's no scraped
    cache row), so this needs no store round-trip."""

    def thumbnail(self, ref: Any) -> str | None:
        video_id = getattr(ref, "slug", None)
        if not video_id:
            return None
        return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"


def _fmt_num(v: float) -> str:
    """``123.0`` → ``"123"``, ``123.4`` → ``"123.4"`` — a badge-sized
    number, never a trailing ``.0``. Mirrors
    ``precis.diagram.handler``'s private ``_num`` (not imported: that
    module is core-only, this is a display-layer duplicate of one line)."""
    return str(int(v)) if v == int(v) else str(v)


class SePresenter(ItemPresenter):
    """``se`` design tree — the level reached (L0 block-graph through L3
    realized solids; see the package docstring's six-level IR) plus block
    / bound-structure counts, straight off the batched per-page lookup
    (``routes/items.py``'s ``_se_summaries_bulk``, one ``se_blocks`` query
    for however many ``se`` rows are on the page — never per-row). Stands
    in for the retiring ``/design`` tree list's one useful glance: how far
    along this design is, without opening it."""

    def state(
        self,
        ref: Any,
        *,
        has_chunks: bool,
        design: dict[str, Any] | None = None,
    ) -> list[dict[str, str]]:
        badges = super().state(ref, has_chunks=has_chunks, design=design)
        if not design:
            return badges
        level = design.get("level")
        if level:
            badges.append(
                {
                    "label": level,
                    "cls": "bg-indigo-100 text-indigo-700",
                    "title": f"highest IR level reached: {level} (see se-kind.md)",
                }
            )
        n_blocks = design.get("blocks") or 0
        if n_blocks:
            noun = "block" if n_blocks == 1 else "blocks"
            badges.append(
                {
                    "label": f"{n_blocks} {noun}",
                    "cls": "bg-slate-100 text-slate-600",
                    "title": f"{n_blocks} live block(s) in the design tree",
                }
            )
        n_bound = design.get("bound_structures") or 0
        if n_bound:
            badges.append(
                {
                    "label": f"{n_bound} bound",
                    "cls": "bg-teal-100 text-teal-700",
                    "title": (
                        f"{n_bound} atomic-mode block(s) bound to a structure design"
                    ),
                }
            )
        return badges


class StructurePresenter(ItemPresenter):
    """``structure`` design — atom count, run count, and the most recent
    successful relax's energy-ladder rung, off the batched per-page lookup
    (``routes/items.py``'s ``_structure_summaries_bulk``, three queries for
    however many ``structure`` rows are on the page, mirroring
    ``routes/structure.py``'s ``_list_rows`` per-row shape but batched).
    The one-glance summary ``/structure`` currently gives per row."""

    def state(
        self,
        ref: Any,
        *,
        has_chunks: bool,
        design: dict[str, Any] | None = None,
    ) -> list[dict[str, str]]:
        badges = super().state(ref, has_chunks=has_chunks, design=design)
        if not design:
            return badges
        n_atoms = design.get("atoms") or 0
        if n_atoms:
            badges.append(
                {
                    "label": f"{n_atoms} atoms",
                    "cls": "bg-slate-100 text-slate-600",
                    "title": f"{n_atoms} live atom(s)",
                }
            )
        n_runs = design.get("runs") or 0
        if n_runs:
            badges.append(
                {
                    "label": f"{n_runs} runs",
                    "cls": "bg-slate-100 text-slate-600",
                    "title": f"{n_runs} relax run(s) recorded",
                }
            )
        last_fidelity = design.get("last_fidelity")
        last_energy = design.get("last_energy")
        if last_fidelity and last_energy is not None:
            badges.append(
                {
                    "label": f"{last_fidelity} {_fmt_num(round(last_energy, 2))} eV",
                    "cls": "bg-emerald-100 text-emerald-700",
                    "title": (
                        f"last successful relax: {last_fidelity} rung, "
                        f"{last_energy:.4f} eV"
                    ),
                }
            )
        return badges


class PcbPresenter(ItemPresenter):
    """``pcb`` board — the last autoplace/route run's own summary, read
    straight off ``ref.meta`` (``last_place``/``last_route``, stamped in
    place by ``PcbHandler``'s ``place``/``route`` ops — see
    ``handlers/pcb.py``) with no extra query at all. The board SVG +
    schematic stay ``/pcb/<slug>``-only; step 4 (retiring that route into a
    Drive chip) is what would ever change that."""

    def state(
        self,
        ref: Any,
        *,
        has_chunks: bool,
        design: dict[str, Any] | None = None,
    ) -> list[dict[str, str]]:
        badges = super().state(ref, has_chunks=has_chunks, design=design)
        meta = getattr(ref, "meta", None) or {}
        last_route = meta.get("last_route")
        if isinstance(last_route, dict):
            realized = int(last_route.get("realized") or 0)
            failed = int(last_route.get("failed") or 0)
            cls = "bg-rose-100 text-rose-700" if failed else "bg-sky-100 text-sky-700"
            badges.append(
                {
                    "label": f"routed {realized}/{realized + failed}",
                    "cls": cls,
                    "title": (
                        f"last route: {realized} realized, {failed} failed net(s)"
                    ),
                }
            )
        elif isinstance(meta.get("last_place"), dict):
            badges.append(
                {
                    "label": "placed",
                    "cls": "bg-sky-100 text-sky-700",
                    "title": "components placed, not yet routed",
                }
            )
        return badges


#: Universal component specs surfaced as Drive-row badges — the fastener
#: facts an ISO title doesn't spell out (head form / drive type / thread
#: size, migrations 0093/0163). ``drive_size`` (a bare mm number, only
#: legible paired with ``drive_type``) is deliberately left for the
#: detail page rather than crowding a fourth badge onto a Drive row.
COMPONENT_BADGE_SPECS: tuple[str, ...] = ("thread_size", "drive_type", "head_form")


class ComponentPresenter(ItemPresenter):
    """procurable ``component`` — the category (free, off ``ref.meta``,
    same as every other component read) plus the fastener facts an ISO
    title doesn't spell out, off the batched per-page current-spec-value
    lookup (``routes/items.py``'s ``_component_summaries_bulk`` — one
    ``component_spec_values`` query for however many ``component`` rows
    are on the page, picking the current value the same way
    ``store/_component_ops.py``'s ``_CURRENT_ORDER`` does for a single
    part)."""

    def state(
        self,
        ref: Any,
        *,
        has_chunks: bool,
        design: dict[str, Any] | None = None,
    ) -> list[dict[str, str]]:
        badges = super().state(ref, has_chunks=has_chunks, design=design)
        meta = getattr(ref, "meta", None) or {}
        category = meta.get("category")
        if category:
            badges.append(
                {
                    "label": str(category),
                    "cls": "bg-slate-100 text-slate-600",
                    "title": "component category",
                }
            )
        specs = design or {}
        thread_size = specs.get("thread_size")
        if thread_size:
            badges.append(
                {
                    "label": str(thread_size),
                    "cls": "bg-violet-100 text-violet-700",
                    "title": f"thread size: {thread_size}",
                }
            )
        drive_type = specs.get("drive_type")
        if drive_type:
            badges.append(
                {
                    "label": f"{drive_type} drive",
                    "cls": "bg-violet-100 text-violet-700",
                    "title": f"drive type: {drive_type}",
                }
            )
        head_form = specs.get("head_form")
        if head_form:
            badges.append(
                {
                    "label": f"{head_form} head",
                    "cls": "bg-violet-100 text-violet-700",
                    "title": f"head form: {head_form}",
                }
            )
        return badges


class MaterialPresenter(ItemPresenter):
    """engineering ``material`` — the material class, free off
    ``ref.meta`` (migration 0092: ``meta = {aliases, material_class,
    composition/formula, notes}``, set at entity-write time). The
    per-property sourced values (``material_values``) are the real
    handbook content but live in a separate fact table — worth a query at
    2 prod rows, but out of scope for the no-N+1 pass this presenter is
    part of; the class alone is the one free fact."""

    def state(
        self,
        ref: Any,
        *,
        has_chunks: bool,
        design: dict[str, Any] | None = None,
    ) -> list[dict[str, str]]:
        badges = super().state(ref, has_chunks=has_chunks, design=design)
        meta = getattr(ref, "meta", None) or {}
        material_class = meta.get("material_class")
        if material_class:
            badges.append(
                {
                    "label": str(material_class),
                    "cls": "bg-amber-100 text-amber-700",
                    "title": "material class",
                }
            )
        return badges


class FigurePresenter(ItemPresenter):
    """``figure`` SVG canvas — the canvas size, free off
    ``ref.meta['viewbox']`` (``[x, y, w, h]``, stamped by
    ``DiagramHandler``/``SvgLang`` at put/edit time — ``diagram/handler.py``,
    ``figure/svg.py``). The assembled SVG itself stays
    ``/figure/<slug>``-only — rendering it here would mean reading +
    compositing the draft body chunks per row, exactly the per-row cost
    this pass rules out."""

    def state(
        self,
        ref: Any,
        *,
        has_chunks: bool,
        design: dict[str, Any] | None = None,
    ) -> list[dict[str, str]]:
        badges = super().state(ref, has_chunks=has_chunks, design=design)
        meta = getattr(ref, "meta", None) or {}
        box = meta.get("viewbox")
        if isinstance(box, (list, tuple)) and len(box) == 4:
            try:
                w, h = float(box[2]), float(box[3])
            except (TypeError, ValueError):
                return badges
            badges.append(
                {
                    "label": f"{_fmt_num(w)}×{_fmt_num(h)}",
                    "cls": "bg-slate-100 text-slate-600",
                    "title": "canvas size (viewBox)",
                }
            )
        return badges


class MermaidPresenter(ItemPresenter):
    """``mermaid`` diagram — currently identical to the generic default.

    Unlike ``figure``, ``MermaidLang.bounds_meta_key`` is unused
    (``read_bounds``/``default_bounds`` are always ``None`` — mermaid
    auto-lays-out, no coordinate frame — ``mermaid/mermaid.py``), so
    ``ref.meta`` carries only ``{"render": "mermaid"}``: nothing free to
    badge. A real per-row fact (node/edge count) lives only in the parsed
    source text, which would mean reading + compiling the draft body chunk
    per row — the per-row cost this pass rules out. This class exists as
    the registered seam (so the eventual real datum has a home + this
    docstring records why it isn't here yet), not because it renders
    anything beyond :class:`ItemPresenter` today."""


#: Per-kind presenter overrides — the registry seam for a kind whose
#: hover/thumbnail/actions need more than the generic default. Grow this
#: as kinds adopt a richer presenter; see the module docstring for why
#: this isn't yet a total ``@abstractmethod`` mapping over every kind.
_PRESENTER_CLASSES: dict[str, type[ItemPresenter]] = {
    "youtube": YoutubePresenter,
    "se": SePresenter,
    "structure": StructurePresenter,
    "pcb": PcbPresenter,
    "component": ComponentPresenter,
    "material": MaterialPresenter,
    "figure": FigurePresenter,
    "mermaid": MermaidPresenter,
}


def presenter_for(kind: str) -> ItemPresenter:
    """Return the presenter for ``kind`` — a registered override
    (:data:`_PRESENTER_CLASSES`) or the generic default."""
    cls = _PRESENTER_CLASSES.get(kind, ItemPresenter)
    return cls(kind)


#: Kinds declared ``placement='artifact'`` that fall back to when the live hub
#: isn't reachable — the sole facet resolver is :func:`artifact_kinds` below
#: (``routes/drive.py`` once carried a near-identical, never-called
#: ``_artifact_kinds``; it was dead code and has been removed).
#: ``tests/test_kind_totality.py`` pins the core (``precis.handlers``)
#: portion of this equal to the live ``placement_kinds(specs, "artifact")``
#: derivation (minus ``folder``, same as :func:`artifact_kinds` below
#: excludes) so a newly-declared core artifact kind failing to land here
#: fails CI instead of only ever showing up when the hub happens to be
#: reachable. Also carries every plugin-declared artifact kind this build
#: ships (``se``/``pathway``/``protein``/``route`` — entry points under
#: ``[project.entry-points."precis.handlers"]``, not under
#: ``precis.handlers`` itself, so they're outside that derivation's scope
#: by design — see ``kind_facts.all_declared_specs``'s docstring): a hub
#: that fails to construct still needs the Author facet to reach an ``se``
#: row, exactly the gap this constant exists to fill.
_ARTIFACT_KIND_FALLBACK: tuple[str, ...] = (
    "cad",
    "checklist",
    "draft",
    "figure",
    "make",
    "mermaid",
    "pathway",
    "plan",
    "protein",
    "route",
    "rxn",
    "se",
    "structure",
    "todo",
)


def artifact_kinds(hub: Any) -> list[str]:
    """Kinds declared ``placement='artifact'`` in this build (minus ``folder``)
    — the "Author" facet on ``/items`` (source vs. authored, per the
    proposal's "author/source split is a facet of ``KindSpec.placement``").
    Reads the live hub so a future placeable kind joins by declaration,
    with no route edit; falls back to a static list when the hub isn't
    wired (e.g. a test double with ``hub=None``)."""
    if hub is None:
        return list(_ARTIFACT_KIND_FALLBACK)
    try:
        out = []
        for k in sorted(hub.kinds):
            handler = hub.handler_for(k)
            spec = getattr(handler, "spec", None)
            if spec is not None and getattr(spec, "placement", None) == "artifact":
                if k != "folder":
                    out.append(k)
        return out
    except Exception:
        return list(_ARTIFACT_KIND_FALLBACK)


def item_row(
    ref: Any,
    block: Any,
    score: float,
    flags: set[str],
    *,
    has_chunks: bool = False,
    tags: list[tuple[str, str]] | None = None,
    identifier: str | None = None,
    summary: str | None = None,
    design: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one unified-list row view-model from a search hit.

    ``flags`` is the ref's active reading-intent flag values (for the
    toggle buttons). ``preview`` is the chunk that made the ref match
    (or its ``llm-v1`` gloss, when ``summary`` was batched for this
    hit). ``has_chunks`` drives the stub/ingested state badges (a search
    hit matched a chunk, so it's ``True``; a recent-list ref is probed).
    ``tags`` are the ref's raw ``(namespace, value)`` tags → the per-row
    chips. ``design`` is this one ref's slice of the caller's batched
    ``se``/``structure``/``component`` facts lookup (``None`` for every
    other kind, or when the caller has none to offer) — see
    :meth:`ItemPresenter.state`.
    """
    p = presenter_for(getattr(ref, "kind", ""))
    return {
        "id": getattr(ref, "id", None),
        "kind": getattr(ref, "kind", ""),
        "title": p.name(ref),
        "open_url": p.open_url(ref),
        "preview": p.preview(block, summary),
        "thumbnail": p.thumbnail(ref),
        "actions": p.actions(ref),
        "created_at": getattr(ref, "created_at", None),
        "state": p.state(ref, has_chunks=has_chunks, design=design),
        "tags": _display_tags(tags),
        "links": p.links(identifier),
        "score": score,
        "title_meta": p.title_meta(ref),
        "chunk_full": p.chunk_full(block),
        "flags": flags,
    }
