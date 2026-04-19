"""Flashcard handler — knowledge items with SM-2 spaced repetition.

Extends RefHandler with:
  - ``/due`` view: items due for review, with nearby almost-due items
  - ``/stats`` view: review statistics and struggle spots
  - ``mode='append'``: create a knowledge item
  - ``mode='review'``: record recall quality (0-5), update SM-2 schedule
  - ``mode='replace'``: edit item text

Knowledge items are stored as refs in the ``flashcards`` corpus.
The ``fc:`` URI scheme is used for addressing.

SM-2 scheduling state lives in ref metadata (JSON):
  easiness, interval, reps, next_review, last_reviewed, review_log[]

The agent sees the same get/put/search tools — this handler just
teaches it how to use them for flashcards via output hints.
"""

from __future__ import annotations

import json as _json
import logging
import re
from datetime import UTC, datetime

from precis.handlers._ref_base import RefHandler, _get_store, _truncate
from precis.handlers.sm2 import DEFAULT_EASINESS
from precis.handlers.sm2 import update as sm2_update
from precis.protocol import PrecisError

log = logging.getLogger(__name__)

# ── Defaults ─────────────────────────────────────────────────────────

HARD_THRESHOLD = 1.8  # easiness below this = "struggle item"
NEARBY_DUE_DAYS = 3  # surface almost-due items within this window
MAX_DUE = 20  # cap on due items returned
MAX_NEARBY = 5  # cap on nearby/almost-due


def _slugify(text: str) -> str:
    """Turn a knowledge statement into a flashcard slug.

    Extracts ASCII alphanumeric parts.  If the input is non-empty but
    contains no ASCII alphanumerics (e.g. pure kanji or emoji), falls
    back to a short SHA-256 hash for a stable, deterministic slug.
    Empty input returns an empty string (no slug).
    """
    if not text or not text.strip():
        return ""
    slug = text.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")[:60]
    if not slug:
        import hashlib

        slug = hashlib.sha256(text.encode()).hexdigest()[:12]
    return f"fc:{slug}"


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _parse_meta(ref: dict) -> dict:
    """Extract parsed metadata from a ref dict."""
    meta = ref.get("meta") or ref.get("metadata") or {}
    if isinstance(meta, str):
        try:
            meta = _json.loads(meta)
        except (ValueError, TypeError):
            meta = {}
    return meta


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _relative_due(meta: dict) -> str:
    """Human-readable due status from meta."""
    raw = meta.get("next_review")
    if not raw:
        return "new"
    try:
        due = datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
    except (ValueError, TypeError):
        return "?"
    now = _now()
    delta = (due - now).total_seconds() / 86400
    if delta < -1:
        days = int(-delta)
        return f"\u26a0 {days}d overdue"
    if delta < 0:
        return "due today"
    if delta < 1:
        return "due today"
    days = int(delta)
    return f"due in {days}d"


def _last_review_note(meta: dict) -> str | None:
    """Get the most recent review note, if any."""
    log_entries = meta.get("review_log", [])
    if not log_entries:
        return None
    last = log_entries[-1]
    return last.get("note") if isinstance(last, dict) else None


# ── Handler ──────────────────────────────────────────────────────────


class FlashcardHandler(RefHandler):
    """Handler for fc: scheme — knowledge items with spaced repetition."""

    scheme = "fc"
    writable = True
    corpus_id = "flashcards"
    views = {"meta", "summary", "toc", "chunk", "links", "due", "stats"}
    extensions: set[str] = set()

    _ref_noun = "item"
    _ref_emoji = "\U0001f9e0"  # 🧠
    _max_list = 20

    # ── Subclass hooks ───────────────────────────────────────────────

    def _dispatch_view(
        self,
        store,
        ref: dict,
        view: str | None,
        subview: str | None,
        selector: str | None,
    ) -> str | None:
        if view == "due":
            return self._read_due(store)
        if view == "stats":
            return self._read_stats(store)
        return None

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

        # Bare call: fc: with no path
        if not path and not selector:
            if view == "due":
                return self._read_due(store)
            if view == "stats":
                return self._read_stats(store)
            if query:
                return self._search_or_grep(store, query, top_k=kwargs.get("top_k", 5))
            return self._list_overview(store)

        # /due and /stats as views on any path (dispatch from bare slug)
        if view == "due":
            return self._read_due(store)
        if view == "stats":
            return self._read_stats(store)

        # Delegate everything else (single item, chunks, links, etc.) to RefHandler
        return super().read(
            path, selector, view, subview, query, summarize, depth, page, **kwargs
        )

    def _read_overview(self, store, ref: dict) -> str:
        slug = ref.get("slug", "???")
        title = ref.get("title", "")
        meta = _parse_meta(ref)
        ease = meta.get("easiness", DEFAULT_EASINESS)
        reps = meta.get("reps", 0)
        due = _relative_due(meta)
        note = _last_review_note(meta)

        blocks = store.get_blocks(slug)
        body = ""
        for b in blocks[:1]:
            body = b.get("text", "")

        lines = [f"\U0001f9e0 {slug}  {due}  ease={ease:.1f}  reps={reps}"]
        if title:
            lines.append(f"  {title}")
        if body and body != title:
            lines.append(f"  {_truncate(body, 200)}")
        if note:
            lines.append(f"  \u26a1 Last review: \"{note}\"")

        lines.append("")
        lines.append("Next:")
        lines.append(
            f"  put(id='{slug}', text='<0-5>', mode='review', "
            f"note='what happened') \u2014 record review"
        )
        lines.append(f"  put(id='{slug}', text='new text', mode='replace') \u2014 edit")
        lines.append(f"  get(id='{slug}/links') \u2014 source links")
        return "\n".join(lines)

    def _read_meta(self, ref: dict) -> str:
        meta = _parse_meta(ref)
        lines = []
        for key in ("slug", "title"):
            val = ref.get(key, "")
            if val:
                lines.append(f"  {key}: {val}")
        for key in ("easiness", "interval", "reps", "next_review", "last_reviewed"):
            val = meta.get(key)
            if val is not None:
                lines.append(f"  {key}: {val}")
        review_log = meta.get("review_log", [])
        if review_log:
            lines.append(f"  reviews: {len(review_log)}")
        return "\n".join(lines)

    def _list_header(self, count: int, grep: str = "") -> str:
        if grep:
            return f"\U0001f9e0 {count} items matching '{grep}'"
        return f"\U0001f9e0 {count} flashcard items"

    def _list_entry(self, ref: dict) -> str:
        slug = ref.get("slug", "???")
        title = _truncate(ref.get("title", ""), 60)
        meta = _parse_meta(ref)
        ease = meta.get("easiness", DEFAULT_EASINESS)
        due = _relative_due(meta)
        return f"  {slug}  {due}  ease={ease:.1f}  {title}"

    # ── List (override — query refs by corpus, not papers table) ─────

    def _list_overview(self, store) -> str:
        """Top-level fc: overview with counts and entry points."""
        refs = self._query_corpus_refs(store)
        total = len(refs)
        now = _now()
        due = [r for r in refs if self._is_due(r, now)]
        hard = [r for r in refs if _parse_meta(r).get("easiness", DEFAULT_EASINESS) < HARD_THRESHOLD]

        lines = [f"\U0001f9e0 {total} flashcard items", ""]
        if due:
            lines.append(f"  {len(due)} due now" + (f" ({sum(1 for d in due if self._overdue_days(d, now) > 0)} overdue)" if any(self._overdue_days(d, now) > 0 for d in due) else ""))
        else:
            lines.append("  None due right now")
        if hard:
            lines.append(f"  {len(hard)} items flagged as hard")

        lines.append("")
        lines.append("Next:")
        lines.append("  get(id='fc:/due')                \u2014 items to review now")
        lines.append("  get(id='fc:/stats')              \u2014 review statistics & struggle spots")
        lines.append("  search(query='...', scope='fc:') \u2014 find items by topic")
        lines.append(
            "  put(id='fc:', text='Paris is the capital of France', mode='append') \u2014 create item"
        )
        return "\n".join(lines)

    def _list_refs(self, store, grep: str = "") -> str:
        """List flashcard refs (overrides RefHandler to query by corpus)."""
        refs = self._query_corpus_refs(store)
        if not refs:
            return (
                "\U0001f9e0 No flashcard items yet.\n\n"
                "Create one:\n"
                "  put(id='fc:', text='Paris is the capital of France', mode='append')"
            )

        if grep:
            from precis.grep import parse_grep
            pattern = parse_grep(grep)

            def _matches(r: dict) -> bool:
                blob = " ".join([
                    r.get("slug", ""),
                    r.get("title", ""),
                    str(r.get("tags", "")),
                ])
                return pattern.matches(blob)

            refs = [r for r in refs if _matches(r)]
            if not refs:
                return f"No items matching '{grep}'."

        lines = [self._list_header(len(refs), grep), ""]
        for r in refs[: self._max_list]:
            lines.append(self._list_entry(r))

        if len(refs) > self._max_list:
            lines.append(f"\n  ... and {len(refs) - self._max_list} more")
        lines.append("")
        lines.append("Next: get(id='<slug>') for details, get(id='fc:/due') for review")
        return "\n".join(lines)

    # ── /due view ────────────────────────────────────────────────────

    def _read_due(self, store) -> str:
        refs = self._query_corpus_refs(store)
        now = _now()

        due = [r for r in refs if self._is_due(r, now)]
        due.sort(key=lambda r: self._overdue_days(r, now), reverse=True)

        if not due:
            lines = ["\U0001f9e0 No items due for review!", ""]
            total = len(refs)
            if total:
                lines.append(f"  {total} items total, all on schedule.")
            else:
                lines.append("  No items yet. Create one:")
                lines.append(
                    "  put(id='fc:', text='knowledge statement', mode='append')"
                )
            return "\n".join(lines)

        overdue_count = sum(1 for r in due if self._overdue_days(r, now) > 0)
        lines = [
            f"\U0001f9e0 {len(due)} items due for review"
            + (f" ({overdue_count} overdue)" if overdue_count else ""),
            "",
        ]

        for r in due[:MAX_DUE]:
            slug = r.get("slug", "???")
            title = _truncate(r.get("title", ""), 70)
            meta = _parse_meta(r)
            ease = meta.get("easiness", DEFAULT_EASINESS)
            reps = meta.get("reps", 0)
            due_str = _relative_due(meta)
            note = _last_review_note(meta)

            lines.append(f"  {slug}  {due_str}  ease={ease:.1f}  reps={reps}")
            lines.append(f"    {title}")
            if note:
                lines.append(f"    \u26a1 \"{note}\"")

        if len(due) > MAX_DUE:
            lines.append(f"\n  ... +{len(due) - MAX_DUE} more")

        # Nearby & almost due
        not_due = [r for r in refs if not self._is_due(r, now)]
        almost = [
            r for r in not_due
            if 0 < self._days_until_due(r, now) <= NEARBY_DUE_DAYS
        ]
        if almost:
            almost.sort(key=lambda r: self._days_until_due(r, now))
            lines.append("")
            lines.append("\u2500\u2500 Nearby & almost due \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500")
            for r in almost[:MAX_NEARBY]:
                slug = r.get("slug", "???")
                title = _truncate(r.get("title", ""), 50)
                due_str = _relative_due(_parse_meta(r))
                lines.append(f"  {slug}  {due_str}  {title}")

        # Review tips — embedded in the output, not the system prompt
        lines.append("")
        lines.append("Review tips:")
        lines.append("  \u2022 Quiz the user \u2014 don't read cards. Vary: cloze, reverse, \"explain why\", compare.")
        lines.append("  \u2022 \u26a1 lines show past mistakes. Target those confusions.")
        lines.append("  \u2022 Combine nearby items into compound questions.")
        lines.append("  \u2022 After each answer, judge 0-5 and note what happened:")
        lines.append("")
        if due:
            first = due[0].get("slug", "fc:...")
            lines.append(
                f"  put(id='{first}', text='4', mode='review',\n"
                f"      note='what the user got right/wrong')"
            )

        return "\n".join(lines)

    # ── /stats view ──────────────────────────────────────────────────

    def _read_stats(self, store) -> str:
        refs = self._query_corpus_refs(store)
        now = _now()

        total = len(refs)
        if not total:
            return (
                "\U0001f9e0 No flashcard items yet.\n\n"
                "Create one:\n"
                "  put(id='fc:', text='knowledge statement', mode='append')"
            )

        due = [r for r in refs if self._is_due(r, now)]
        overdue = [r for r in due if self._overdue_days(r, now) > 0]
        hard = []
        easiness_vals = []
        mature = young = new = 0

        for r in refs:
            meta = _parse_meta(r)
            e = meta.get("easiness", DEFAULT_EASINESS)
            easiness_vals.append(e)
            interval = meta.get("interval", 0)
            reps = meta.get("reps", 0)

            if reps == 0:
                new += 1
            elif interval >= 21:
                mature += 1
            else:
                young += 1

            if e < HARD_THRESHOLD:
                note = _last_review_note(meta)
                hard.append((r.get("slug", "???"), e, note))

        avg_e = sum(easiness_vals) / len(easiness_vals) if easiness_vals else 0
        min_e = min(easiness_vals) if easiness_vals else 0
        max_e = max(easiness_vals) if easiness_vals else 0

        lines = ["\U0001f9e0 Flashcard Stats", ""]
        lines.append(f"  Total items:    {total:>4}")
        lines.append(
            f"  Due now:        {len(due):>4}"
            + (f" ({len(overdue)} overdue)" if overdue else "")
        )
        lines.append(f"  Avg easiness:  {avg_e:>5.1f}  (range {min_e:.1f} \u2013 {max_e:.1f})")
        lines.append(
            f"  Mature (\u226521d): {mature:>4}  ({mature * 100 // total}%)" if total else ""
        )
        lines.append(f"  Young (<21d):  {young:>4}  ({young * 100 // total}%)" if total else "")
        lines.append(f"  New (0 reps):  {new:>4}  ({new * 100 // total}%)" if total else "")

        if hard:
            hard.sort(key=lambda x: x[1])
            lines.append("")
            lines.append(
                f"\u2500\u2500 Struggle spots (easiness < {HARD_THRESHOLD}) "
                "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500"
            )
            for slug, e, note in hard[:10]:
                line = f"  {slug}  ease={e:.1f}"
                if note:
                    line += f"  \"{_truncate(note, 60)}\""
                lines.append(line)

        lines.append("")
        lines.append("Next:")
        lines.append("  get(id='fc:/due') \u2014 start reviewing")
        lines.append("  search(query='...', scope='fc:') \u2014 find items by topic")
        return "\n".join(lines)

    # ── Write operations ─────────────────────────────────────────────

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
            # Allow path in id (e.g. fc:kanji) — use text for content,
            # fall back to path as text if text is empty
            effective_text = text or path or ""
            return self._create_item(store, effective_text, **kwargs)

        if mode == "review":
            if not path:
                raise PrecisError("item slug required for review")
            return self._record_review(store, path, text, **kwargs)

        if mode == "replace":
            if not path:
                raise PrecisError("item slug required for replace")
            return self._update_body(store, path, text)

        if mode == "after":
            if not path:
                raise PrecisError("item slug required for adding context")
            return self._add_context_block(store, path, text)

        if mode == "note":
            return self._write_note(path, selector, text, **kwargs)

        if mode == "delete":
            if not path:
                raise PrecisError("item slug required for delete")
            return self._delete_item(store, path)

        raise PrecisError(
            f"Unsupported mode '{mode}' for flashcard.\n"
            "Use: mode='append' or 'add' (create), mode='review' (record recall),\n"
            "     mode='replace' (edit), mode='after' (add context),\n"
            "     mode='note' (annotate), mode='delete' (remove)."
        )

    def _create_item(self, store, text: str, **kwargs) -> str:
        """Create a new knowledge item."""
        if not text:
            raise PrecisError(
                "text required \u2014 provide the knowledge statement.\n"
                "Example: put(id='fc:', text='Paris is the capital of France', mode='append')"
            )

        title = text.split("\n")[0][:120]
        tags = kwargs.get("tags", [])

        slug = _slugify(title)
        if not slug:
            raise PrecisError("Cannot generate slug from text")

        now = _now()
        meta = {
            "easiness": DEFAULT_EASINESS,
            "interval": 0,
            "reps": 0,
            "next_review": _iso(now),
            "last_reviewed": None,
            "review_log": [],
        }

        # Disambiguate slug on collision
        base_slug = slug
        suffix = 0
        while True:
            try:
                store.create_ref(
                    slug=slug,
                    corpus_id="flashcards",
                    title=title,
                    metadata=meta,
                    tags=tags if tags else None,
                    blocks=[{"text": text, "block_type": "text"}],
                )
                break
            except ValueError as e:
                if "already exists" in str(e) and suffix < 26:
                    suffix += 1
                    slug = f"{base_slug}-{chr(96 + suffix)}"
                else:
                    raise

        return (
            f"\U0001f9e0 Created: {slug}\n"
            f"  {_truncate(title, 100)}\n"
            f"  ease: {DEFAULT_EASINESS}  next review: tomorrow\n"
            f"\nNext:\n"
            f"  put(id='{slug}', link='source_slug:references') \u2014 link to source\n"
            f"  put(id='{slug}', text='Extra context...', mode='after') \u2014 add context\n"
            f"  get(id='{slug}') \u2014 view item"
        )

    def _record_review(self, store, path: str, text: str, **kwargs) -> str:
        """Record a review: update SM-2 schedule and append to review log."""
        try:
            quality = int(text.strip())
        except (ValueError, TypeError):
            raise PrecisError(
                f"Review quality must be 0-5, got: {text!r}\n"
                "  5=perfect  4=correct,hesitant  3=correct,hard  "
                "2=wrong,close  1=wrong,recognised  0=blank"
            )
        if not 0 <= quality <= 5:
            raise PrecisError("Review quality must be 0-5")

        ref = self._resolve_ref(store, path)
        slug = ref.get("slug", "???")
        meta = _parse_meta(ref)

        # Current SM-2 state
        easiness = meta.get("easiness", DEFAULT_EASINESS)
        interval = meta.get("interval", 0)
        reps = meta.get("reps", 0)

        # Run SM-2
        now = _now()
        result = sm2_update(easiness, interval, reps, quality, now=now)

        # Build review log entry
        note = kwargs.get("note", "")
        log_entry = {"date": _iso(now), "quality": quality}
        if note:
            log_entry["note"] = note

        review_log = meta.get("review_log", [])
        review_log.append(log_entry)

        # Keep last 50 reviews
        if len(review_log) > 50:
            review_log = review_log[-50:]

        # Update metadata
        new_meta = {
            "easiness": result.easiness,
            "interval": result.interval,
            "reps": result.reps,
            "next_review": _iso(result.next_review),
            "last_reviewed": _iso(now),
            "review_log": review_log,
        }
        store.update_ref_metadata(slug, new_meta)

        # Format response
        old_e = easiness
        new_e = result.easiness
        lines = [f"\u2713 {slug} reviewed (quality {quality})"]
        lines.append(
            f"  ease: {old_e:.1f} \u2192 {new_e:.1f}  "
            f"interval: {interval:.0f}d \u2192 {result.interval:.0f}d  "
            f"next: {result.next_review.strftime('%Y-%m-%d')}"
        )

        # Streak from recent log
        recent = review_log[-5:]
        streak = "".join("\u2713" if e.get("quality", 0) >= 3 else "\u2717" for e in recent)
        correct = sum(1 for e in recent if e.get("quality", 0) >= 3)
        lines.append(f"  Streak: {streak} ({correct} of {len(recent)} correct)")

        lines.append("")
        lines.append("Next:")
        lines.append("  get(id='fc:/due') \u2014 refresh due list")
        return "\n".join(lines)

    def _update_body(self, store, path: str, text: str) -> str:
        """Replace the body text of an item."""
        if not text:
            raise PrecisError("text required for replace")
        ref = self._resolve_ref(store, path)
        slug = ref.get("slug", "???")

        blocks = store.get_blocks(slug, block_type="text")
        if blocks:
            node_id = blocks[0].get("node_id")
            if node_id:
                store.update_block_text(slug, node_id, text)
                return f"\u2713 Updated {slug}\n  {_truncate(text, 200)}"

        raise PrecisError(f"No text block found in {slug} to update")

    def _add_context_block(self, store, path: str, text: str) -> str:
        """Add a context block to an item (mode='after')."""
        if not text:
            raise PrecisError("text required for context block")
        ref = self._resolve_ref(store, path)
        slug = ref.get("slug", "???")

        # Count existing blocks to get next index
        blocks = store.get_blocks(slug)
        next_idx = len(blocks)
        node_id = f"{slug}-b{next_idx:04d}"

        # Insert block via store session
        try:
            from acatome_store.models import Block
            session_factory = store._Session
            with session_factory() as session:
                paper = store.get(slug)
                if not paper:
                    raise PrecisError(f"Item not found: {slug}")
                ref_id = paper["ref_id"]
                block = Block(
                    node_id=node_id,
                    profile="default",
                    ref_id=ref_id,
                    page=0,
                    block_index=next_idx,
                    block_type="text",
                    text=text,
                    section_path="[]",
                )
                session.add(block)
                session.commit()
        except ImportError:
            raise PrecisError("acatome-store required for flashcard context blocks")

        return (
            f"\u2713 Added context to {slug}\n"
            f"  {_truncate(text, 200)}\n\n"
            f"Next: get(id='{slug}') \u2014 view item"
        )

    def _delete_item(self, store, path: str) -> str:
        """Soft-delete a flashcard item by marking it in metadata."""
        ref = self._resolve_ref(store, path)
        slug = ref.get("slug", "???")
        store.update_ref_metadata(slug, {"deleted": True})
        return f"\u2713 Deleted {slug}"

    # ── Corpus query helpers ─────────────────────────────────────────

    def _query_corpus_refs(self, store) -> list[dict]:
        """Query all flashcard refs from the store."""
        try:
            from acatome_store.models import Ref
            from sqlalchemy import select
            session_factory = store._Session
            with session_factory() as session:
                stmt = (
                    select(Ref)
                    .where(Ref.corpus_id == "flashcards")
                    .order_by(Ref.first_seen_at.desc())
                )
                rows = session.execute(stmt).scalars().all()
                results = []
                for r in rows:
                    d = r.to_dict()
                    meta = _parse_meta(d)
                    if meta.get("deleted"):
                        continue
                    results.append(d)
                return results
        except ImportError:
            raise PrecisError(
                "acatome-store required for flashcards.\n"
                "Install with: pip install precis-mcp[flashcards]"
            )

    @staticmethod
    def _is_due(ref: dict, now: datetime) -> bool:
        meta = _parse_meta(ref)
        raw = meta.get("next_review")
        if not raw:
            return True  # new item, never reviewed
        try:
            due = datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
        except (ValueError, TypeError):
            return True
        return due <= now

    @staticmethod
    def _overdue_days(ref: dict, now: datetime) -> float:
        meta = _parse_meta(ref)
        raw = meta.get("next_review")
        if not raw:
            return 0
        try:
            due = datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
        except (ValueError, TypeError):
            return 0
        delta = (now - due).total_seconds() / 86400
        return max(0, delta)

    @staticmethod
    def _days_until_due(ref: dict, now: datetime) -> float:
        meta = _parse_meta(ref)
        raw = meta.get("next_review")
        if not raw:
            return 0
        try:
            due = datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
        except (ValueError, TypeError):
            return 0
        return (due - now).total_seconds() / 86400
