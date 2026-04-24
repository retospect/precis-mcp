"""Todo handler — create and manage task items with state transitions.

Extends RefHandler with todo-specific views and state machine:
  pending → in_progress → done
  pending → blocked → pending
  any → cancelled

Todos are stored as refs in the ``todos`` corpus with state tracked
in ref metadata.  The ``todo:`` URI scheme is used for addressing.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

from precis.handlers._ref_base import (
    RefHandler,
    _get_store,
    _parse_tags,
    _truncate,
)
from precis.protocol import ErrorCode, PrecisError, extract_kwargs


def _parse_meta(ref: dict[str, Any]) -> dict[str, Any]:
    """Return the parsed metadata dict for a ref, defensively.

    The store exposes ``meta`` either as a plain dict (acatome-store v2+)
    or as a JSON string (older shape).  This helper normalises both and
    returns an empty dict on any parse error so callers can ``.get()``
    without guarding.
    """
    raw = ref.get("meta") or ref.get("metadata") or {}
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _coerce_tag_list(raw: Any) -> list[str]:
    """Coerce a ``tags=`` kwarg or free-text blob to a clean list.

    Accepts either:
      - a Python list/tuple of strings
      - a comma- or whitespace-separated string (e.g. ``"urgent, work"``)

    Returns a de-duplicated, order-preserving list of non-empty tags.
    """
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        items = [str(t).strip() for t in raw]
    else:
        items = [t.strip() for t in re.split(r"[,\s]+", str(raw))]
    out: list[str] = []
    seen: set[str] = set()
    for t in items:
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


log = logging.getLogger(__name__)


# ── State machine ────────────────────────────────────────────────────

STATES = {"pending", "in_progress", "done", "blocked", "cancelled"}

# Valid transitions: from_state → {allowed next states}
TRANSITIONS: dict[str, set[str]] = {
    "pending": {"in_progress", "blocked", "cancelled"},
    "in_progress": {"done", "blocked", "pending", "cancelled"},
    "blocked": {"pending", "in_progress", "cancelled"},
    "done": {"pending"},  # reopen
    "cancelled": {"pending"},  # reopen
}

STATE_EMOJI = {
    "pending": "⬚",
    "in_progress": "▶",
    "done": "✓",
    "blocked": "⊘",
    "cancelled": "✗",
}

PRIORITIES = {"low", "medium", "high"}

DEFAULT_STATE = "pending"
DEFAULT_PRIORITY = "medium"

#: States that count as "still open" in the ``/open`` view.  Mirrors
#: the todo-triage skill's mental model: anything not finished yet.
OPEN_STATES: frozenset[str] = frozenset({"pending", "in_progress", "blocked"})

#: States that count as "closed" in the ``/done`` view.  Cancelled
#: belongs here too — the user explicitly stopped caring, which is a
#: terminal state for triage purposes.
DONE_STATES: frozenset[str] = frozenset({"done", "cancelled"})


def _slugify(title: str) -> str:
    """Turn a title into a todo slug: todo:fix-the-bug.

    The selector separator character is reserved as the URI selector separator and
    is stripped along with other non-alphanumeric characters.
    """
    slug = title.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")[:60]
    return f"todo:{slug}" if slug else ""


# ─────────────────────────────────────────────────────────────────────


class TodoHandler(RefHandler):
    """Handler for todo: scheme — writable task items with state.

    Extends RefHandler with:
      /state view, state transitions via put(mode='state'),
      creation via put(mode='append'), priority management.
    """

    scheme = "todo"
    writable = True
    corpus_id = "todos"
    onboarding_skill = "todo-triage"
    views = {
        **RefHandler.views,
        "state": "_read_state_view",
    }
    #: Collection-level views — dispatch with no ref slug.  ``/recent``
    #: mirrors the other ref kinds' recency list; ``/today`` is the
    #: today-centric digest agents want first thing in the morning; each
    #: state name appears as its own view so ``todo:/open`` and
    #: ``todo:/done`` work without passing a state selector.
    collection_views: dict[str, str] = {
        "recent": "_read_recent_view",
        "today": "_read_today_view",
        "open": "_read_open_view",
        "done": "_read_done_view",
        "pending": "_read_pending_view",
        "in_progress": "_read_in_progress_view",
        "blocked": "_read_blocked_view",
        "cancelled": "_read_cancelled_view",
        "tags": "_read_tags_view",
    }
    allowed_modes = {"append", "state", "replace", "note", "tag", "untag"}
    extensions: set[str] = set()

    _ref_noun = "todo"
    _ref_emoji = "☐"
    _slug_prefix = "todo"

    # ── View dispatchers ─────────────────────────────────────────────

    def _read_state_view(self, store, ref, selector, subview, **kwargs) -> str:
        extract_kwargs(kwargs, (), context="todo/state")
        return self._read_state(ref)

    # ── Collection-level views ──────────────────────────────────────
    # All of these share shape: filter :meth:`_query_corpus_refs` by
    # ref.metadata['state'] (or a date predicate for ``/today``) and
    # delegate to :meth:`_render_state_filtered` for a single rendering.

    def _read_recent_view(self, store, subview, **kwargs) -> str:
        extract_kwargs(kwargs, ("top_k",), context="todo/recent")
        refs = self._query_corpus_refs(store)
        return self._render_state_filtered(
            refs, header_noun="recent todos", limit=kwargs.get("top_k") or 20
        )

    def _read_today_view(self, store, subview, **kwargs) -> str:
        extract_kwargs(kwargs, (), context="todo/today")
        today = datetime.now(UTC).date().isoformat()
        refs = self._query_corpus_refs(store)

        def _is_today(r: dict) -> bool:
            meta = _parse_meta(r)
            for key in ("due", "created"):
                raw = str(meta.get(key) or "")
                if raw.startswith(today):
                    return True
            return False

        today_refs = [r for r in refs if _is_today(r)]
        return self._render_state_filtered(
            today_refs, header_noun=f"todos due/created today ({today})"
        )

    def _read_open_view(self, store, subview, **kwargs) -> str:
        return self._view_by_states(store, OPEN_STATES, "open todos", **kwargs)

    def _read_done_view(self, store, subview, **kwargs) -> str:
        return self._view_by_states(store, DONE_STATES, "closed todos", **kwargs)

    def _read_pending_view(self, store, subview, **kwargs) -> str:
        return self._view_by_states(
            store, frozenset({"pending"}), "pending todos", **kwargs
        )

    def _read_in_progress_view(self, store, subview, **kwargs) -> str:
        return self._view_by_states(
            store,
            frozenset({"in_progress"}),
            "in-progress todos",
            **kwargs,
        )

    def _read_blocked_view(self, store, subview, **kwargs) -> str:
        return self._view_by_states(
            store, frozenset({"blocked"}), "blocked todos", **kwargs
        )

    def _read_cancelled_view(self, store, subview, **kwargs) -> str:
        return self._view_by_states(
            store, frozenset({"cancelled"}), "cancelled todos", **kwargs
        )

    def _read_tags_view(self, store, subview, **kwargs) -> str:
        extract_kwargs(kwargs, (), context="todo/tags")
        return self._read_tags(store)

    def _read_tags(self, store) -> str:
        """Histogram of tags across all todos.

        Scans the todos corpus only — ``store.list_tags()`` would span
        every ref kind, which is not what agents want here.
        """
        refs = self._query_corpus_refs(store)
        counts: dict[str, int] = {}
        for r in refs:
            for t in _parse_tags(r):
                counts[t] = counts.get(t, 0) + 1
        if not counts:
            return (
                "☐ No tagged todos yet.\n\n"
                "Tag a todo:\n"
                "  put(id='todo:<slug>', text='urgent,work', mode='tag')\n"
                "  put(type='todo', text='...', tags=['urgent'], mode='append')"
            )
        lines = [f"☐ todo tags ({len(counts)} distinct)", ""]
        for tag, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
            lines.append(f"  {n:>3}  {tag}")
        lines.append("")
        lines.append(
            "Next:\n"
            "  get(id='todo:', grep='tag:<name>')           — filter by tag\n"
            "  put(id='todo:<slug>', text='<name>', mode='untag')  — remove tag only"
        )
        return "\n".join(lines)

    def _view_by_states(
        self,
        store,
        wanted: frozenset[str],
        header_noun: str,
        **kwargs,
    ) -> str:
        extract_kwargs(
            kwargs, ("top_k",), context=f"todo/{'|'.join(sorted(wanted))}"
        )
        refs = self._query_corpus_refs(store)
        refs = [r for r in refs if _parse_meta(r).get("state") in wanted]
        return self._render_state_filtered(
            refs,
            header_noun=header_noun,
            limit=kwargs.get("top_k") or self._max_list,
        )

    def _render_state_filtered(
        self,
        refs: list[dict],
        *,
        header_noun: str,
        limit: int | None = None,
    ) -> str:
        """Shared rendering for the state-filtered collection views.

        Reuses :meth:`_list_entry` so the formatting matches the bare
        ``todo:`` list; the header names the filter so agents can tell
        at a glance which slice they're looking at.
        """
        if not refs:
            return (
                f"☐ No {header_noun}.\n\n"
                "Next:\n"
                "  get(id='todo:')          — full list\n"
                "  get(id='todo:/recent')   — recent activity"
            )
        cap = limit or self._max_list
        lines = [f"☐ {len(refs)} {header_noun}", ""]
        for r in refs[:cap]:
            lines.append(self._list_entry(r))
        if len(refs) > cap:
            lines.append(f"\n  ... and {len(refs) - cap} more")
        lines.append("")
        lines.append(
            "Next:\n"
            "  get(id='<slug>')                               — details\n"
            "  put(id='<slug>', text='done', mode='state')    — complete\n"
            "  put(id='<slug>', text='cancelled', mode='state') — cancel"
        )
        return "\n".join(lines)

    def _read_overview(self, store, ref: dict) -> str:
        slug = ref.get("slug", "???")
        title = ref.get("title", "")
        meta = ref.get("meta") or ref.get("metadata") or {}
        if isinstance(meta, str):
            import json

            try:
                meta = json.loads(meta)
            except (ValueError, TypeError):
                meta = {}

        state = meta.get("state", DEFAULT_STATE)
        priority = meta.get("priority", DEFAULT_PRIORITY)
        created = meta.get("created", "")
        tags = _parse_tags(ref)
        emoji = STATE_EMOJI.get(state, "?")

        lines = [f"{emoji} {slug}"]
        lines.append(f"  {title}")
        lines.append(f"  state: {state}  priority: {priority}")
        if tags:
            lines.append(f"  tags: {', '.join(tags)}")
        if created:
            lines.append(f"  created: {created}")

        # Show body text
        blocks = store.get_blocks(slug)
        for block in blocks[:3]:
            text = block.get("text", "")
            if text:
                lines.append(f"  {_truncate(text, 200)}")

        # Link count
        try:
            link_counts = store.get_link_count(slug)
            if link_counts:
                total = sum(link_counts.values())
                lines.append(f"  {total} links")
        except Exception:
            pass

        # Valid transitions
        valid = sorted(TRANSITIONS.get(state, set()))
        if valid:
            lines.append("")
            lines.append(f"  Transitions: {', '.join(valid)}")

        lines.append("")
        lines.append("Next:")
        lines.append(f"  put(id='{slug}', text='done', mode='state')  — change state")
        lines.append(f"  put(id='{slug}', text='...', mode='replace') — update text")
        lines.append(f"  get(id='{slug}/links')  — links")
        return "\n".join(lines)

    def _read_meta(self, ref: dict) -> str:
        meta = ref.get("meta") or ref.get("metadata") or {}
        if isinstance(meta, str):
            import json

            try:
                meta = json.loads(meta)
            except (ValueError, TypeError):
                meta = {}

        lines = []
        for key in ("slug", "title"):
            val = ref.get(key, "")
            if val:
                lines.append(f"  {key}: {val}")
        for key in ("state", "priority", "created", "due"):
            val = meta.get(key, "")
            if val:
                lines.append(f"  {key}: {val}")
        ref_id = ref.get("ref_id") or ref.get("id")
        if ref_id:
            lines.append(f"  ref_id: {ref_id}")
        return "\n".join(lines)

    # ── List (override — query refs by corpus, not papers table) ─────

    def _query_corpus_refs(self, store) -> list[dict]:
        """Query all todo refs from the store.

        Uses the public ``Store.list_refs_by_corpus`` API so tests can
        mock cleanly and the handler does not reach into private store
        internals.
        """
        return store.list_refs_by_corpus("todos")

    def _list_refs(self, store, grep: str = "") -> str:
        """List todo refs (overrides RefHandler to query by corpus)."""
        refs = self._query_corpus_refs(store)
        if not refs:
            return (
                "☐ No todos yet.\n\n"
                "Create one:\n"
                "  put(id='todo:', text='Buy milk', mode='append')"
            )

        if grep:
            from precis.grep import parse_grep

            pattern = parse_grep(grep)

            def _matches(r: dict) -> bool:
                tags = _parse_tags(r)
                # Include tags twice: as bare names (so grep='urgent' hits)
                # and with the ``tag:`` prefix (so grep='tag:urgent' hits
                # only tags, never titles that happen to contain the word).
                blob = " ".join(
                    [
                        r.get("slug", ""),
                        r.get("title", ""),
                        " ".join(tags),
                        " ".join(f"tag:{t}" for t in tags),
                    ]
                )
                return pattern.matches(blob)

            refs = [r for r in refs if _matches(r)]
            if not refs:
                return f"No todos matching '{grep}'."

        lines = [self._list_header(len(refs), grep), ""]
        for r in refs[: self._max_list]:
            lines.append(self._list_entry(r))

        if len(refs) > self._max_list:
            lines.append(f"\n  ... and {len(refs) - self._max_list} more")
        lines.append("")
        lines.append(
            "Next: get(id='<slug>') for details, "
            "put(id='<slug>', text='done', mode='state') to complete"
        )
        return "\n".join(lines)

    def _list_header(self, count: int, grep: str = "") -> str:
        if grep:
            return f"☐ {count} todos matching '{grep}'"
        return f"☐ {count} todos"

    def _list_entry(self, ref: dict) -> str:
        slug = ref.get("slug", "???")
        title = _truncate(ref.get("title", ""), 60)
        meta = ref.get("meta") or ref.get("metadata") or {}
        if isinstance(meta, str):
            import json

            try:
                meta = json.loads(meta)
            except (ValueError, TypeError):
                meta = {}
        state = meta.get("state", DEFAULT_STATE)
        priority = meta.get("priority", DEFAULT_PRIORITY)
        tags = _parse_tags(ref)
        emoji = STATE_EMOJI.get(state, "?")
        pri_tag = f" [{priority}]" if priority != "medium" else ""
        tag_str = f"  #{' #'.join(tags)}" if tags else ""
        return f"  {emoji} {slug}  {state}{pri_tag}  {title}{tag_str}"

    def _overview_hints(self, slug: str, ref: dict) -> list[str]:
        return [
            f"put(id='{slug}', text='done', mode='state')  — change state",
        ]

    # ── State view ───────────────────────────────────────────────────

    def _read_state(self, ref: dict) -> str:
        slug = ref.get("slug", "???")
        meta = ref.get("meta") or ref.get("metadata") or {}
        if isinstance(meta, str):
            import json

            try:
                meta = json.loads(meta)
            except (ValueError, TypeError):
                meta = {}

        state = meta.get("state", DEFAULT_STATE)
        emoji = STATE_EMOJI.get(state, "?")
        valid = sorted(TRANSITIONS.get(state, set()))

        lines = [f"{emoji} {slug}  state={state}"]
        if valid:
            lines.append(f"  Valid transitions: {', '.join(valid)}")
            lines.append("")
            for t in valid:
                lines.append(f"  put(id='{slug}', text='{t}', mode='state')")
        else:
            lines.append("  No transitions available.")
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

        if mode == "append" and not path:
            # Create new todo
            return self._create_todo(store, text, **kwargs)

        if mode == "state":
            if not path:
                raise PrecisError(
                    ErrorCode.PARAM_INVALID,
                    cause="id= required for mode='state' (which todo to transition)",
                    next="get(type='todo', id='/today') to find the slug",
                )
            return self._transition_state(store, path, text)

        if mode == "replace":
            if not path:
                raise PrecisError(
                    ErrorCode.PARAM_INVALID,
                    cause="id= required for mode='replace'",
                )
            return self._update_body(store, path, text)

        if mode == "note":
            return self._write_note(path, selector, text, **kwargs)

        if mode in ("tag", "untag"):
            if not path:
                raise PrecisError(
                    ErrorCode.PARAM_INVALID,
                    cause=f"id= required for mode={mode!r} (which todo to tag)",
                    next="get(id='todo:/open') to find the slug",
                )
            return self._mutate_tags(store, path, text, mode=mode, **kwargs)

        raise PrecisError(
            ErrorCode.MODE_UNSUPPORTED,
            cause=f"mode {mode!r} not supported on todo",
        )

    def _create_todo(self, store, text: str, **kwargs) -> str:
        """Create a new todo item."""
        if not text:
            raise PrecisError(
                ErrorCode.PARAM_INVALID,
                cause="text= required \u2014 provide the todo title/description",
                next="put(type='todo', text='your task', mode='append')",
            )

        title = kwargs.get("title", "") or text.split("\n")[0][:120]
        priority = kwargs.get("priority", DEFAULT_PRIORITY)
        if priority not in PRIORITIES:
            raise PrecisError(
                ErrorCode.PARAM_INVALID,
                cause=f"priority must be one of the standard values; got {priority!r}",
                options=sorted(PRIORITIES),
            )

        tags = _coerce_tag_list(kwargs.get("tags"))

        slug = _slugify(title)
        if not slug:
            raise PrecisError(
                ErrorCode.PARAM_INVALID,
                cause="cannot generate slug from title (title has no alphanumerics)",
            )

        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        meta = {
            "state": DEFAULT_STATE,
            "priority": priority,
            "created": now,
        }

        # Try to create; disambiguate slug if collision
        base_slug = slug
        suffix = 0
        while True:
            try:
                ref_id = store.create_ref(
                    slug=slug,
                    corpus_id="todos",
                    title=title,
                    metadata=meta,
                    tags=tags if tags else None,
                    blocks=[{"text": text, "block_type": "text"}],
                )
                break
            except ValueError as e:
                if "already exists" in str(e) and suffix < 26:
                    suffix += 1
                    slug = f"{base_slug}-{chr(96 + suffix)}"  # a, b, c...
                else:
                    raise

        emoji = STATE_EMOJI[DEFAULT_STATE]
        tag_line = f"  tags: {', '.join(tags)}\n" if tags else ""
        return (
            f"{emoji} Created todo: {slug}\n"
            f"  {title}\n"
            f"  state: pending  priority: {priority}\n"
            f"{tag_line}"
            f"\nNext:\n"
            f"  put(id='{slug}', text='in_progress', mode='state')  — start working\n"
            f"  get(id='{slug}')  — view details"
        )

    def _transition_state(self, store, path: str, new_state: str) -> str:
        """Transition a todo to a new state."""
        new_state = new_state.strip().lower()
        if new_state not in STATES:
            raise PrecisError(
                ErrorCode.PARAM_INVALID,
                cause=f"unknown state: {new_state!r}",
                options=sorted(STATES),
            )

        ref = self._resolve_ref(store, path)
        slug = ref.get("slug", "???")
        meta = ref.get("meta") or ref.get("metadata") or {}
        if isinstance(meta, str):
            import json

            try:
                meta = json.loads(meta)
            except (ValueError, TypeError):
                meta = {}

        current = meta.get("state", DEFAULT_STATE)
        allowed = TRANSITIONS.get(current, set())
        if new_state not in allowed:
            raise PrecisError(
                ErrorCode.PARAM_INVALID,
                cause=f"cannot transition {slug} from {current!r} to {new_state!r}",
                options=sorted(allowed),
            )

        # Update metadata
        store.update_ref_metadata(slug, {"state": new_state})

        old_emoji = STATE_EMOJI.get(current, "?")
        new_emoji = STATE_EMOJI.get(new_state, "?")
        return (
            f"{old_emoji}→{new_emoji} {slug}: {current} → {new_state}\n"
            f"\nNext:\n"
            f"  get(id='{slug}')  — view todo"
        )

    def _mutate_tags(
        self,
        store,
        path: str,
        text: str,
        *,
        mode: str,
        **kwargs,
    ) -> str:
        """Add or remove tags on a todo without touching the todo itself.

        Accepts tags either as a ``tags=`` kwarg (list or comma-string)
        or as the positional ``text`` (comma/space-separated).  Mode
        ``'tag'`` unions; ``'untag'`` removes.  The underlying ref is
        preserved — this only rewrites the ``refs.tags`` column via
        :meth:`acatome_store.Store.add_tags` / ``remove_tags``.
        """
        ref = self._resolve_ref(store, path)
        slug = ref.get("slug", path)

        # Prefer explicit kwarg, fall back to text payload.
        raw = kwargs.get("tags")
        if raw is None and text:
            raw = text
        wanted = _coerce_tag_list(raw)
        if not wanted:
            raise PrecisError(
                ErrorCode.PARAM_INVALID,
                cause=(
                    f"mode={mode!r} needs at least one tag \u2014 pass via "
                    "text='urgent,work' or tags=['urgent','work']"
                ),
            )

        if mode == "tag":
            ok = store.add_tags(slug, wanted)
            verb = "Tagged"
        else:
            ok = store.remove_tags(slug, wanted)
            verb = "Untagged"

        if not ok:
            raise PrecisError(
                ErrorCode.ID_NOT_FOUND,
                cause=f"could not update tags on {slug} (ref not found)",
            )

        # Re-read to show the resulting tag set.
        refreshed = store.get(slug) or ref
        current = _parse_tags(refreshed)
        current_str = ", ".join(current) if current else "(none)"
        return (
            f"☐ {verb} {slug}\n"
            f"  {mode}: {', '.join(wanted)}\n"
            f"  tags: {current_str}\n"
            f"\nNext:\n"
            f"  get(id='{slug}')                 — view todo\n"
            f"  get(id='todo:/tags')             — tag histogram"
        )

    def _update_body(self, store, path: str, text: str) -> str:
        """Replace the body text of a todo."""
        if not text:
            raise PrecisError(
                ErrorCode.PARAM_INVALID,
                cause="text= required for mode='replace'",
            )
        ref = self._resolve_ref(store, path)
        slug = ref.get("slug", "???")
        ref_id = ref.get("ref_id") or ref.get("id")

        # Get existing blocks and update first text block
        blocks = store.get_blocks(slug, block_type="text")
        if blocks:
            node_id = blocks[0].get("node_id")
            if node_id:
                store.update_block_text(slug, node_id, text)
                return f"✓ Updated {slug}\n{_truncate(text, 200)}"

        # No existing block — shouldn't happen but handle gracefully
        raise PrecisError(
            ErrorCode.UNEXPECTED,
            cause=f"no text block found in {slug} to update",
        )
