"""OracleHandler — wisdom traditions as paper-shaped refs.

Replaces the v1 ``wisdom:`` and ``iching:`` kinds with a single
paper-shaped corpus where each ref is a tradition (I-Ching, chengyu,
stoic, …) and each chunk is one entry within that tradition.

Design choices (see docs/stochastic-kinds-plan.md, Phase F):

- **One ref per tradition, one chunk per entry.**  ``oracle:iching``
  is the I-Ching ref with 64 chunks (one per hexagram).
  ``oracle:chengyu`` is the chengyu ref with N chunks.  Etc.
- **Tags inherit via JOIN.**  Every chunk inside a stoic-tagged ref
  is reachable by ``random:?corpus=oracle&tag=stoic`` because the
  random handler joins blocks→refs and applies the tag filter on
  ``Ref.tags``.  No per-chunk tagging required.
- **Layout-aware bodies, baked at ingest time.**  An iching chunk's
  body holds the three-layer markdown rendering pre-baked, so no
  read-time templating is needed.  ``Block.section_path`` carries
  human-readable navigation labels (e.g. ``["Hexagram 12 —
  Stagnation", "Cognitive: Goodhart's Law (principle)"]``) for TOC
  display and structural navigation.
- **Composes with everything.**  Search, random, notes, links,
  TOC, ranges — all work via the base RefHandler.  This handler
  adds only tradition-level overview and write-surface
  (chunk-append).

Agent usage::

    get(id='oracle:')                    — list traditions in corpus
    get(id='oracle:iching')              — I-Ching tradition overview
    get(id='oracle:iching/toc')          — list all 64 hexagrams
    get(id='oracle:iching~12')           — hexagram 12 (chunk 12)
    get(id='oracle:iching~12..16')       — range
    search(q='waiting', type='oracle')   — across all traditions
    search(q='waiting', type='oracle', tag='i-ching')
    random:?corpus=oracle&tag=stoic      — one stoic chunk
    random:?corpus=oracle&not-tag=built-in — personal-only

    put(type='oracle', tradition='personal', text='...', title='...')
        — append a chunk to oracle:personal (creates ref if missing)
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import ClassVar

from precis.handlers._ref_base import RefHandler, _get_store, _parse_tags
from precis.protocol import ErrorCode, PrecisError, extract_kwargs

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(UTC).replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_meta(ref: dict) -> dict:
    raw = ref.get("meta") or ref.get("metadata") or {}
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {}


# ─────────────────────────────────────────────────────────────────────


class OracleHandler(RefHandler):
    """Handler for the ``oracle:`` scheme — paper-shaped wisdom corpus.

    See module docstring for design.  Inherits TOC, chunks, ranges,
    summary, search, links, notes from :class:`RefHandler`; adds:

    - Tradition-level overview (``oracle:<tradition>``).
    - Corpus-level listing (``oracle:`` or ``oracle:/``).
    - Chunk-append write surface (``put(type='oracle', ...)``).
    """

    scheme = "oracle"
    writable = True
    corpus_id = "oracle"
    views = {
        **RefHandler.views,
        "by-tradition": "_read_by_tradition_view",
    }
    extensions: ClassVar[set[str]] = set()

    _ref_noun = "tradition"
    _ref_emoji = "🔮"
    _slug_prefix = "oracle"

    # ── Read dispatch ────────────────────────────────────────────────

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

        # Bare path → list traditions.
        if not path or path == "/":
            if not view and not selector and not query:
                return self._list_traditions(store)

        # Bare-path view shortcuts.
        if path in ("/by-tradition", "by-tradition") or view == "by-tradition":
            return self._list_traditions(store)

        # ``/recent`` parity with state-backed kinds (memory, todo,
        # quest, skill, flashcard) — every one of those accepts
        # ``/recent`` so an agent that learned the pattern from one
        # shouldn't fail with PARAM_INVALID on oracle.  Oracle has no
        # draw history (review 2026-04-25 finding D12), so we fall
        # through to the tradition listing with a one-line note up top.
        if path in ("/recent", "recent") or view == "recent":
            return (
                "Oracle draws aren't tracked \u2014 each "
                "``random:?corpus=oracle`` call is independent.  Showing "
                "available traditions instead.\n\n"
                + self._list_traditions(store)
            )

        return super().read(
            path, selector, view, subview, query, summarize, depth, page, **kwargs
        )

    def _read_by_tradition_view(
        self, store, ref, selector, subview, **kwargs,
    ) -> str:
        extract_kwargs(kwargs, (), context="oracle/by-tradition")
        return self._list_traditions(store)

    # ── Tradition-level overview (oracle:<slug>) ─────────────────────

    def _read_overview(self, store, ref: dict) -> str:
        slug = ref.get("slug", "???")
        title = ref.get("title", "")
        tags = _parse_tags(ref) or []
        meta = _parse_meta(ref)

        # Count chunks in this ref (== entries in the tradition).
        n_entries = self._count_chunks(store, slug)

        lines: list[str] = []
        lines.append(f"🔮 {slug} — {title}")
        if n_entries:
            lines.append(f"   {n_entries} entries")
        lines.append("")

        description = (meta.get("description") or "").strip()
        if description:
            lines.append(description)
            lines.append("")

        if tags:
            tag_str = "  ".join(f"#{t}" for t in tags if t != "oracle")
            if tag_str:
                lines.append(f"Tags: {tag_str}")
                lines.append("")

        # Show a few example chunks if there's room.
        if 0 < n_entries <= 5:
            lines.append("Entries:")
            for chunk in self._first_n_chunks(store, slug, 5):
                idx = chunk.get("block_index", "?")
                section_path = chunk.get("section_path") or "[]"
                try:
                    sp = json.loads(section_path) if isinstance(section_path, str) else section_path
                except (TypeError, ValueError):
                    sp = []
                label = sp[0] if sp else (chunk.get("text", "") or "")[:60]
                lines.append(f"  {idx:>3}. {label}")
            lines.append("")
        elif n_entries > 5:
            lines.append("Sample entries:")
            for chunk in self._first_n_chunks(store, slug, 3):
                idx = chunk.get("block_index", "?")
                section_path = chunk.get("section_path") or "[]"
                try:
                    sp = json.loads(section_path) if isinstance(section_path, str) else section_path
                except (TypeError, ValueError):
                    sp = []
                label = sp[0] if sp else (chunk.get("text", "") or "")[:60]
                lines.append(f"  {idx:>3}. {label}")
            lines.append(f"  … and {n_entries - 3} more")
            lines.append("")

        lines.append("Browse:")
        lines.append(f"  get(id='{slug}/toc')          — list every entry")
        lines.append(f"  get(id='{slug}~0')            — read first entry")
        if n_entries > 1:
            lines.append(
                f"  get(id='{slug}~0..{min(n_entries - 1, 9)}') "
                "         — range"
            )
        tradition_tag = next(
            (t for t in tags if t not in {"oracle", "built-in"}), None,
        )
        if tradition_tag:
            lines.append(
                f"  get(id='random:?corpus=oracle&tag={tradition_tag}') "
                "— sample one"
            )
        lines.append(
            f"  search(q='…', type='oracle', "
            f"tag='{tradition_tag or '<tradition>'}')"
        )
        return "\n".join(lines)

    # ── Corpus-level listing (oracle:/) ──────────────────────────────

    def _list_traditions(self, store) -> str:
        refs = self._query_corpus_refs(store)
        if not refs:
            return (
                "🔮 Oracle corpus is empty.\n\n"
                "Built-in starter papers can be loaded with the\n"
                "`precis-ingest-oracle` CLI (see precis-mcp's\n"
                "data/oracle/ directory for the bundled set).\n\n"
                "Or write your own:\n"
                "  put(type='oracle', tradition='personal', "
                "title='knuth-on-optimisation',\n"
                "      text='Premature optimisation is the root of all evil.')"
            )

        lines = [f"🔮 Oracle — {len(refs)} traditions", ""]
        # Sort by slug for stable order; group built-ins separately.
        builtin: list[tuple[str, str, int]] = []
        personal: list[tuple[str, str, int]] = []
        for r in refs:
            slug = r.get("slug", "?")
            title = r.get("title", "")
            tags = _parse_tags(r) or []
            n = self._count_chunks(store, slug)
            row = (slug, title, n)
            if "built-in" in tags:
                builtin.append(row)
            else:
                personal.append(row)
        builtin.sort()
        personal.sort()

        if builtin:
            lines.append("## Built-in")
            for slug, title, n in builtin:
                short = slug.split(":", 1)[-1]
                entries = f"{n} entr{'y' if n == 1 else 'ies'}"
                lines.append(f"  {short:<22}  {title:<26}  {entries}")
            lines.append("")
        if personal:
            lines.append("## Personal")
            for slug, title, n in personal:
                short = slug.split(":", 1)[-1]
                entries = f"{n} entr{'y' if n == 1 else 'ies'}"
                lines.append(f"  {short:<22}  {title:<26}  {entries}")
            lines.append("")

        lines.append("Next:")
        lines.append("  get(id='oracle:<tradition>')         — open a tradition")
        lines.append("  get(id='random:?corpus=oracle')      — sample one entry")
        lines.append(
            "  get(id='random:?corpus=oracle&not-tag=built-in') "
            "— personal only"
        )
        lines.append("  search(q='…', type='oracle')         — full-corpus search")
        return "\n".join(lines)

    # ── Write surface ────────────────────────────────────────────────

    def put(
        self,
        path: str,
        selector: str | None,
        text: str,
        mode: str,
        **kwargs,
    ) -> str:
        store = _get_store()

        if mode in ("append", "add", "create"):
            return self._append_entry(store, path, text, **kwargs)
        if mode == "delete":
            return self._delete_entry(store, path, selector)
        return super().put(path, selector, text, mode, **kwargs)

    def _append_entry(
        self, store, path: str, text: str, **kwargs,
    ) -> str:
        """Append a chunk to an oracle paper.

        Resolution:

        - If ``path`` (i.e. the id arg minus the ``oracle:`` prefix) is
          a tradition slug like ``personal``, append to that ref.  If
          the ref doesn't exist, create it.
        - If ``kwargs['tradition']`` is given and ``path`` is empty,
          use that.
        - Otherwise default to ``personal``.

        Per-chunk meta:

        - ``title``           → used as the chunk's section_path[0]
        - ``original`` /
          ``pinyin`` / ``source`` / ``lang`` / ``cognitive`` etc. →
          appended to the chunk body as a structured tail block
          (rendering them as YAML-style trailing fields keeps them
          searchable and visible without a Block.meta column).
        """
        if not text:
            raise PrecisError(
                ErrorCode.PARAM_INVALID,
                "oracle: text= required for append",
            )

        tradition = (
            (path.strip("/") if path else None)
            or (kwargs.get("tradition") or "").strip()
            or "personal"
        )
        if tradition.startswith("oracle:"):
            tradition = tradition[len("oracle:"):]
        slug = f"oracle:{tradition}"

        title = (kwargs.get("title") or "").strip()
        chunk_label = title or (text.splitlines()[0][:80] if text else f"entry-{tradition}")

        # Compose chunk body: text + optional structured tail.
        chunk_body = text.strip()
        tail_lines: list[str] = []
        for key in ("original", "pinyin", "source", "lang", "tradition"):
            val = kwargs.get(key)
            if val:
                tail_lines.append(f"_{key}_: {val}")
        if tail_lines:
            chunk_body = f"{chunk_body}\n\n" + "\n".join(tail_lines)

        section_path = json.dumps([chunk_label])

        # Ensure the parent ref exists; create with paper-level metadata
        # on first chunk.
        existing_ref = store.get(slug)
        ref_tags = list(kwargs.get("tags") or [])
        if "oracle" not in ref_tags:
            ref_tags.insert(0, "oracle")
        # If the tradition is one of the built-in slugs we ship, the
        # ingest CLI will tag with 'built-in'; user-write puts default
        # to no built-in tag.

        try:
            from acatome_store.models import Block, Ref
            from sqlalchemy import select

            with store._Session() as session:
                if existing_ref is None:
                    paper_title = (
                        kwargs.get("paper_title")
                        or kwargs.get("ref_title")
                        or tradition.replace("-", " ").title()
                    )
                    ref_meta = {
                        "created_at": _now_iso(),
                        "tradition": tradition,
                    }
                    description = (kwargs.get("description") or "").strip()
                    if description:
                        ref_meta["description"] = description
                    store.create_ref(
                        slug=slug,
                        corpus_id=self.corpus_id,
                        title=paper_title,
                        metadata=ref_meta,
                        tags=ref_tags,
                        blocks=[],
                    )

                # Re-fetch the ref id (create_ref committed already).
                ref_row = session.execute(
                    select(Ref).where(Ref.slug == slug)
                ).scalar_one_or_none()
                if ref_row is None:
                    raise PrecisError(
                        ErrorCode.ID_NOT_FOUND,
                        f"oracle: failed to load ref after create: {slug}",
                    )
                ref_id = ref_row.id

                # Determine next block_index for this ref.
                existing_blocks = session.execute(
                    select(Block)
                    .where(Block.ref_id == ref_id)
                    .where(Block.profile == "default")
                ).scalars().all()
                next_idx = (
                    max((b.block_index or 0) for b in existing_blocks) + 1
                    if existing_blocks else 0
                )

                node_id = f"{slug}-b{next_idx:04d}"
                block = Block(
                    node_id=node_id,
                    profile="default",
                    ref_id=ref_id,
                    page=0,
                    block_index=next_idx,
                    block_type="text",
                    text=chunk_body,
                    section_path=section_path,
                )
                session.add(block)
                session.commit()
        except ImportError as exc:
            raise PrecisError(
                ErrorCode.KIND_UNAVAILABLE,
                "oracle: acatome-store not installed.  Install with "
                "`pip install precis-mcp[paper]`.",
            ) from exc

        return (
            f"🔮 oracle: appended to {slug} as chunk ›{next_idx}\n"
            f"   {chunk_label}\n\n"
            f"Next:\n"
            f"  get(id='{slug}~{next_idx}')         — read this entry\n"
            f"  get(id='{slug}/toc')                — full tradition TOC"
        )

    def _delete_entry(
        self, store, path: str, selector: str | None,
    ) -> str:
        """Soft-delete a chunk by emptying its text + flagging in
        section_path.  No physical row removal — keeps history.

        Implemented as ref-level soft-delete for now (mirrors
        memory:'s pattern); per-chunk soft-delete is a v1.1 follow-up.
        """
        if not path:
            raise PrecisError(
                ErrorCode.PARAM_INVALID,
                "oracle: id= required for delete",
            )
        slug = path if path.startswith("oracle:") else f"oracle:{path}"
        ref = store.get(slug)
        if ref is None:
            raise PrecisError(
                ErrorCode.ID_NOT_FOUND,
                f"oracle: no ref '{slug}'",
            )
        if selector:
            return (
                f"oracle: per-chunk delete not yet supported (got "
                f"selector ›{selector}).  Edit the chunk via "
                f"put(mode='replace') or contact dev for a v1.1 path."
            )
        meta = _parse_meta(ref)
        meta["deleted"] = True
        meta["deleted_at"] = _now_iso()
        store.update_ref_metadata(slug, meta, merge=True)
        return f"🔮 oracle: tradition soft-deleted: {slug}"

    # ── Helpers ──────────────────────────────────────────────────────

    def _query_corpus_refs(self, store) -> list[dict]:
        """List all non-deleted refs in the oracle corpus."""
        try:
            refs = store.list_refs_by_corpus(self.corpus_id, limit=1000)
        except AttributeError:
            return []
        return [
            r for r in refs
            if not _parse_meta(r).get("deleted", False)
        ]

    def _count_chunks(self, store, slug: str) -> int:
        """Count blocks in a ref (= entries in the tradition)."""
        try:
            blocks = store.get_blocks(slug)
            return sum(1 for b in blocks if b.get("block_index") is not None)
        except (AttributeError, TypeError):
            return 0

    def _first_n_chunks(self, store, slug: str, n: int) -> list[dict]:
        """Pull the first N blocks of a ref, ordered by block_index."""
        try:
            blocks = store.get_blocks(slug)
            blocks = [b for b in blocks if b.get("block_index") is not None]
            blocks.sort(key=lambda b: b.get("block_index", 0))
            return blocks[:n]
        except (AttributeError, TypeError):
            return []
