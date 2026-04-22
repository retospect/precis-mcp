"""MemoryHandler — verbatim agent-memory drawers with tags.

Phase 6 journal kind.  Slug-based ids, semantic search via pgvector
(when the store's embedding index is populated), plus ``/recent`` and
``/tags`` convenience views.

Design (§7.1 / §13 Phase 6):

- **Flat** — no wings / rooms / folder hierarchy.  One corpus, many
  memories, tag filtering handles grouping.
- **Verbatim content** — the agent writes a drawer once; future reads
  return exactly what was written (modulo soft-delete).  Compare to
  ``fc:`` which stores SM-2 scheduling state.
- **Slug-based ids** — auto-derived from title via :func:`_slugify`,
  explicitly settable via ``put(id='memory:my-slug', ...)``.  This is
  different from ``todo:`` and ``fc:`` which use integer ``bigserial``.

URI scheme: ``memory:``.  Corpus: ``memories``.

Agent usage::

    put(type='memory', text='The cluster DB user is cluster_app', title='cluster db user')
    get(type='memory', id='/recent')
    get(id='memory:cluster-db-user')
    get(id='memory:/tags')                  # tag histogram
    search(query='database', type='memory') # grep across memories
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime

from precis.handlers._ref_base import RefHandler, _get_store
from precis.protocol import ErrorCode, PrecisError, extract_kwargs
from precis.uri import SEP

log = logging.getLogger(__name__)


# ── Slug derivation ───────────────────────────────────────────────────


def _slugify(title: str, *, prefix: str = "memory") -> str:
    """Turn a free-text title into a stable ``memory:<slug>`` id.

    Mirrors the flashcard slugifier but emits the ``memory:`` prefix.
    Returns the empty string if the title contains no slug-safe chars.
    """
    body = title.lower().strip()
    body = re.sub(r"[^a-z0-9]+", "-", body)
    body = body.strip("-")[:60]
    return f"{prefix}:{body}" if body else ""


def _parse_meta(ref: dict) -> dict:
    """Return the JSON-parsed ``meta`` dict for a ref (defensive)."""
    raw = ref.get("meta") or ref.get("metadata") or {}
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {}


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _fmt_ts(raw: str | None) -> str:
    """Pretty-print an ISO timestamp as ``YYYY-MM-DD HH:MM``.

    Returns the original string on parse failure so we never lose
    agent-visible data to a formatting quirk.
    """
    if not raw:
        return "—"
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
        return dt.strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return raw


# ─────────────────────────────────────────────────────────────────────


class MemoryHandler(RefHandler):
    """Handler for the ``memory:`` scheme.

    See module docstring for design rationale.  Inherits most of the
    read surface (TOC, chunks, links, grep, semantic search) from
    :class:`RefHandler` and adds two views: ``/recent`` and ``/tags``.
    """

    scheme = "memory"
    writable = True
    corpus_id = "memories"
    views = {
        **RefHandler.views,
        "recent": "_read_recent_view",
        "tags": "_read_tags_view",
    }
    extensions: set[str] = set()

    _ref_noun = "memory"
    _ref_emoji = "🧠"
    _slug_prefix = "memory"

    # ── Subclass hooks ───────────────────────────────────────────────

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
        """Dispatch ``/recent`` and ``/tags`` as bare-path views.

        ``memory:/recent`` and ``memory:/tags`` reach here with the
        view portion parsed into ``path='/recent'`` by the URI layer
        (leading slash means "no id, view only").  Route those before
        handing off to the generic RefHandler read pipeline.
        """
        store = _get_store()

        if not path or path == "/":
            if not view and not selector and not query:
                return self._list_overview(store)

        if path in ("/recent", "recent") or view == "recent":
            limit_raw = kwargs.get("top_k") or 20
            try:
                limit = int(limit_raw)
            except (TypeError, ValueError):
                limit = 20
            return self._read_recent(store, limit=limit)

        if path in ("/tags", "tags") or view == "tags":
            return self._read_tags(store)

        return super().read(
            path, selector, view, subview, query, summarize, depth, page, **kwargs
        )

    def _read_recent_view(self, store, ref, selector, subview, **kwargs) -> str:
        extract_kwargs(kwargs, (), context="memory/recent")
        return self._read_recent(store)

    def _read_tags_view(self, store, ref, selector, subview, **kwargs) -> str:
        extract_kwargs(kwargs, (), context="memory/tags")
        return self._read_tags(store)

    def _read_overview(self, store, ref: dict) -> str:
        slug = ref.get("slug", "???")
        title = ref.get("title", "")
        meta = _parse_meta(ref)
        tags = ref.get("tags") or []

        lines: list[str] = []
        lines.append(f"🧠 {slug}")
        if title:
            lines.append(f"   {title}")
        if tags:
            lines.append(f"   tags: {', '.join(tags)}")
        created = ref.get("first_seen_at") or meta.get("created_at")
        if created:
            lines.append(f"   created: {_fmt_ts(str(created))}")
        if meta.get("deleted"):
            lines.append("   [deleted]")
        lines.append("")

        # Content preview — first block text if present.
        try:
            blocks = store.get_blocks(slug, block_type="text")
        except Exception:
            blocks = []
        if blocks:
            preview = (blocks[0].get("text") or "").strip()
            if preview:
                lines.append(preview)
                lines.append("")

        lines.append("Next:")
        lines.append(f"  get(id='{slug}{SEP}0..5')   — read in chunks")
        lines.append(f"  get(id='{slug}/links')    — outbound links")
        lines.append(f"  put(id='{slug}', text='…', mode='replace')  — rewrite content")
        lines.append(f"  put(id='{slug}', link='<target>:references')")
        return "\n".join(lines)

    def _list_overview(self, store) -> str:
        """Top-level ``memory:`` overview — recent + tag histogram."""
        refs = self._query_corpus_refs(store)
        if not refs:
            return (
                "🧠 No memories yet.\n\n"
                "Create one:\n"
                "  put(type='memory', text='…', title='…')\n"
                "  put(id='memory:<slug>', text='…', mode='append')\n"
            )

        lines = [f"🧠 {len(refs)} memories"]
        lines.append("")
        lines.append("Recent (top 5):")
        for r in refs[:5]:
            lines.append(self._list_entry(r))
        lines.append("")

        # Quick tag histogram.
        tag_counts: dict[str, int] = {}
        for r in refs:
            for t in r.get("tags") or []:
                tag_counts[t] = tag_counts.get(t, 0) + 1
        if tag_counts:
            top = sorted(tag_counts.items(), key=lambda kv: -kv[1])[:8]
            lines.append("Tags:  " + ", ".join(f"{t}({n})" for t, n in top))
            lines.append("")

        lines.append("Next:")
        lines.append("  get(id='memory:/recent')   — last 20")
        lines.append("  get(id='memory:/tags')     — full tag histogram")
        lines.append("  search(query='…', type='memory')")
        return "\n".join(lines)

    def _list_entry(self, ref: dict) -> str:
        slug = ref.get("slug", "???")
        title = ref.get("title") or ""
        tags = ref.get("tags") or []
        tag_str = f"  [{', '.join(tags)}]" if tags else ""
        created = ref.get("first_seen_at") or "?"
        return f"  {_fmt_ts(str(created))}  {slug}{tag_str}  {title}"

    def _list_header(self, count: int, grep: str = "") -> str:
        extra = f" (grep={grep!r})" if grep else ""
        return f"🧠 {count} memories{extra}"

    # ── /recent view ────────────────────────────────────────────────

    def _read_recent(self, store, *, limit: int = 20) -> str:
        """List the N most recently created memories."""
        refs = self._query_corpus_refs(store)
        if not refs:
            return "🧠 No memories yet."
        recent = refs[:limit]
        lines = [f"🧠 {len(recent)} recent memories (of {len(refs)} total)"]
        lines.append("")
        for r in recent:
            lines.append(self._list_entry(r))
        return "\n".join(lines)

    # ── /tags view ──────────────────────────────────────────────────

    def _read_tags(self, store) -> str:
        """Histogram of tags across all non-deleted memories."""
        refs = self._query_corpus_refs(store)
        counts: dict[str, int] = {}
        for r in refs:
            for t in r.get("tags") or []:
                counts[t] = counts.get(t, 0) + 1
        if not counts:
            return "🧠 No tagged memories yet."
        lines = [f"🧠 tags ({len(counts)} distinct)"]
        lines.append("")
        for tag, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {n:>3}  {tag}")
        return "\n".join(lines)

    # ── Write surface ───────────────────────────────────────────────

    def put(
        self,
        path: str,
        selector: str | None,
        text: str,
        mode: str,
        **kwargs,
    ) -> str:
        store = _get_store()

        # Creation path: put(type='memory', text='…', title='…', mode='append')
        if mode in ("append", "add", "create"):
            return self._create_memory(store, path, text, **kwargs)

        if mode == "replace":
            return self._replace_memory(store, path, text, **kwargs)

        if mode == "delete":
            return self._delete_memory(store, path)

        # Fall through to RefHandler for note (annotation) + link handling.
        return super().put(path, selector, text, mode, **kwargs)

    def _create_memory(self, store, path: str, text: str, **kwargs) -> str:
        if not text:
            raise PrecisError(
                ErrorCode.PARAM_INVALID,
                "memory: text= required for creation",
            )
        title = (kwargs.get("title") or "").strip()
        tags = kwargs.get("tags") or []
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]

        # If the agent supplied an explicit slug in the path, honour it;
        # otherwise derive from title, falling back to the first line of
        # text when title is empty.
        if path:
            slug = path if path.startswith("memory:") else f"memory:{path}"
        else:
            source = title or text.splitlines()[0] if text else ""
            slug = _slugify(source)
            if not slug:
                raise PrecisError(
                    ErrorCode.PARAM_INVALID,
                    "memory: could not derive a slug from the provided "
                    "title/text (all characters stripped). "
                    "Supply title='…' or id='memory:<slug>'.",
                )

        blocks = [{"text": text, "block_type": "text", "section_path": []}]
        metadata = {"created_at": _now().strftime("%Y-%m-%dT%H:%M:%SZ")}

        try:
            store.create_ref(
                slug=slug,
                corpus_id=self.corpus_id,
                title=title or text.splitlines()[0][:120],
                metadata=metadata,
                tags=tags if tags else None,
                blocks=blocks,
            )
        except ValueError as exc:
            # Includes "slug already exists"; surface as ID_AMBIGUOUS so
            # the agent can retry with a different slug or switch to
            # mode='replace'.
            raise PrecisError(
                ErrorCode.ID_AMBIGUOUS,
                f"memory: could not create '{slug}': {exc}. "
                f"If this slug already exists, use put(mode='replace').",
            ) from exc

        return (
            f"🧠 Memory created: {slug}\n"
            f"Next:\n"
            f"  get(id='{slug}')               — read back\n"
            f"  put(id='{slug}', link='<target>:references')  — link to another ref"
        )

    def _replace_memory(self, store, path: str, text: str, **kwargs) -> str:
        if not path:
            raise PrecisError(
                ErrorCode.PARAM_INVALID,
                "memory: id= required for replace",
            )
        slug = path if path.startswith("memory:") else f"memory:{path}"
        ref = store.get(slug)
        if ref is None:
            raise PrecisError(
                ErrorCode.ID_NOT_FOUND,
                f"memory: no ref '{slug}'. Use put(mode='append') to create a new one.",
            )
        if not text:
            raise PrecisError(
                ErrorCode.PARAM_INVALID,
                "memory: text= required for replace",
            )

        # Overwrite the first block; remove subsequent blocks.  We use
        # the store's direct SQLAlchemy surface since acatome-store
        # doesn't expose a "replace blocks" helper.
        try:
            from acatome_store.models import Block, Ref
            from sqlalchemy import delete as sa_delete
            from sqlalchemy import select

            with store._Session() as session:
                ref_row = session.execute(
                    select(Ref).where(Ref.slug == slug)
                ).scalar_one_or_none()
                if ref_row is None:
                    raise PrecisError(
                        ErrorCode.ID_NOT_FOUND,
                        f"memory: no ref '{slug}'",
                    )
                session.execute(sa_delete(Block).where(Block.ref_id == ref_row.id))
                block = Block(
                    node_id=f"{slug}-b0000",
                    profile="default",
                    ref_id=ref_row.id,
                    page=0,
                    block_index=0,
                    block_type="text",
                    text=text,
                    section_path=json.dumps([]),
                )
                session.add(block)
                session.commit()
        except ImportError as exc:
            raise PrecisError(
                ErrorCode.KIND_UNAVAILABLE,
                "memory: acatome-store not installed.  Install with "
                "`pip install precis-mcp[paper]`.",
            ) from exc

        # Touch the updated_at timestamp in meta.
        meta = _parse_meta(ref)
        meta["updated_at"] = _now().strftime("%Y-%m-%dT%H:%M:%SZ")
        store.update_ref_metadata(slug, meta, merge=False)

        return f"🧠 Memory replaced: {slug}"

    def _delete_memory(self, store, path: str) -> str:
        if not path:
            raise PrecisError(
                ErrorCode.PARAM_INVALID,
                "memory: id= required for delete",
            )
        slug = path if path.startswith("memory:") else f"memory:{path}"
        ref = store.get(slug)
        if ref is None:
            raise PrecisError(
                ErrorCode.ID_NOT_FOUND,
                f"memory: no ref '{slug}'",
            )
        meta = _parse_meta(ref)
        meta["deleted"] = True
        meta["deleted_at"] = _now().strftime("%Y-%m-%dT%H:%M:%SZ")
        store.update_ref_metadata(slug, meta, merge=True)
        return (
            f"🧠 Memory soft-deleted: {slug}\n"
            f"(Content preserved for audit; hidden from /recent and /tags.)"
        )

    # ── Corpus query ────────────────────────────────────────────────

    def _query_corpus_refs(self, store) -> list[dict]:
        """Return all non-deleted memories, newest first."""
        try:
            from acatome_store.models import Ref
            from sqlalchemy import select

            session_factory = store._Session
            with session_factory() as session:
                stmt = (
                    select(Ref)
                    .where(Ref.corpus_id == self.corpus_id)
                    .order_by(Ref.first_seen_at.desc())
                )
                rows = session.execute(stmt).scalars().all()
                results = []
                for r in rows:
                    d = r.to_dict()
                    meta = _parse_meta(d)
                    if meta.get("deleted"):
                        continue
                    # Enrich with tag list the same way acatome-store does
                    # elsewhere — ``r.tags`` is a relationship.
                    d["tags"] = [t.name for t in r.tags] if r.tags else []
                    results.append(d)
                return results
        except ImportError as exc:
            raise PrecisError(
                ErrorCode.KIND_UNAVAILABLE,
                "memory: acatome-store not installed.  "
                "Install with `pip install precis-mcp[paper]`.",
            ) from exc
