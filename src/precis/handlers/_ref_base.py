"""RefHandler — base handler for corpus-backed refs.

Provides generic read operations (TOC, chunks, links, summary, search,
list, notes) that work for any corpus type.  Subclasses override:

  * ``_read_overview()`` — corpus-specific overview formatting
  * ``_read_meta()``     — corpus-specific metadata display
  * ``views`` dict       — view name → dispatch method name
  * ``_overview_hints()``— extra Next: lines in overview
  * ``_list_header()``   — header line for list output
  * ``_list_entry()``    — format a single list entry

See PaperHandler and TodoHandler for concrete examples.
"""

from __future__ import annotations

import json as _json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from precis.grep import parse_grep
from precis.protocol import ErrorCode, Handler, PrecisError, extract_kwargs
from precis.uri import SEP

log = logging.getLogger(__name__)


def _get_store():
    """Lazy-load acatome_store to avoid hard dependency at import time."""
    from precis._store import get_store

    return get_store()


def _truncate(text: str, n: int = 100) -> str:
    return (text[:n] + "…") if len(text) > n else text


def _pluralise(count: int, singular: str, plural: str | None = None) -> str:
    """Return ``"1 result"`` / ``"3 results"`` etc.

    Tiny helper because ``f"{n} results"`` produced ``"1 results"`` all
    over the codebase — visible to every search call that returned a
    single hit.  ``plural`` defaults to ``singular + "s"`` for the
    common case; supply it explicitly when English would mangle the
    naive append (``hit`` / ``hits`` works, ``entry`` / ``entries``
    doesn't).
    """
    if count == 1:
        return f"1 {singular}"
    return f"{count} {plural or singular + 's'}"


def _parse_section(raw: str) -> str:
    """Extract clean section heading from JSON section_path string."""
    try:
        sp = _json.loads(raw) if raw else []
    except (ValueError, TypeError):
        sp = []
    heading = sp[0] if sp else ""
    heading = (
        heading.replace("\u2212", "-").replace("\u2013", "-").replace("\u2014", "-")
    )
    return heading


def _parse_date_value(value: str) -> datetime | None:
    """Parse a date keyword or ISO date string into a datetime.

    Supports: today, yesterday, this-week, this-month, ISO date (YYYY-MM-DD).
    Returns None if the value isn't recognized as a date.
    """
    v = value.strip().lower()
    now = datetime.now(UTC).replace(tzinfo=None)
    if v == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if v == "yesterday":
        return (now - timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    if v == "this-week":
        monday = now - timedelta(days=now.weekday())
        return monday.replace(hour=0, minute=0, second=0, microsecond=0)
    if v == "this-month":
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    try:
        return datetime.strptime(v, "%Y-%m-%d")
    except ValueError:
        return None


def _parse_year_value(value: str) -> tuple[int | None, int | None]:
    """Parse a year or year range string.

    Examples: '2024' → (2024,2024), '2020-2024' → (2020,2024), '2020-' → (2020,None).
    Returns (None, None) on invalid input.
    """
    value = value.strip()
    if not value:
        return (None, None)
    if "-" in value:
        parts = value.split("-", 1)
        try:
            lo = int(parts[0])
        except ValueError:
            return (None, None)
        hi_str = parts[1].strip()
        if not hi_str:
            return (lo, None)
        try:
            return (lo, int(hi_str))
        except ValueError:
            return (None, None)
    try:
        y = int(value)
        return (y, y)
    except ValueError:
        return (None, None)


_FILTER_PREFIXES = {"ingested", "year", "tag"}


def _parse_tags(ref: dict[str, Any]) -> list[str]:
    """Return the tag list for a ref, defensively.

    ``Ref.to_dict()`` exposes the ``tags`` column as a raw JSON-encoded
    string (see :class:`acatome_store.models.Ref`) — it is **not** an
    ORM relationship, despite the old ``[t.name for t in r.tags]``
    pattern that appeared in early ref handlers.  Accept either the
    raw string or an already-parsed list so this helper works against
    both the real store and test mocks.

    Promoted from ``handlers/todo.py`` in Apr 2026 after the memory
    handler's iteration-over-string bug (``tags: [, ", s, m, o, ...]``)
    was caught live — every ref kind that surfaces tags should funnel
    through here instead of re-implementing the parse.
    """
    raw = ref.get("tags")
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(t) for t in raw]
    try:
        parsed = _json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [str(t) for t in parsed] if isinstance(parsed, list) else []


def _parse_filters(grep: str) -> dict[str, str]:
    """Parse structured prefix filters from a grep string.

    Recognized prefixes: ingested:, year:, tag:.
    Remaining text becomes the 'grep' key.
    """
    result: dict[str, str] = {}
    remaining: list[str] = []
    for token in grep.split():
        if ":" in token:
            prefix, val = token.split(":", 1)
            if prefix in _FILTER_PREFIXES and val:
                result[prefix] = val
                continue
        remaining.append(token)
    result["grep"] = " ".join(remaining)
    return result


def _relative_date(dt: datetime | None) -> str:
    """Format a datetime as a relative string (today, yesterday, 3d ago, etc.)."""
    if dt is None:
        return ""
    now = datetime.now(UTC).replace(tzinfo=None)
    delta = now - dt
    days = delta.days
    if days == 0:
        return "today"
    if days == 1:
        return "yesterday"
    if days < 7:
        return f"{days}d ago"
    if days < 30:
        weeks = days // 7
        return f"{weeks}w ago"
    if days < 365:
        months = days // 30
        return f"{months}mo ago"
    return dt.strftime("%Y-%m-%d")


# ─────────────────────────────────────────────────────────────────────
# RefHandler base
# ─────────────────────────────────────────────────────────────────────


class RefHandler(Handler):
    """Base handler for corpus-backed refs (papers, todos, wiki, etc.).

    Subclasses must set ``scheme`` and typically override:
      - ``_read_overview()``
      - ``_read_meta()``
      - ``_overview_hints()`` for extra Next: lines

    Custom views are added by merging into the ``views`` dict::

        class PaperHandler(RefHandler):
            views = {
                **RefHandler.views,
                "abstract": "_read_abstract_view",
                "cite":     "_read_cite_view",
            }

    Each dispatch method has the uniform signature
    ``(self, store, ref, selector, subview, **kwargs) -> str`` and
    should validate its custom kwargs with :func:`extract_kwargs`.
    """

    scheme: str = ""
    writable: bool = False
    corpus_id: str = ""

    # Subclass display config
    _ref_noun: str = "ref"  # "paper", "todo", "wiki page"
    _ref_emoji: str = "📄"
    _max_list: int = 20

    #: Prefix under which subclass slugs are stored in Ref.slug (e.g.
    #: ``todo`` → stored slug ``todo:fix-the-bug``; ``fc``, ``memory``,
    #: ``conv`` similarly).  Papers store slugs without a prefix so the
    #: default is empty.  When set, :meth:`_resolve_ref` retries a
    #: ``<prefix>:<ident>`` lookup so agents can pass either the bare
    #: slug body or the full stored slug.
    _slug_prefix: str = ""

    # View registry: view-name → dispatch-method-name on self.  Subclasses
    # extend via ``views = {**RefHandler.views, ...}``.  All dispatch
    # methods share the signature
    #   (self, store, ref, selector, subview, **kwargs) -> str
    views = {
        "meta": "_read_meta_view",
        "summary": "_read_summary_view",
        "toc": "_read_toc_view",
        "chunk": "_read_chunk_view",
        "links": "_read_links_view",
        "links-in": "_read_links_inbound_view",
        "help": "_read_help_view",
    }

    # Collection-level view registry — dispatched when the caller passes
    # ``<scheme>:/<view>`` with no ref identifier.  Methods have the
    # signature ``(self, store, subview, **kwargs) -> str`` and must
    # tolerate ``subview=None``.  Subclasses extend via
    # ``collection_views = {**RefHandler.collection_views, ...}``.
    collection_views: dict[str, str] = {}

    # Base write vocabulary provided by RefHandler.put() — only 'note'.
    # Writable subclasses (todo, flashcard, memory, conversation) extend
    # this set so that MODE_UNSUPPORTED errors auto-fill options=.
    allowed_modes = {"note"}

    # Max blocks before switching to overview TOC
    _TOC_OVERVIEW_THRESHOLD = 50
    # Max blocks per section before splitting into sub-ranges
    _MAX_SECTION_BLOCKS = 60
    # Tiny sections get merged into the previous group
    _MERGE_THRESHOLD = 3

    def _block_chunk_hint(self, store, slug: str, block: dict) -> str:
        """Return a single-line ``→ get(id=…)`` hint for an empty block.

        Default: empty (no hint).  Subclasses with non-text block types
        whose payload lives in a sibling subview override this — see
        :meth:`PaperHandler._block_chunk_hint` for the figure case.

        Review 2026-04-25 mcp-critic finding B5 — empty figure-block
        chunks used to render as a header followed by two blank lines
        with no hint that the figure binary is reachable via
        ``/fig/N``.
        """
        return ""

    # ── Main dispatch ────────────────────────────────────────────────

    def read(
        self,
        path: str,
        selector: str | None,
        view: str | None,
        subview: str | None,
        query: str,
        summarize: bool,
        depth: int,
        page: int,
        **kwargs,
    ) -> str:
        store = _get_store()
        top_k = kwargs.get("top_k", 5)
        grep = str(kwargs.get("grep") or "").strip()

        # Bare call: list all refs (optionally filtered by grep or
        # semantic-search-via-query).  When both are supplied, ``grep``
        # is a metadata pre-filter and ``query`` is the vector search
        # over the filtered subset — the two compose.  BUG-F fix:
        # previously grep won outright and query was ignored.
        if not path and not selector and not view:
            if query and grep:
                return self._search_with_grep(
                    store, query=query, grep=grep, top_k=top_k
                )
            if grep:
                return self._list_refs(store, grep=grep)
            if query:
                return self._search_or_grep(store, query, top_k=top_k)
            return self._list_refs(store)

        # Search mode (scoped to a ref).  ``path`` here came from the
        # caller's ``scope=`` and is the slug we must restrict the
        # vector search to — passing it through fixes the silent
        # cross-paper leak where ``search(scope='X', query='Y')`` was
        # returning hits from any paper because the slug filter was
        # dropped on the floor between server and handler.
        if query or grep:
            return self._search_or_grep(
                store, query or grep, top_k=top_k, scope=path
            )

        # Collection-level views (``<scheme>:/<view>`` with no ref id).
        # Declared per-subclass via :attr:`collection_views`.  Dispatch
        # here before the "id required" guard so e.g. ``todo:/open``
        # doesn't fall into the per-ref error path.  Unknown views fall
        # through to the VIEW_UNKNOWN branch below (after ref resolve)
        # so the error lists the full set of per-ref views the caller
        # can try instead.
        if view and not path and view in self.collection_views:
            method_name = self.collection_views[view]
            return getattr(self, method_name)(store, subview, **kwargs)

        # Resolve ref
        if not path:
            # No id, no collection view — give the caller the full menu.
            collection_opts = sorted(self.collection_views.keys())
            next_hint = f"get(id='<{self._ref_noun}-slug>')"
            if collection_opts:
                opts = ", ".join(f"/{v}" for v in collection_opts)
                next_hint = (
                    f"{next_hint} — or pick a view: {opts} "
                    f"(e.g. {self.scheme}:/{collection_opts[0]})"
                )
            raise PrecisError(
                ErrorCode.PARAM_INVALID,
                cause=f"{self._ref_noun} identifier required",
                next=next_hint,
            )

        ref = self._resolve_ref(store, path)

        # View dispatch via the views registry — subclass extends by
        # dict-merging into ``views``.  Unknown view → VIEW_UNKNOWN with
        # options listed in ``/view`` form to match the URI shape the
        # caller writes.  The framework also auto-fills the same
        # ``/``-prefixed form when a raise site omits options=, so
        # both code paths now agree (mcp-critic finding M4 / C5).
        if view:
            method_name = self.views.get(view)
            if method_name is None:
                raise PrecisError(
                    ErrorCode.VIEW_UNKNOWN,
                    cause=f"view '/{view}' not supported on {self.scheme}",
                    options=sorted(f"/{v}" for v in self.views),
                )
            return getattr(self, method_name)(store, ref, selector, subview, **kwargs)

        # Selector without view: specific chunk(s)
        if selector:
            return self._read_chunks(store, ref, selector)

        # Default: overview
        return self._read_overview(store, ref)

    def put(
        self,
        path: str,
        selector: str | None,
        text: str,
        mode: str,
        **kwargs,
    ) -> str:
        # Writable subclasses (memory, todo, flashcard, conversation,
        # gripe, …) override ``put`` and only fall through to this base
        # implementation for unknown modes.  When that happens we must
        # *not* claim the kind is read-only \u2014 the caller can see other
        # successful writes happening to the same kind.  Branch on
        # ``self.writable`` so genuinely read-only kinds (paper, quest)
        # keep the read-only wording while writable kinds get an honest
        # "mode unknown" envelope.  ``_enrich_error`` auto-fills
        # ``options=`` from ``self.allowed_modes`` so the agent sees
        # the full mode vocabulary.  mcp-critic finding M3.
        if mode == "note":
            return self._write_note(path, selector, text, **kwargs)
        if getattr(self, "writable", False):
            raise PrecisError(
                ErrorCode.MODE_UNSUPPORTED,
                cause=f"mode {mode!r} unknown for {self.scheme}",
                next=(
                    "see options above; put(id='<slug>', mode='note', "
                    "text='...') always works to annotate"
                ),
            )
        raise PrecisError(
            ErrorCode.MODE_UNSUPPORTED,
            cause=f"mode {mode!r} not allowed on read-only {self.scheme}",
            next="put(id='<slug>', mode='note', text='...') to annotate",
        )

    # ── View dispatchers (uniform signature) ─────────────────────────
    #
    # Each dispatcher is thin: validate kwargs, delegate to the real
    # worker.  Subclasses override a dispatcher when they want a
    # scheme-specific variant (e.g. PaperHandler._read_meta_view), or
    # add new dispatchers for their own views.

    def _read_meta_view(self, store, ref, selector, subview, **kwargs) -> str:
        extract_kwargs(kwargs, (), context=f"{self.scheme}/meta")
        return self._read_meta(ref)

    def _read_summary_view(self, store, ref, selector, subview, **kwargs) -> str:
        extract_kwargs(kwargs, (), context=f"{self.scheme}/summary")
        return self._read_summary(store, ref, selector)

    def _read_toc_view(self, store, ref, selector, subview, **kwargs) -> str:
        extract_kwargs(kwargs, (), context=f"{self.scheme}/toc")
        return self._read_toc(store, ref, selector)

    def _read_chunk_view(self, store, ref, selector, subview, **kwargs) -> str:
        extract_kwargs(kwargs, (), context=f"{self.scheme}/chunk")
        if selector:
            return self._read_chunks(store, ref, selector)
        return self._read_toc(store, ref)

    def _read_links_view(self, store, ref, selector, subview, **kwargs) -> str:
        extract_kwargs(kwargs, (), context=f"{self.scheme}/links")
        return self._read_links(store, ref, selector)

    def _read_links_inbound_view(self, store, ref, selector, subview, **kwargs) -> str:
        extract_kwargs(kwargs, (), context=f"{self.scheme}/links-in")
        return self._read_links(store, ref, selector, direction="inbound")

    def _read_help_view(self, store, ref, selector, subview, **kwargs) -> str:
        """Inline the onboarding skill body for this kind (Phase 12b v1.1)."""
        extract_kwargs(kwargs, (), context=f"{self.scheme}/help")
        if not self.onboarding_skill:
            raise PrecisError(
                ErrorCode.VIEW_UNKNOWN,
                cause=f"{self.scheme} has no onboarding skill declared",
                next="search(type='skill', query='…') to find relevant skills",
            )
        # Delegate to SkillHandler.  Filesystem-native; always available.
        from precis.handlers.skill import SkillHandler

        sh = SkillHandler()
        sh._ensure_fresh()
        try:
            return sh._render_skill(self.onboarding_skill)
        except PrecisError as exc:
            if exc.code is ErrorCode.ID_NOT_FOUND:
                raise PrecisError(
                    ErrorCode.ID_NOT_FOUND,
                    cause=(
                        f"{self.scheme} declares onboarding_skill="
                        f"{self.onboarding_skill!r} but no SKILL.md for "
                        f"it is installed"
                    ),
                    next=(
                        f"create skill:{self.onboarding_skill} in "
                        f"~/.precis/skills/ or search(type='skill')"
                    ),
                ) from exc
            raise

    # ── Subclass hooks ───────────────────────────────────────────────

    def _read_overview(self, store, ref: dict) -> str:
        """Override in subclass for corpus-specific overview formatting."""
        slug = ref.get("slug", "???")
        title = ref.get("title", "")
        all_blocks = store.get_blocks(slug)
        n_blocks = len(all_blocks)

        lines = [f"{self._ref_emoji} {slug}"]
        if title:
            lines.append(f"  {title}")
        lines.append(f"  {n_blocks} blocks")

        # Link count hint
        try:
            link_counts = store.get_link_count(slug)
            if link_counts:
                total = sum(link_counts.values())
                lines.append(f"  {total} links")
        except Exception:
            pass

        lines.append("")
        lines.append("Next:")
        lines.append(f"  get(id='{slug}/toc')     — structure")
        lines.append(f"  get(id='{slug}{SEP}0..10')   — first 10 chunks")
        lines.append(f"  get(id='{slug}/links')   — links graph")
        for hint in self._overview_hints(slug, ref):
            lines.append(f"  {hint}")
        return "\n".join(lines)

    def _overview_hints(self, slug: str, ref: dict) -> list[str]:
        """Return extra Next: hint lines for overview. Override in subclass."""
        return []

    def _read_meta(self, ref: dict) -> str:
        """Override in subclass for corpus-specific metadata display."""
        lines = []
        for key in ("slug", "title"):
            val = ref.get(key, "")
            if val:
                lines.append(f"  {key}: {val}")
        ref_id = ref.get("ref_id") or ref.get("id")
        if ref_id:
            lines.append(f"  ref_id: {ref_id}")
        return "\n".join(lines)

    def _list_header(self, count: int, grep: str = "") -> str:
        """Header line for list output. Override for custom emoji/noun."""
        if grep:
            return f"{self._ref_emoji} {count} {self._ref_noun}s matching '{grep}'"
        return f"{self._ref_emoji} {count} {self._ref_noun}s in library"

    def _list_entry(self, ref: dict) -> str:
        """Format a single ref for the list. Override for custom columns."""
        slug = ref.get("slug", "???")
        title = _truncate(ref.get("title", ""), 80)
        return f"  {slug}  {title}"

    # ── Resolve ──────────────────────────────────────────────────────

    def _resolve_ref(self, store, ident: str) -> dict[str, Any]:
        """Resolve identifier (slug, DOI, or ref_id) to a ref dict.

        Tries the raw identifier first — matches papers' un-prefixed
        slugs and any DOI / ref_id lookup.  If that misses and the
        handler declares a ``_slug_prefix`` (e.g. ``todo``, ``fc``,
        ``memory``, ``conv``), retries with ``<prefix>:<ident>`` so
        agents can pass either the bare slug body (``fix-the-bug``)
        or the full stored form (``todo:fix-the-bug``) — the URI
        parser strips the scheme before the handler ever sees it,
        so the raw form is what arrives here on every call.
        """
        ref = store.get(ident)
        if (
            ref is None
            and self._slug_prefix
            and not ident.startswith(f"{self._slug_prefix}:")
        ):
            ref = store.get(f"{self._slug_prefix}:{ident}")
        if ref is None:
            raise PrecisError(
                ErrorCode.ID_NOT_FOUND,
                cause=f"{self._ref_noun} '{ident}' not in corpus",
            )
        return ref

    # ── List ─────────────────────────────────────────────────────────

    def _list_refs(self, store, grep: str = "") -> str:
        papers = store.list_papers(limit=10000)
        if not papers:
            return f"No {self._ref_noun}s in library."

        if grep:
            pattern = parse_grep(grep)

            def _matches(p: dict) -> bool:
                # ``p.get(key, "")`` still returns ``None`` when the
                # key is present with a ``None`` value (common for
                # partially-ingested refs: missing DOI, year, etc.).
                # Coerce every field defensively so the join never
                # sees a non-str element.
                blob = " ".join(
                    str(p.get(key) or "")
                    for key in ("slug", "title", "authors", "year", "doi")
                )
                return pattern.matches(blob)

            papers = [p for p in papers if _matches(p)]
            if not papers:
                return (
                    f"No {self._ref_noun}s matching '{grep}'.\n"
                    "Try: get(grep='...') with different keywords, or /regex/i for regex."
                )

        lines = [self._list_header(len(papers), grep), ""]

        cap = len(papers) if grep else self._max_list
        shown = papers[:cap]
        for p in shown:
            lines.append(self._list_entry(p))

        lines.append("")
        if len(papers) > cap:
            lines.append(f"  ... and {len(papers) - cap} more (showing first {cap})")
            lines.append("")
            lines.append("To find specific items:")
            lines.append("  search(query='...')  — semantic search")
            lines.append("  get(grep='...')      — filter by title/slug")
            lines.append("")
        lines.append(
            "Next: get(id='<slug>') for overview, get(id='<slug>/toc') for structure"
        )
        return "\n".join(lines)

    # ── TOC ──────────────────────────────────────────────────────────

    def _read_toc(self, store, ref: dict, selector: str | None = None) -> str:
        slug = ref.get("slug", "???")
        toc = store.get_toc(slug)
        if not toc:
            return f"No blocks found for {slug}"

        # Drop positionless blocks (``block_index IS NULL`` — abstract,
        # document_summary, paper_summary).  ``store.get_toc`` returns
        # every block_type for completeness; the TOC renderer needs
        # ordered positional blocks so it can compute ranges and split
        # oversized sections.  Without this filter the next step would
        # crash with ``TypeError: unsupported operand for -: NoneType``
        # when computing ``g["end"] - g["start"]``.
        toc = [e for e in toc if e.get("block_index") is not None]
        if not toc:
            # Every block was positionless (rare — typically a stub ref
            # with only an abstract).  Return a structured envelope with
            # concrete recovery paths instead of crashing or showing an
            # empty TOC.
            raise PrecisError(
                ErrorCode.UNAVAILABLE,
                cause=(
                    f"{slug!r} has no positional blocks — only metadata "
                    "(abstract / summary).  TOC requires ordered text."
                ),
                next=(
                    f"get(id='{slug}/abstract')  — read the abstract\n"
                    f"  get(id='{slug}/summary')   — derived summary\n"
                    f"  get(id='{slug}')           — overview + metadata\n"
                    f"  put(type='gripe', text='{slug} has no body blocks — "
                    "ingestion may have failed') if you expected text"
                ),
            )

        if selector:
            return self._read_toc_range(slug, toc, selector)

        if len(toc) <= self._TOC_OVERVIEW_THRESHOLD:
            return self._read_toc_flat(slug, toc)

        return self._read_toc_overview(slug, toc)

    def _read_toc_flat(self, slug: str, toc: list[dict]) -> str:
        return self._format_grouped_toc(
            slug, toc, f"{self._ref_emoji} {slug}  ({len(toc)} blocks)"
        )

    def _read_toc_overview(self, slug: str, toc: list[dict]) -> str:
        """Section-based overview TOC for large documents."""
        raw_groups: list[dict] = []
        current: dict | None = None

        for entry in toc:
            sp_raw = entry.get("section_path", "")
            try:
                sp_list = _json.loads(sp_raw) if sp_raw else []
            except (ValueError, TypeError):
                sp_list = []
            heading = sp_list[0] if sp_list else ""
            idx = entry.get("block_index", 0)
            preview = entry.get("summary") or entry.get("preview", "")

            if current is None or heading != current["heading"]:
                current = {
                    "heading": heading,
                    "headings": [heading] if heading else [],
                    "start": idx,
                    "end": idx,
                    "previews": [],
                }
                raw_groups.append(current)
            current["end"] = idx
            if preview:
                current["previews"].append(preview)

        # Merge tiny sections
        merged: list[dict] = []
        for g in raw_groups:
            size = g["end"] - g["start"] + 1
            if merged and size <= self._MERGE_THRESHOLD:
                prev = merged[-1]
                prev["end"] = g["end"]
                if g["heading"] and g["heading"] not in prev["headings"]:
                    prev["headings"].append(g["heading"])
                prev["previews"].extend(g["previews"])
            else:
                merged.append(g)

        # Split oversized groups
        final_groups: list[dict] = []
        for g in merged:
            size = g["end"] - g["start"] + 1
            if size <= self._MAX_SECTION_BLOCKS:
                final_groups.append(g)
            else:
                sub_entries = [
                    e for e in toc if g["start"] <= e.get("block_index", 0) <= g["end"]
                ]
                for i in range(0, len(sub_entries), self._MAX_SECTION_BLOCKS):
                    chunk = sub_entries[i : i + self._MAX_SECTION_BLOCKS]
                    s_idx = chunk[0].get("block_index", 0)
                    e_idx = chunk[-1].get("block_index", 0)
                    previews = [
                        e.get("summary") or e.get("preview", "")
                        for e in chunk
                        if e.get("summary") or e.get("preview", "")
                    ]
                    final_groups.append(
                        {
                            "heading": g["heading"],
                            "headings": g.get("headings", []),
                            "start": s_idx,
                            "end": e_idx,
                            "previews": previews,
                        }
                    )

        lines = [
            f"{self._ref_emoji} {slug}  "
            f"({len(toc)} blocks, {len(final_groups)} sections)",
            "",
        ]
        for g in final_groups:
            size = g["end"] - g["start"] + 1
            heading = g["heading"] or "(untitled)"
            snippet = ""
            heading_lower = heading.lower()
            for p in g.get("previews", []):
                if p.lower().strip() != heading_lower.strip():
                    snippet = _truncate(p, 80)
                    break
            line = (
                f"  {SEP}{g['start']}..{g['end']}  ({size})  {_truncate(heading, 60)}"
            )
            if snippet:
                line += f"  — {snippet}"
            lines.append(line)

        lines.append("")
        lines.append(
            f"Next: get(id='{slug}{SEP}0..{min(60, len(toc) - 1)}/toc') "
            f"to drill into a range"
        )
        return "\n".join(lines)

    def _read_toc_range(self, slug: str, toc: list[dict], selector: str) -> str:
        """Detailed TOC for a block range (drill-down)."""
        try:
            if ".." in selector:
                parts = selector.split("..")
                start = int(parts[0])
                end = int(parts[1]) if parts[1] else start + 60
            else:
                start = int(selector)
                end = start + 60
        except ValueError as exc:
            raise PrecisError(
                ErrorCode.ID_MALFORMED,
                cause=f"invalid block range: {selector!r}",
                next=f"use id='<slug>{SEP}N..M/toc'",
            ) from exc

        filtered = [e for e in toc if start <= e.get("block_index", 0) <= end]
        if not filtered:
            return f"No blocks in {SEP}{start}..{end} for {slug}"

        header = (
            f"{self._ref_emoji} {slug}  {SEP}{start}..{end}  ({len(filtered)} blocks)"
        )
        result = self._format_grouped_toc(slug, filtered, header)

        last_idx = filtered[-1].get("block_index", end)
        max_idx = toc[-1].get("block_index", 0)
        if last_idx < max_idx:
            next_start = last_idx + 1
            result += (
                f"\nNext: get(id='{slug}{SEP}{next_start}..{min(next_start + 60, max_idx)}/toc') "
                f"for next section"
            )
        return result

    def _format_grouped_toc(self, slug: str, entries: list[dict], header: str) -> str:
        """Format TOC entries grouped by section heading."""
        lines = [header, ""]
        current_section = None
        has_summaries = False

        for entry in entries:
            idx = entry.get("block_index", "?")
            kind = entry.get("block_type", "text")
            preview = entry.get("summary") or entry.get("preview", "")
            section = _parse_section(entry.get("section_path", ""))
            has_summary = entry.get("has_summary", False)
            if has_summary:
                has_summaries = True

            if section != current_section:
                current_section = section
                if section:
                    lines.append(f"  {SEP}{idx}  §{section}")
                    if kind == "section_header":
                        continue

            mark = "✦" if has_summary else " "
            type_tag = f"  [{kind}]" if kind != "text" else ""
            snippet = f"  {_truncate(preview, 80)}" if preview else ""
            lines.append(f"    {SEP}{idx}{mark}{type_tag}{snippet}")

        lines.append("")
        lines.append(f"Read: get(id='{slug}{SEP}N') for full chunk text")
        if has_summaries:
            lines.append(f"✦ = summary available: get(id='{slug}{SEP}N/summary')")
        return "\n".join(lines)

    # ── Chunks ───────────────────────────────────────────────────────

    def _read_chunks(self, store, ref: dict, selector: str) -> str:
        slug = ref.get("slug", "???")
        try:
            if ".." in selector:
                parts = selector.split("..")
                start = int(parts[0])
                end = int(parts[1]) if parts[1] else start + 10
            else:
                start = int(selector)
                end = start + 1
        except ValueError as exc:
            raise PrecisError(
                ErrorCode.ID_MALFORMED,
                cause=f"invalid chunk selector: {selector!r}",
                next=f"use id='<slug>{SEP}N', '{SEP}N..M', or '{SEP}N..'",
            ) from exc

        # Phase 7b — reject negative indices.  Block indices are
        # non-negative integers (dense 0..N per paper), so a negative
        # selector is always a typo or an off-by-one bug.  Silently
        # returning "No blocks in range" was indistinguishable from a
        # valid empty range, hiding the agent's mistake.
        if start < 0 or end < 0:
            raise PrecisError(
                ErrorCode.ID_MALFORMED,
                cause=(
                    f"invalid chunk selector: {selector!r} — block indices "
                    "are non-negative (papers are numbered 0..N)"
                ),
                next=(
                    f"use id='<slug>{SEP}0' for the first block, "
                    f"or get(id='{slug}/toc') to see the available range"
                ),
            )

        # Inverted ranges (``~5..3``) used to fall through to "no
        # blocks in range" silently — indistinguishable from a valid
        # empty selector, while blocks 3..5 actually exist.  Detect
        # and surface the inversion so the caller can swap the ends.
        # Review 2026-04-25 mcp-critic finding M (inverted range).
        if start > end:
            raise PrecisError(
                ErrorCode.ID_MALFORMED,
                cause=(
                    f"inverted chunk range {selector!r} — "
                    f"start ({start}) is greater than end ({end})"
                ),
                next=f"try id='{slug}{SEP}{end}..{start}' (swap the ends)",
            )

        # Don't filter by block_type — search returns hits across every
        # type that has an embedding (figure captions, section headers,
        # lists), so a search hint at ``slug›N`` could refer to any of
        # them.  Filtering to text-only here would make non-text hits
        # silently unreachable ("No blocks in range" while search swore
        # one was there).  The block_type is surfaced in the per-block
        # header so the agent knows what they're reading.
        #
        # Drop positionless blocks (``block_index IS NULL`` — abstract,
        # document_summary) up-front: they have no chunk number, can't
        # be in a numeric range, and would crash the comparison below.
        # See Phase 4 /toc fix for the same pattern.
        all_blocks = [
            b for b in store.get_blocks(slug) if b.get("block_index") is not None
        ]
        # Clamp the requested range to the paper's actual block count so
        # the trailer never invites the caller off the end of the paper.
        # Review 2026-04-25 finding D5 — ``~38..200`` on an 87-block
        # paper used to deliver blocks 38..85 then advertise a fake
        # ``Next: ~200..`` hint, which paginated into an empty range.
        max_idx = (
            max(b["block_index"] for b in all_blocks) if all_blocks else -1
        )
        clamped_end = end
        clamped = False
        if all_blocks and end > max_idx + 1:
            clamped_end = max_idx + 1
            clamped = True
        blocks = [
            b for b in all_blocks if start <= b["block_index"] < clamped_end
        ]

        if not blocks:
            if all_blocks and start > max_idx:
                return (
                    f"No blocks in range {SEP}{start}..{end} for {slug} — "
                    f"paper has {len(all_blocks)} blocks "
                    f"({SEP}0..{max_idx}).  "
                    f"Try get(id='{slug}/toc') for structure."
                )
            return f"No blocks in range {SEP}{start}..{end} for {slug}"

        # Pre-fetch link counts for all blocks in range
        block_link_counts: dict[str, int] = {}
        try:
            all_links = store.get_links(slug)
            for lnk in all_links:
                for nid_key in ("src_node_id", "dst_node_id"):
                    nid = lnk.get(nid_key)
                    if nid:
                        block_link_counts[nid] = block_link_counts.get(nid, 0) + 1
        except Exception:
            pass

        lines = []
        if clamped:
            # Tell the caller their range was wider than the paper so
            # they don't think we silently dropped anything.  Single
            # leading line keeps the chunk output uncluttered.
            lines.append(
                f"Range clamped to {SEP}{start}..{max_idx} "
                f"(paper has {len(all_blocks)} blocks).\n"
            )
        last_idx_emitted = blocks[-1]["block_index"]
        for block in blocks:
            idx = block.get("block_index", "?")
            kind = block.get("block_type", "text")
            text = block.get("text", "")
            page = block.get("page", "")
            header = f">> {slug} {SEP}{idx}  p{page}"
            # Surface block_type when it isn't plain text so the agent
            # immediately sees they're reading a figure caption / list /
            # section header rather than body prose.  Keeps the common
            # case (text) noise-free.
            if kind and kind != "text":
                header += f"  [{kind}]"
            node_id = block.get("node_id")
            n_links = block_link_counts.get(node_id, 0) if node_id else 0
            if n_links:
                header += f"  [{n_links} link{'s' if n_links != 1 else ''}]"
            lines.append(header)
            lines.append(text)
            # Empty-content blocks (figures with no extracted caption)
            # used to render as a header followed by two blank lines.
            # The agent had no signal that the chunk is intentionally
            # opaque (image binary lives elsewhere) vs. an extraction
            # failure.  Subclasses can override ``_block_chunk_hint``
            # to emit a ``→ get(id=…)`` line pointing at the right
            # subview.  Review 2026-04-25 mcp-critic finding B5.
            if not (text or "").strip():
                hint = self._block_chunk_hint(store, slug, block)
                if hint:
                    lines.append(hint)
            lines.append("")

        # Trailer (D5): suppress the off-the-end ``Next:`` hint when we
        # already delivered the last block.  Always append a clear
        # "End of paper" marker in that case so the caller doesn't
        # paginate further.
        at_end = last_idx_emitted >= max_idx
        if at_end:
            lines.append(
                f"End of paper ({len(all_blocks)} blocks, last {SEP}{max_idx})."
            )
        elif end - start >= 10:
            next_start = last_idx_emitted + 1
            lines.append(f"Next: get(id='{slug}{SEP}{next_start}..') for more")
        return "\n".join(lines)

    # ── Summary ──────────────────────────────────────────────────────

    def _read_summary(self, store, ref: dict, selector: str | None) -> str:
        slug = ref.get("slug", "???")
        if selector:
            try:
                idx = int(selector)
            except ValueError as exc:
                raise PrecisError(
                    ErrorCode.ID_MALFORMED,
                    cause=f"invalid chunk index: {selector!r}",
                ) from exc
            if idx < 0:
                raise PrecisError(
                    ErrorCode.ID_MALFORMED,
                    cause=(
                        f"invalid chunk index: {selector!r} — block "
                        "indices are non-negative (papers numbered 0..N)"
                    ),
                )
            # No block_type filter — see _read_chunks for rationale.
            # Search hits land on any embedded block, so /summary on a
            # non-text block (figure caption, etc.) must still resolve.
            # Equality with ``idx`` (an int) implicitly drops any
            # positionless block whose block_index is None.
            blocks = store.get_blocks(slug)
            target = [b for b in blocks if b.get("block_index") == idx]
            if not target:
                raise PrecisError(
                    ErrorCode.ID_NOT_FOUND,
                    cause=f"block {SEP}{idx} not found in {slug}",
                )
            summary = target[0].get("summary", "")
            return summary or f"No enrichment summary for block {SEP}{idx}"
        blocks = store.get_blocks(slug, block_type="document_summary")
        if not blocks:
            blocks = store.get_blocks(slug, block_type="paper_summary")
        if blocks:
            return blocks[0].get("text", "")
        return f"No document-level summary for {slug}"

    # ── Links ────────────────────────────────────────────────────────

    def _read_links(
        self,
        store,
        ref: dict,
        selector: str | None,
        *,
        direction: str = "both",
    ) -> str:
        """Render the link graph centred on ``ref``.

        ``direction``:

        - ``"both"`` (default, used by ``/links``) — outbound + inbound.
        - ``"outbound"`` — only links this ref points at.
        - ``"inbound"`` (used by ``/links-in``) — only links that point
          at this ref.  Useful for "what cites me" / "what references
          this memory" queries.
        """
        slug = ref.get("slug", "???")
        node_id = None
        if selector:
            try:
                block_idx = int(selector)
            except ValueError as exc:
                raise PrecisError(
                    ErrorCode.ID_MALFORMED,
                    cause=f"invalid block index: {selector!r}",
                ) from exc
            if block_idx < 0:
                raise PrecisError(
                    ErrorCode.ID_MALFORMED,
                    cause=(
                        f"invalid block index: {selector!r} — indices are "
                        "non-negative (papers numbered 0..N)"
                    ),
                )
            # No block_type filter — search hits can land on any embedded
            # block, so /links on a figure or header block must resolve.
            # Equality with ``block_idx`` (an int) implicitly drops any
            # positionless block whose block_index is None.
            blocks = store.get_blocks(slug)
            target = [b for b in blocks if b.get("block_index") == block_idx]
            if not target:
                raise PrecisError(
                    ErrorCode.ID_NOT_FOUND,
                    cause=f"block {SEP}{block_idx} not found in {slug}",
                )
            node_id = target[0].get("node_id")

        links = store.get_links(slug, node_id=node_id, direction=direction)
        label_map = {
            "both": "Links",
            "outbound": "Outbound links",
            "inbound": "Inbound links",
        }
        label = label_map.get(direction, "Links")

        if not links:
            anchor = f"{SEP}{selector}" if selector else ""
            empty_hint = (
                "  put(id='{slug}', link='other_slug:cites')  — create a link"
                if direction != "inbound"
                else "  (no inbound links — nothing references this ref yet)"
            ).format(slug=slug)
            return f"No {label.lower()} for {slug}{anchor}\nNext:\n{empty_hint}"

        lines = [
            f"{label} for {slug}"
            + (f"{SEP}{selector}" if selector else "")
            + f"  ({len(links)} total)"
        ]
        lines.append("")
        for link in links:
            ldir = link.get("direction", "?")
            rel = link.get("display_relation", link.get("relation", "?"))
            if ldir == "outbound":
                other = link.get("dst_slug", "?")
                arrow = f"  → [{rel}] → {other}"
                if link.get("dst_node_id"):
                    arrow += " (block)"
            else:
                other = link.get("src_slug", "?")
                arrow = f"  ← [{rel}] ← {other}"
                if link.get("src_node_id"):
                    arrow += " (block)"
            lines.append(arrow)

        lines.append("")
        lines.append("Next:")
        if direction != "outbound":
            lines.append(f"  get(id='{slug}/links')     — all directions")
        if direction != "inbound":
            lines.append(f"  get(id='{slug}/links-in')  — inbound-only")
        lines.append(f"  put(id='{slug}', link='other_slug:cites')   — add link")
        lines.append(f"  put(id='{slug}', unlink='other_slug')       — remove links")
        lines.append(f"  get(id='{slug}')  — overview")
        return "\n".join(lines)

    # ── Search ───────────────────────────────────────────────────────

    def _search_or_grep(
        self,
        store,
        query: str,
        top_k: int = 10,
        scope: str = "",
    ) -> str:
        # Every ref-backed kind now participates in semantic search.
        # Historical note: before the corpus_id filter landed in the
        # PgVectorIndex (see acatome-store v5.1), non-paper kinds fell
        # back to keyword grep because ``search_text`` couldn't filter
        # by corpus — cross-contamination was the only alternative.
        # The filter now JOINs ``blocks → refs`` so ``corpora=[self.corpus_id]``
        # returns exactly the current kind's blocks ranked by embedding
        # distance.  We keep the grep fallback for missing embedder /
        # ANN backend (e.g. Chroma without the right config) so the
        # call still degrades gracefully to substring search.
        #
        # ``scope`` (April 2026 fix): when set, restrict semantic hits to
        # blocks owned by that ref's slug.  Implemented as over-fetch +
        # post-filter so it works on any backend that supports
        # ``search_text`` regardless of whether it offers a slug index.
        try:
            return self._search(store, query, top_k=top_k, scope=scope)
        except (ImportError, ModuleNotFoundError):
            log.info("Semantic search unavailable, falling back to keyword grep")
            # Grep fallback: when scoped, restrict to that one ref's row
            # in the list view; otherwise return the whole grep'd list.
            if scope:
                return self._list_refs(store, grep=scope)
            return self._list_refs(store, grep=query)

    def _search_with_grep(
        self, store, query: str, grep: str, top_k: int = 10
    ) -> str:
        """Vector search pre-filtered by grep metadata.  BUG-F path.

        ``grep`` restricts the paper set first (fast metadata filter);
        the vector search then runs with over-fetch so enough hits
        survive the post-filter to fill ``top_k``.  Non-paper corpora
        don't have a vector index so they fall back to the
        ``grep``-plus-``query`` combined substring match on the ref
        list (deterministic, fast).
        """
        # Non-paper corpora: combine query into grep as a second
        # substring filter.  ``parse_grep`` supports AND by combining
        # keywords; we approximate by concatenating the two.
        if self.corpus_id and self.corpus_id != "papers":
            return self._list_refs(store, grep=f"{grep} {query}")

        # Paper path: compute the set of slugs that pass the grep
        # pre-filter, then vector-search with over-fetch and drop hits
        # whose paper isn't in the filtered set.
        papers = store.list_papers(limit=10000)
        pattern = parse_grep(grep)
        filtered_slugs: set[str] = set()
        for p in papers:
            blob = " ".join(
                str(p.get(key) or "")
                for key in ("slug", "title", "authors", "year", "doi")
            )
            if pattern.matches(blob):
                slug = p.get("slug")
                if slug:
                    filtered_slugs.add(slug)

        if not filtered_slugs:
            return (
                f"No papers matching grep='{grep}' — the filter "
                f"eliminated every candidate before the vector search "
                f"for query='{query}' could run.\n"
                "Try a broader grep=, or drop it to search the full corpus."
            )

        # Over-fetch so enough hits survive the post-filter.  5× is
        # empirical — agents usually ask top_k=5, so 25 candidates
        # leaves room for a tag-filter that keeps ~20% of the corpus.
        try:
            hits = store.search_text(query, top_k=max(top_k * 5, 25))
        except (ImportError, ModuleNotFoundError):
            log.info("Semantic search unavailable, falling back to grep-only")
            return self._list_refs(store, grep=grep)

        filtered_hits = [
            h
            for h in hits
            if (
                h.get("paper", {}).get("slug")
                or h.get("metadata", {}).get("slug")
            )
            in filtered_slugs
        ][:top_k]

        if not filtered_hits:
            return (
                f"No results for query='{query}' within the "
                f"grep='{grep}'-filtered subset "
                f"({len(filtered_slugs)} papers).\n"
                "Try broader terms for either filter."
            )

        lines = [
            f"🔍 {_pluralise(len(filtered_hits), 'result')} for: {query} "
            f"(filtered by grep='{grep}')",
            "",
        ]
        for hit in filtered_hits:
            text = hit.get("text", "")
            distance = hit.get("distance", 0)
            meta = hit.get("metadata", {})
            paper_info = hit.get("paper", {})
            slug = paper_info.get("slug", meta.get("slug", "???"))
            block_idx = meta.get("block_index", "?")
            summary = hit.get("summary", "")
            snippet = summary or _truncate(text, 100)
            lines.append(
                f"  {slug}{SEP}{block_idx}  ({distance:.2f})  {snippet}"
            )
        lines.append("")
        lines.append("Next:")
        first = filtered_hits[0]
        fslug = first.get("paper", {}).get("slug") or first.get(
            "metadata", {}
        ).get("slug", "???")
        fblock = first.get("metadata", {}).get("block_index", "?")
        lines.append(f"  get(id='{fslug}{SEP}{fblock}')  — read this chunk")
        lines.append(f"  get(id='{fslug}/toc')  — structure")
        return "\n".join(lines)

    def _format_search_hit_line(self, hit: dict) -> str:
        """Render one search-hit line.

        Stamps non-text ``block_type`` (figure / section_header / list
        / etc.) into the output so the agent isn't surprised when
        ``get(id='slug›N')`` returns a figure caption or header rather
        than body prose.  Text hits stay clean — no ``[text]`` noise on
        the common case.

        The previous version of this code rendered every hit identically
        regardless of type.  Combined with the chunk reader's
        ``block_type='text'`` filter, that produced a "search points at
        a chunk that doesn't exist" experience: the search said ``›0``
        was relevant, but ``get(id='slug›0')`` returned ``"No blocks in
        range"`` because ›0 was a section_header.  The type tag closes
        the gap by making the kind of block visible at search time.
        """
        text = hit.get("text", "")
        distance = hit.get("distance", 0)
        meta = hit.get("metadata", {})
        paper_info = hit.get("paper", {})
        slug = paper_info.get("slug", meta.get("slug", "???"))
        block_idx = meta.get("block_index", "?")
        btype = meta.get("type") or meta.get("block_type", "text")
        type_tag = f"  [{btype}]" if btype and btype != "text" else ""
        summary = hit.get("summary", "")
        snippet = summary or _truncate(text, 100)
        return f"  {slug}{SEP}{block_idx}{type_tag}  ({distance:.2f})  {snippet}"

    def _search(
        self,
        store,
        query: str,
        top_k: int = 10,
        scope: str = "",
    ) -> str:
        # Scope the vector search to this handler's own corpus so
        # e.g. ``search(type='memory')`` returns only memory blocks.
        # Papers keep a None/explicit filter so an unscoped search
        # on ``type='paper'`` behaves as before (only the papers
        # corpus has block-level embeddings for every ref).
        kwargs: dict[str, object] = {"top_k": top_k}
        if self.corpus_id:
            kwargs["corpora"] = [self.corpus_id]

        # When ``scope`` is set (came from the user's ``scope=`` arg,
        # e.g. ``search(query='X', scope='wang2020state')``), over-fetch
        # so enough hits survive the post-filter to fill ``top_k``.  5×
        # is empirical, mirroring ``_search_with_grep``.  We can't push
        # the slug filter down to ``search_text`` reliably across
        # backends (pgvector index has it, Chroma does not), so the
        # post-filter is the portable contract.
        if scope:
            kwargs["top_k"] = max(top_k * 5, 25)

        hits = store.search_text(query, **kwargs)

        # Post-filter by scope.  ``hit['paper']['slug']`` is the
        # canonical location; ``hit['metadata']['slug']`` is the
        # fallback some backends populate.  Both must agree for a
        # match because a stale/aliased slug would be confusing.
        if scope:
            hits = [
                h
                for h in hits
                if (
                    h.get("paper", {}).get("slug") == scope
                    or h.get("metadata", {}).get("slug") == scope
                )
            ][:top_k]

        if not hits:
            if scope:
                # Specific zero-match: tell the caller exactly why and
                # offer concrete recovery paths.  Do NOT fall back to a
                # corpus-wide search — that would silently restore the
                # old buggy behaviour.
                return (
                    f"No results for query={query!r} within "
                    f"scope={scope!r}.\n"
                    f"\nNext:\n"
                    f"  get(id='{scope}')                    "
                    f"— check the ref exists and read its overview\n"
                    f"  search(query={query!r}, type='{self.scheme}') "
                    f"— search the whole {self._ref_noun} corpus\n"
                    f"  get(id='{scope}/toc')                "
                    f"— browse the ref's structure"
                )
            return f"No results for: {query}"

        # Quality banner: when every returned hit has cosine distance
        # > 0.4 the result set is dominated by noise — at that range
        # the embedder is essentially saying "I have no idea what you
        # mean".  Without a flag, an agent that doesn't track distance
        # itself will trust the top hit.  Threshold and one-liner
        # banner shape per mcp-critic finding N1.
        distances = [h.get("distance", 0.0) for h in hits]
        weak = bool(distances) and min(distances) > 0.4
        if weak:
            header = (
                f"🔍 {_pluralise(len(hits), 'weak result')} for: {query}  "
                f"(best d={min(distances):.2f} — consider rephrasing)"
            )
        else:
            header = f"🔍 {_pluralise(len(hits), 'result')} for: {query}"
        if scope:
            header += f"  (scope={scope!r})"
        lines = [header, ""]
        for hit in hits:
            lines.append(self._format_search_hit_line(hit))
        lines.append("")
        seen = []
        for hit in hits[:3]:
            pi = hit.get("paper", {})
            s = pi.get("slug", hit.get("metadata", {}).get("slug"))
            bi = hit.get("metadata", {}).get("block_index")
            if s and bi is not None and s not in [x[0] for x in seen]:
                seen.append((s, bi))
        lines.append("Next:")
        if seen:
            s0, b0 = seen[0]
            lines.append(f"  get(id='{s0}{SEP}{b0}')  — read this chunk")
            if len(seen) > 1:
                batch = ",".join(f"{s}{SEP}{b}" for s, b in seen)
                lines.append(f"  get(id='{batch}')  — batch read")
            lines.append(f"  get(id='{s0}/toc')  — structure")
        return "\n".join(lines)

    # ── Notes ────────────────────────────────────────────────────────

    def _write_note(
        self,
        path: str,
        selector: str | None,
        text: str,
        **kwargs,
    ) -> str:
        if not text:
            raise PrecisError(
                ErrorCode.PARAM_INVALID,
                cause="text= required for note",
                next="put(id='<slug>', note='your annotation')",
            )
        store = _get_store()
        ref = self._resolve_ref(store, path)
        slug = ref.get("slug", "???")
        ref_id = ref.get("ref_id") or ref.get("id")

        title = kwargs.get("title", "")
        tags = kwargs.get("tags", [])

        if selector:
            try:
                block_idx = int(selector)
            except ValueError as exc:
                raise PrecisError(
                    ErrorCode.ID_MALFORMED,
                    cause=f"invalid block index for note: {selector!r}",
                ) from exc
            if block_idx < 0:
                raise PrecisError(
                    ErrorCode.ID_MALFORMED,
                    cause=(
                        f"invalid block index for note: {selector!r} — "
                        "indices are non-negative (papers numbered 0..N)"
                    ),
                )
            # No block_type filter — annotating a figure caption or
            # section header is just as valid as annotating body text.
            # Equality with ``block_idx`` (an int) implicitly drops any
            # positionless block whose block_index is None.
            blocks = store.get_blocks(slug)
            target = [b for b in blocks if b.get("block_index") == block_idx]
            if not target:
                raise PrecisError(
                    ErrorCode.ID_NOT_FOUND,
                    cause=f"block {SEP}{block_idx} not found in {slug}",
                )
            block_node_id = target[0].get("node_id")
            note_id = store.add_note(
                text,
                ref_id=ref_id,
                block_node_id=block_node_id,
                title=title or None,
                tags=tags or None,
                origin="bot",
            )
            return f"📝 Note #{note_id} on {slug}{SEP}{block_idx}\n{text}"
        else:
            note_id = store.add_note(
                text,
                ref_id=ref_id,
                title=title or None,
                tags=tags or None,
                origin="bot",
            )
            return f"📝 Note #{note_id} on {slug}\n{text}"
