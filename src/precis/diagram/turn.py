"""One interactive turn on a diagram — the draw-with-me loop, generic over
:class:`~precis.diagram.lang.DiagramLang`.

Each turn the model sees the *current* state (the source with its named
elements), the mechanical **lints** (compile + geometry + binding drift), the
two model-owned prose docs, the element→chunk prepared context, and the user's
message; it emits a JSON reply carrying a short chat ``reply``, the whole
rewritten source, optionally updated ``vocab`` / ``notes``, and optionally the
full desired ``links`` (element→chunk bindings).

Robustness seams — all language-agnostic: **sanitize** every model source
before storage; **bounded auto-heal** on a compile failure (never overwrite
good source with broken); **conventions are the model's job**, held via the
docs, not a checker. Only the source *language* (compile / sanitize / lint /
elements / bounds / prompt fragments) varies, behind the ``lang`` port.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

from precis.diagram.context import render_diagram_context
from precis.diagram.lang import DiagramLang, LintFinding

log = logging.getLogger(__name__)


class _StoreLike(Protocol):
    def reading_order(self, ref_id: int, *, kind: str = ...) -> list[Any]: ...
    def edit_text(self, handle: str, text: str, *, kind: str = ...) -> Any: ...
    def add_chunks(self, **kw: Any) -> list[Any]: ...
    def stamp_ref_meta(self, ref_id: int, patch: dict[str, Any]) -> Any: ...
    def element_bindings(self, node_chunk_id: int) -> list[dict[str, Any]]: ...
    def set_element_bindings(
        self, *, node_chunk_id: int, desired: list[dict[str, Any]], set_by: str = ...
    ) -> dict[str, int]: ...
    def universal_chunk(self, handle: str) -> dict[str, Any] | None: ...


#: A turn model call: takes the built prompt, returns the parsed JSON dict.
ClaudeFn = Callable[[str], dict[str, Any]]


@lru_cache(maxsize=8)
def pinned_skill(skill_name: str) -> str:
    """The pinned skill body (frontmatter stripped) for ``skill_name``, cached
    so editing the skill edits the guidance. Empty on any failure — a turn
    must never break because a doc file moved."""
    try:
        import precis

        path = (
            Path(precis.__file__).resolve().parent
            / "data"
            / "skills"
            / f"{skill_name}.md"
        )
        raw = path.read_text(encoding="utf-8")
    except Exception:  # pragma: no cover — defensive
        return ""
    if raw.startswith("---"):
        parts = raw.split("---", 2)
        if len(parts) == 3:
            return parts[2].strip()
    return raw.strip()


@dataclass(frozen=True, slots=True)
class TurnResult:
    """Outcome of one :func:`run_turn`."""

    reply: str
    svg: str  # the source after the turn (new if changed, else old)
    findings: list[Any]  # list[LintFinding] on the final source
    changed: bool  # did the source change this turn?
    healed: bool  # did an auto-heal retry run?
    vocab: str = ""  # the shared vocabulary after the turn (for pane reload)
    notes: str = ""  # the implementation notes after the turn (for pane reload)
    bindings: list[dict[str, Any]] = field(
        default_factory=list
    )  # element→chunk bindings after the turn (for the chips)


def _docs(
    lang: DiagramLang, store: _StoreLike, ref_id: int
) -> tuple[Any | None, Any | None, Any | None]:
    """Return ``(source_chunk, vocab_chunk, notes_chunk)`` for a diagram."""
    source = vocab = notes = None
    for c in store.reading_order(ref_id, kind=lang.kind):
        if c.chunk_kind == lang.source_kind and source is None:
            source = c
        elif c.chunk_kind == lang.vocab_kind and vocab is None:
            vocab = c
        elif c.chunk_kind == lang.notes_kind and notes is None:
            notes = c
    return source, vocab, notes


def _bounds(lang: DiagramLang, ref: Any, source: str) -> Any:
    """The active bounds: the source's own, then ref.meta, then the default."""
    from_doc = lang.read_bounds(source)
    if from_doc is not None:
        return from_doc
    raw = (getattr(ref, "meta", None) or {}).get(lang.bounds_meta_key)
    from_meta = lang.bounds_from_meta(raw)
    if from_meta is not None:
        return from_meta
    return lang.default_bounds()


def build_prompt(
    lang: DiagramLang,
    *,
    message: str,
    source: str,
    vocab: str,
    notes: str = "",
    findings: list[Any],
    bounds: Any,
    skills: str = "",
    context: str = "",
) -> str:
    """Assemble the turn prompt. Pure — tested for carrying each part.

    The pinned skill (``skills``) carries the full guidance; ``lang``'s inline
    floor (the admonishment + safety + JSON contract) still steers even if the
    skill fails to load. ``context`` is the element→chunk prepared context
    (ADR 0057), inserted after the source so the model edits with the linked
    chunk bodies in hand; empty when nothing is bound.
    """
    lint_block = (
        "\n".join(f"- [{f.kind}] {f.message}" for f in findings)
        if findings
        else "(none — the current diagram parses and fits the canvas)"
    )
    parts = []
    if skills.strip():
        parts.append(skills.strip())
    parts.append(lang.floor_guidance())
    parts.append(lang.canvas_section(bounds))
    parts.append(
        f"## Shared vocabulary (for the human — high-level)\n"
        f"{vocab or '(empty — describe what the drawing is)'}"
    )
    parts.append(
        f"## Implementation notes (your private design log)\n"
        f"{notes or '(empty — record ids / structure / conventions here)'}"
    )
    parts.append(f"## Current source\n{source}")
    if context.strip():
        parts.append(context.strip())
    parts.append(f"## Lints on the current source\n{lint_block}")
    parts.append(f"## The human says\n{message}")
    parts.append(lang.json_contract())
    return "\n\n".join(parts)


def run_turn(
    lang: DiagramLang,
    store: _StoreLike,
    ref: Any,
    message: str,
    *,
    claude_fn: ClaudeFn,
    skills: str | None = None,
) -> TurnResult:
    """Run one draw-with-me turn and persist the result. See module docstring."""
    skills_text = skills if skills is not None else pinned_skill(lang.skill_name)
    source_chunk, vocab_chunk, notes_chunk = _docs(lang, store, ref.id)
    current = source_chunk.text if source_chunk is not None else ""
    vocab = vocab_chunk.text if vocab_chunk is not None else ""
    notes = notes_chunk.text if notes_chunk is not None else ""
    bounds = _bounds(lang, ref, current)

    # Element→chunk bindings (ADR 0057): the prepared context + the
    # dangling-binding lint both key off the source chunk's id.
    node_chunk_id = source_chunk.chunk_id if source_chunk is not None else None
    context = (
        render_diagram_context(lang, store, node_chunk_id, current)
        if node_chunk_id is not None
        else ""
    )
    findings = _all_findings(lang, store, node_chunk_id, current, bounds)

    prompt = build_prompt(
        lang,
        message=message,
        source=current,
        vocab=vocab,
        notes=notes,
        findings=findings,
        bounds=bounds,
        skills=skills_text,
        context=context,
    )

    reply, new_src, new_vocab, new_notes, new_links, healed = _ask_with_heal(
        lang, claude_fn, prompt
    )

    changed = False
    final_src = current
    if new_src is not None:
        # new_src is already sanitized + compiles (guaranteed by _ask_with_heal).
        if source_chunk is not None:
            store.edit_text(source_chunk.handle, new_src, kind=lang.kind)
        else:
            created = store.add_chunks(
                ref_id=ref.id,
                chunk_kind=lang.source_kind,
                text=new_src,
                meta={"no_index": "true"},
                split=False,
                kind=lang.kind,
            )
            if created:
                node_chunk_id = created[0].chunk_id  # bind against the new source
        final_src = new_src
        changed = True
        vb = lang.read_bounds(new_src)
        if vb is not None:
            store.stamp_ref_meta(
                ref.id, {lang.bounds_meta_key: lang.bounds_to_meta(vb)}
            )

    # Reconcile bindings to the model's declared set (the whole `links` array
    # replaces the current set); omitting `links` leaves them untouched.
    if new_links is not None and node_chunk_id is not None:
        store.set_element_bindings(node_chunk_id=node_chunk_id, desired=new_links)

    final_vocab = _persist_doc(
        lang, store, ref.id, vocab_chunk, lang.vocab_kind, new_vocab, vocab, index=True
    )
    final_notes = _persist_doc(
        lang, store, ref.id, notes_chunk, lang.notes_kind, new_notes, notes, index=False
    )

    _persist_turn(lang, store, ref.id, message, reply)
    final_findings = _all_findings(lang, store, node_chunk_id, final_src, bounds)
    final_bindings = (
        store.element_bindings(node_chunk_id) if node_chunk_id is not None else []
    )
    return TurnResult(
        reply=reply,
        svg=final_src,
        findings=final_findings,
        changed=changed,
        healed=healed,
        vocab=final_vocab,
        notes=final_notes,
        bindings=final_bindings,
    )


def _all_findings(
    lang: DiagramLang,
    store: _StoreLike,
    node_chunk_id: int | None,
    source: str,
    bounds: Any,
) -> list[Any]:
    """The full lint set: compile + geometry (the language) plus the
    dangling-binding check (ADR 0057). Empty on an empty source."""
    if not source:
        return []
    findings: list[LintFinding] = lang.lint(source, bounds)
    if node_chunk_id is not None:
        bound = {b["element"] for b in store.element_bindings(node_chunk_id)}
        findings = findings + lang.lint_bindings(source, bound)
    return findings


def _persist_doc(
    lang: DiagramLang,
    store: _StoreLike,
    ref_id: int,
    chunk: Any | None,
    chunk_kind: str,
    new_text: str | None,
    current: str,
    *,
    index: bool,
) -> str:
    """Persist a changed vocab/notes doc (create-or-replace); return the value
    after the turn. No-op when the model didn't change it. ``index`` False
    mints ``meta.no_index`` (notes are internal, never searched)."""
    if new_text is None or not new_text.strip() or new_text == current:
        return current
    if chunk is not None:
        store.edit_text(chunk.handle, new_text, kind=lang.kind)
    else:
        store.add_chunks(
            ref_id=ref_id,
            chunk_kind=chunk_kind,
            text=new_text,
            meta=None if index else {"no_index": "true"},
            split=False,
            kind=lang.kind,
        )
    return new_text


def _ask_with_heal(
    lang: DiagramLang,
    call: ClaudeFn,
    prompt: str,
) -> tuple[str, str | None, str | None, str | None, list[dict[str, Any]] | None, bool]:
    """Call the model; auto-heal one compile failure. Returns ``(reply,
    sanitized_source_or_None, vocab_or_None, notes_or_None, links_or_None,
    healed)``.

    ``sanitized_source`` is ``None`` when the model changed nothing OR never
    produced compilable source (the caller keeps the old source either way — a
    broken reply must not overwrite good work). ``links`` is ``None`` when the
    key is absent (bindings untouched) or an explicit list (the full desired
    binding set, ADR 0057)."""
    data = _safe_call(call, prompt)
    reply = str(data.get("reply") or "").strip()
    vocab_out = _str_or_none(data.get("vocab"))
    notes_out = _str_or_none(data.get("notes"))
    links_out = _links_or_none(data.get("links"))

    raw = data.get(lang.source_key)
    if not isinstance(raw, str) or not raw.strip():
        return reply, None, vocab_out, notes_out, links_out, False  # chat-only

    ok = _clean_if_valid(lang, raw)
    if ok is not None:
        return reply, ok, vocab_out, notes_out, links_out, False

    # One bounded auto-heal: re-prompt with the parse error.
    err = lang.parse_error(raw) or "the source did not parse"
    heal_prompt = (
        f"{prompt}\n\n## Your previous reply's source was rejected\n{err}\n"
        "Return corrected JSON (same shape). The source must be well-formed."
    )
    data2 = _safe_call(call, heal_prompt)
    reply2 = str(data2.get("reply") or reply).strip()
    vocab_out2 = _str_or_none(data2.get("vocab")) or vocab_out
    notes_out2 = _str_or_none(data2.get("notes")) or notes_out
    links_out2 = _links_or_none(data2.get("links"))
    if links_out2 is None:
        links_out2 = links_out
    raw2 = data2.get(lang.source_key)
    if isinstance(raw2, str) and raw2.strip():
        ok2 = _clean_if_valid(lang, raw2)
        if ok2 is not None:
            return reply2, ok2, vocab_out2, notes_out2, links_out2, True
    # Still broken — keep the old source, surface nothing new to storage.
    log.warning("%s turn: model source failed to compile after auto-heal", lang.kind)
    return reply2, None, vocab_out2, notes_out2, links_out2, True


def _str_or_none(v: Any) -> str | None:
    return v if isinstance(v, str) else None


def _links_or_none(v: Any) -> list[dict[str, Any]] | None:
    """A model ``links`` value → a clean list of binding specs, or ``None``
    (key absent / wrong type ⇒ leave bindings untouched). An explicit empty
    list is preserved (it clears all bindings)."""
    if not isinstance(v, list):
        return None
    return [item for item in v if isinstance(item, dict)]


def _safe_call(call: ClaudeFn, prompt: str) -> dict[str, Any]:
    """Invoke the model; coerce any non-dict / error into an empty dict so a
    turn never crashes the request (the caller degrades to a chat-only turn)."""
    try:
        out = call(prompt)
    except Exception as exc:  # model/parse/subprocess failure
        # Almost always an env issue on the server (an expired `claude`
        # OAuth login, a missing binary, or a quota cap) — not something the
        # user did. Log the detail; give the human an actionable, jargon-free
        # note instead of a raw "claude -p exited 1". Nothing was changed.
        log.warning("diagram turn: model call failed: %s", exc)
        return {
            "reply": (
                "(I couldn't reach the drawing model just now — nothing was "
                "changed. This usually means `claude` needs re-authenticating "
                "on the server (an operator runs `claude /login`). Your "
                "message wasn't lost — try again once it's back.)"
            )
        }
    if isinstance(out, dict):
        return out
    if isinstance(out, str):
        try:
            parsed = json.loads(out)
            if isinstance(parsed, dict):
                return parsed
        except ValueError:
            pass
    return {}


def _clean_if_valid(lang: DiagramLang, raw: str) -> str | None:
    """Sanitize ``raw`` and return it iff it compiles, else ``None``."""
    if lang.parse_error(raw) is not None:
        return None
    try:
        cleaned = lang.sanitize(raw)
    except Exception:
        return None
    return cleaned if lang.parse_error(cleaned) is None else None


def _persist_turn(
    lang: DiagramLang, store: _StoreLike, ref_id: int, message: str, reply: str
) -> None:
    """Append the turn's chat chunk (embedded, resumable)."""
    text = f"user: {message.strip()}\n\nassistant: {reply.strip()}"
    store.add_chunks(
        ref_id=ref_id,
        chunk_kind=lang.turn_kind,
        text=text,
        at={"last": True},
        split=False,
        kind=lang.kind,
    )
