"""Todo handler — create and manage task items with state transitions.

Extends RefHandler with todo-specific views and state machine:
  pending → in_progress → done
  pending → blocked → pending
  any → cancelled

Todos are stored as refs in the ``todos`` corpus with state tracked
in ref metadata.  The ``todo:`` URI scheme is used for addressing.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime

from precis.handlers._ref_base import RefHandler, _get_store, _truncate
from precis.protocol import PrecisError

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
    views = {"meta", "summary", "toc", "chunk", "links", "state"}
    extensions: set[str] = set()

    _ref_noun = "todo"
    _ref_emoji = "☐"

    # ── Subclass hooks ───────────────────────────────────────────────

    def _dispatch_view(
        self,
        store,
        ref: dict,
        view: str | None,
        subview: str | None,
        selector: str | None,
    ) -> str | None:
        if view == "state":
            return self._read_state(ref)
        return None

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
        emoji = STATE_EMOJI.get(state, "?")

        lines = [f"{emoji} {slug}"]
        lines.append(f"  {title}")
        lines.append(f"  state: {state}  priority: {priority}")
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
                blob = " ".join([
                    r.get("slug", ""),
                    r.get("title", ""),
                ])
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
        emoji = STATE_EMOJI.get(state, "?")
        pri_tag = f" [{priority}]" if priority != "medium" else ""
        return f"  {emoji} {slug}  {state}{pri_tag}  {title}"

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
            # State transition
            if not path:
                raise PrecisError("todo slug required for state transition")
            return self._transition_state(store, path, text)

        if mode == "replace":
            # Update todo body text
            if not path:
                raise PrecisError("todo slug required for replace")
            return self._update_body(store, path, text)

        if mode == "note":
            return self._write_note(path, selector, text, **kwargs)

        raise PrecisError(
            f"Unsupported mode '{mode}' for todo.\n"
            "Use: mode='append' (create), mode='state' (transition), "
            "mode='replace' (update text), mode='note' (annotate)."
        )

    def _create_todo(self, store, text: str, **kwargs) -> str:
        """Create a new todo item."""
        if not text:
            raise PrecisError("text required — provide the todo title/description")

        title = kwargs.get("title", "") or text.split("\n")[0][:120]
        priority = kwargs.get("priority", DEFAULT_PRIORITY)
        if priority not in PRIORITIES:
            raise PrecisError(
                f"Invalid priority: {priority}. Use: {', '.join(sorted(PRIORITIES))}"
            )

        slug = _slugify(title)
        if not slug:
            raise PrecisError("Cannot generate slug from title")

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
        return (
            f"{emoji} Created todo: {slug}\n"
            f"  {title}\n"
            f"  state: pending  priority: {priority}\n"
            f"\nNext:\n"
            f"  put(id='{slug}', text='in_progress', mode='state')  — start working\n"
            f"  get(id='{slug}')  — view details"
        )

    def _transition_state(self, store, path: str, new_state: str) -> str:
        """Transition a todo to a new state."""
        new_state = new_state.strip().lower()
        if new_state not in STATES:
            raise PrecisError(
                f"Unknown state: {new_state}\nValid states: {', '.join(sorted(STATES))}"
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
                f"Cannot transition {slug} from '{current}' to '{new_state}'.\n"
                f"Valid transitions from '{current}': {', '.join(sorted(allowed))}"
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

    def _update_body(self, store, path: str, text: str) -> str:
        """Replace the body text of a todo."""
        if not text:
            raise PrecisError("text required for replace")
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
        raise PrecisError(f"No text block found in {slug} to update")
