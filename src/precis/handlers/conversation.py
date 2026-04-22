"""ConversationHandler — session-level transcripts with turn-per-block.

Phase 6 journal kind.  Each conversation is a ref in the
``conversations`` corpus; each turn is a block.  Supports streamed
``put(mode='append', id='conv:<session>')`` to add turns as they
happen, plus batch ingestion of full transcripts.

Design (§13 Phase 6):

- **Id format** — ISO date + short label (``conv:2026-04-21-asa-session``)
  or UUID.  The handler accepts either form; slug comes from the agent.
- **Turn-per-block** — each block is one speaker turn.  ``section_path``
  carries ``[speaker, timestamp]`` for downstream filtering.
- **Views** — standard RefHandler surface (toc, chunk, links, meta,
  summary) plus ``/recent`` (last N sessions) and ``/session/<slug>``
  (full transcript of one session).  The ``/session/<slug>`` form is a
  convenience for agents that know the slug but want the transcript
  rendered cleanly rather than via chunk selectors.

URI scheme: ``conversation:``.  Corpus: ``conversations``.

Agent usage::

    # Start a new session
    put(type='conversation', id='conv:2026-04-21-asa',
        text='user: Hello\\nasa: Hi there')

    # Append a turn
    put(id='conv:2026-04-21-asa', text='user: how do I X?', mode='append')

    # Read
    get(type='conversation', id='/recent')
    get(id='conv:2026-04-21-asa')                  # overview
    get(id='conv:2026-04-21-asa/session')          # full transcript
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from precis.handlers._ref_base import RefHandler, _get_store
from precis.protocol import ErrorCode, PrecisError, extract_kwargs
from precis.uri import SEP

log = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _parse_meta(ref: dict) -> dict:
    raw = ref.get("meta") or ref.get("metadata") or {}
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {}


def _fmt_ts(raw: str | None) -> str:
    if not raw:
        return "—"
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
        return dt.strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return raw


class ConversationHandler(RefHandler):
    """Handler for the ``conversation:`` scheme."""

    scheme = "conversation"
    writable = True
    corpus_id = "conversations"
    views = {
        **RefHandler.views,
        "recent": "_read_recent_view",
        "session": "_read_session_view",
    }
    extensions: set[str] = set()

    _ref_noun = "conversation"
    _ref_emoji = "💬"

    # ── Read surface ─────────────────────────────────────────────────

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

        if not path or path == "/":
            if not view and not selector and not query:
                return self._list_overview(store)

        if path in ("/recent", "recent") or view == "recent":
            try:
                limit = int(kwargs.get("top_k") or 20)
            except (TypeError, ValueError):
                limit = 20
            return self._read_recent(store, limit=limit)

        return super().read(
            path, selector, view, subview, query, summarize, depth, page, **kwargs
        )

    def _read_session_view(self, store, ref, selector, subview, **kwargs) -> str:
        extract_kwargs(kwargs, (), context="conversation/session")
        return self._read_session(store, ref)

    def _read_recent_view(self, store, ref, selector, subview, **kwargs) -> str:
        extract_kwargs(kwargs, (), context="conversation/recent")
        return self._read_recent(store)

    def _read_overview(self, store, ref: dict) -> str:
        slug = ref.get("slug", "???")
        title = ref.get("title", "")
        meta = _parse_meta(ref)

        try:
            blocks = store.get_blocks(slug, block_type="text")
        except Exception:
            blocks = []
        turn_count = len(blocks)

        lines: list[str] = []
        lines.append(f"💬 {slug}  ({turn_count} turn{'s' if turn_count != 1 else ''})")
        if title:
            lines.append(f"   {title}")
        created = ref.get("first_seen_at") or meta.get("created_at")
        if created:
            lines.append(f"   started: {_fmt_ts(str(created))}")
        updated = meta.get("updated_at")
        if updated and updated != created:
            lines.append(f"   last turn: {_fmt_ts(str(updated))}")
        tags = ref.get("tags") or []
        if tags:
            lines.append(f"   tags: {', '.join(tags)}")
        lines.append("")

        # Preview first and last turns to anchor the transcript.
        if blocks:
            first = (blocks[0].get("text") or "").strip().splitlines()[:1]
            last = (blocks[-1].get("text") or "").strip().splitlines()[:1]
            if first:
                lines.append(f"  first: {first[0][:120]}")
            if turn_count > 1 and last:
                lines.append(f"  last:  {last[0][:120]}")
            lines.append("")

        lines.append("Next:")
        lines.append(f"  get(id='{slug}/session')       — full transcript")
        lines.append(f"  get(id='{slug}{SEP}0..10')      — first 10 turns")
        lines.append(f"  put(id='{slug}', text='…', mode='append')  — add a turn")
        return "\n".join(lines)

    def _list_overview(self, store) -> str:
        refs = self._query_corpus_refs(store)
        if not refs:
            return (
                "💬 No conversations yet.\n\n"
                "Create one:\n"
                "  put(id='conv:<session>', text='…', mode='append')\n"
            )

        lines = [f"💬 {len(refs)} conversations"]
        lines.append("")
        lines.append("Recent (top 5):")
        for r in refs[:5]:
            lines.append(self._list_entry(r))
        lines.append("")
        lines.append("Next:")
        lines.append("  get(id='conversation:/recent')    — last 20")
        lines.append("  search(query='…', type='conversation')")
        return "\n".join(lines)

    def _list_entry(self, ref: dict) -> str:
        slug = ref.get("slug", "???")
        title = ref.get("title") or ""
        created = ref.get("first_seen_at") or "?"
        turns = ref.get("_turn_count")
        tcount = f"  [{turns} turn{'s' if turns != 1 else ''}]" if turns else ""
        return f"  {_fmt_ts(str(created))}  {slug}{tcount}  {title[:80]}"

    def _list_header(self, count: int, grep: str = "") -> str:
        extra = f" (grep={grep!r})" if grep else ""
        return f"💬 {count} conversations{extra}"

    # ── /recent ──────────────────────────────────────────────────────

    def _read_recent(self, store, *, limit: int = 20) -> str:
        refs = self._query_corpus_refs(store)
        if not refs:
            return "💬 No conversations yet."
        recent = refs[:limit]
        lines = [f"💬 {len(recent)} recent conversations (of {len(refs)} total)"]
        lines.append("")
        for r in recent:
            lines.append(self._list_entry(r))
        return "\n".join(lines)

    # ── /session view — full transcript rendering ───────────────────

    def _read_session(self, store, ref: dict) -> str:
        """Render the full transcript, one turn per paragraph.

        Each block's ``section_path`` holds ``[speaker, timestamp]``
        when available; we surface them as ``speaker (ts):`` headers.
        """
        slug = ref.get("slug", "???")
        try:
            blocks = store.get_blocks(slug, block_type="text")
        except Exception as exc:
            raise PrecisError(
                ErrorCode.UPSTREAM_ERROR,
                f"conversation: could not fetch turns for {slug}: {exc}",
            ) from exc

        if not blocks:
            return f"💬 {slug} has no turns yet."

        lines = [f"💬 {slug}  ({len(blocks)} turns)"]
        lines.append("")
        for b in blocks:
            sp_raw = b.get("section_path") or "[]"
            try:
                sp = json.loads(sp_raw) if isinstance(sp_raw, str) else sp_raw
            except (TypeError, ValueError):
                sp = []
            speaker = sp[0] if sp else ""
            ts = sp[1] if len(sp) > 1 else ""
            header_bits = [x for x in (speaker, ts) if x]
            header = f"{' · '.join(header_bits)}:" if header_bits else ""
            if header:
                lines.append(header)
            text = (b.get("text") or "").strip()
            if text:
                lines.append(text)
            lines.append("")
        return "\n".join(lines).rstrip()

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

        if mode in ("append", "add"):
            return self._append_turn(store, path, text, **kwargs)

        if mode == "delete":
            return self._delete_conversation(store, path)

        return super().put(path, selector, text, mode, **kwargs)

    def _append_turn(self, store, path: str, text: str, **kwargs) -> str:
        """Append a turn, creating the conversation ref if new.

        The first call for a given slug creates the ref with the turn
        as its first block; subsequent calls append additional blocks
        to the same ref.  This matches the streaming use case where
        turns arrive one at a time.
        """
        if not text:
            raise PrecisError(
                ErrorCode.PARAM_INVALID,
                "conversation: text= required for append",
            )
        if not path:
            raise PrecisError(
                ErrorCode.PARAM_INVALID,
                "conversation: id= required (e.g. id='conv:2026-04-21-session-name')",
            )
        slug = path if ":" in path else f"conv:{path}"

        speaker = (kwargs.get("speaker") or "").strip()
        ts = kwargs.get("timestamp") or _now().strftime("%Y-%m-%dT%H:%M:%SZ")
        section_path = [x for x in (speaker, ts) if x]

        ref = store.get(slug)
        if ref is None:
            # First turn → create the ref.
            title = (kwargs.get("title") or "").strip()
            tags = kwargs.get("tags") or []
            if isinstance(tags, str):
                tags = [t.strip() for t in tags.split(",") if t.strip()]
            blocks = [
                {
                    "text": text,
                    "block_type": "text",
                    "section_path": section_path,
                }
            ]
            metadata = {"created_at": ts, "updated_at": ts}
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
                raise PrecisError(
                    ErrorCode.ID_AMBIGUOUS,
                    f"conversation: could not create '{slug}': {exc}",
                ) from exc
            return (
                f"💬 Conversation started: {slug}\n"
                f"(Turn 1 added; append more with "
                f"put(id='{slug}', text='…', mode='append').)"
            )

        # Existing ref → append a block.
        try:
            from acatome_store.models import Block, Ref
            from sqlalchemy import func, select

            with store._Session() as session:
                ref_row = session.execute(
                    select(Ref).where(Ref.slug == slug)
                ).scalar_one_or_none()
                if ref_row is None:
                    raise PrecisError(
                        ErrorCode.ID_NOT_FOUND,
                        f"conversation: no ref '{slug}'",
                    )
                # Next block_index = current max + 1.
                max_idx = session.execute(
                    select(func.max(Block.block_index)).where(
                        Block.ref_id == ref_row.id
                    )
                ).scalar_one()
                next_idx = (max_idx or 0) + 1
                block = Block(
                    node_id=f"{slug}-b{next_idx:04d}",
                    profile="default",
                    ref_id=ref_row.id,
                    page=0,
                    block_index=next_idx,
                    block_type="text",
                    text=text,
                    section_path=json.dumps(section_path),
                )
                session.add(block)
                session.commit()
        except ImportError as exc:
            raise PrecisError(
                ErrorCode.KIND_UNAVAILABLE,
                "conversation: acatome-store not installed.  "
                "Install with `pip install precis-mcp[paper]`.",
            ) from exc

        # Bump the updated_at timestamp.
        meta = _parse_meta(ref)
        meta["updated_at"] = ts
        store.update_ref_metadata(slug, meta, merge=True)

        return f"💬 Turn appended to {slug} (turn #{next_idx + 1})."

    def _delete_conversation(self, store, path: str) -> str:
        if not path:
            raise PrecisError(
                ErrorCode.PARAM_INVALID,
                "conversation: id= required for delete",
            )
        slug = path if ":" in path else f"conv:{path}"
        ref = store.get(slug)
        if ref is None:
            raise PrecisError(
                ErrorCode.ID_NOT_FOUND,
                f"conversation: no ref '{slug}'",
            )
        meta = _parse_meta(ref)
        meta["deleted"] = True
        meta["deleted_at"] = _now().strftime("%Y-%m-%dT%H:%M:%SZ")
        store.update_ref_metadata(slug, meta, merge=True)
        return f"💬 Conversation soft-deleted: {slug}"

    # ── Corpus query ────────────────────────────────────────────────

    def _query_corpus_refs(self, store) -> list[dict]:
        """Return all non-deleted conversations, newest-updated first."""
        try:
            from acatome_store.models import Block, Ref
            from sqlalchemy import func, select

            with store._Session() as session:
                stmt = (
                    select(
                        Ref,
                        func.count(Block.id).label("turn_count"),
                    )
                    .outerjoin(Block, Block.ref_id == Ref.id)
                    .where(Ref.corpus_id == self.corpus_id)
                    .group_by(Ref.id)
                    .order_by(Ref.first_seen_at.desc())
                )
                rows = session.execute(stmt).all()
                results = []
                for ref, turn_count in rows:
                    d = ref.to_dict()
                    meta = _parse_meta(d)
                    if meta.get("deleted"):
                        continue
                    d["_turn_count"] = turn_count or 0
                    d["tags"] = [t.name for t in ref.tags] if ref.tags else []
                    results.append(d)
                return results
        except ImportError as exc:
            raise PrecisError(
                ErrorCode.KIND_UNAVAILABLE,
                "conversation: acatome-store not installed.  "
                "Install with `pip install precis-mcp[paper]`.",
            ) from exc
