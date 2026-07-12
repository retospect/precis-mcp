"""One interactive turn on a figure — the draw-with-me loop.

Each turn the model sees the *current* state (the SVG source with its named
``id``s), the two mechanical **lints** (compile + out-of-bounds), the two
model-owned prose docs, and the user's message; it emits a JSON reply
carrying a short chat ``reply``, the whole rewritten ``svg`` (cad-style
whole-source rewrite), and optionally updated ``vocab`` / ``notes``.

The two docs are deliberately split (they were one and got overloaded):

- **vocab** — the *human-facing* shared vocabulary: high-level, "what the
  drawing IS". Embedded + searchable. Shown by default.
- **notes** — the model's *private* implementation notes: element ids,
  structure, conventions. Not embedded; shown behind a tab. Both are fed to
  the model every turn (both are its memory), but only the vocab is for the
  human.

Robustness seams: **sanitize** every model SVG before storage/DOM; **bounded
auto-heal** on a compile failure (never overwrite good source with broken);
**conventions are the model's job**, held via the docs, not a checker.

The guidance the model follows (keep the vocab high-level + updated, put
detail in notes, keep the reply short, safety rules) lives in the pinned
``precis-figure-svg`` skill, which is prepended to the prompt — so editing
the skill edits the prompt. The model call is injected (``claude_fn``) so the
whole loop is testable without a real ``claude``.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

from precis.figure.svg import (
    DEFAULT_VIEWBOX,
    SvgError,
    lint_svg,
    parse_error,
    read_viewbox,
    sanitize_svg,
)

log = logging.getLogger(__name__)

#: The model reply is a single JSON object with these keys.
_JSON_CONTRACT = (
    'Reply with ONE JSON object and nothing else: {"reply": "<a SHORT chat '
    'message to the human>", "svg": "<the COMPLETE new <svg>…</svg> source, '
    'or omit/empty to leave the drawing unchanged>", "vocab": "<the updated '
    'shared vocabulary — high-level, for the human — or omit if unchanged>", '
    '"notes": "<the updated implementation notes — your private design log — '
    'or omit if unchanged>"}.'
)

#: Drawing wants a capable model; haiku (call_claude_p's default) is too weak.
_DEFAULT_MODEL = os.environ.get("PRECIS_FIGURE_MODEL", "claude-opus-4-8")


class _StoreLike(Protocol):
    def reading_order(self, ref_id: int, *, kind: str = ...) -> list[Any]: ...
    def edit_text(self, handle: str, text: str, *, kind: str = ...) -> Any: ...
    def add_chunks(self, **kw: Any) -> list[Any]: ...
    def stamp_ref_meta(self, ref_id: int, patch: dict[str, Any]) -> Any: ...


#: A turn model call: takes the built prompt, returns the parsed JSON dict.
ClaudeFn = Callable[[str], dict[str, Any]]


@lru_cache(maxsize=1)
def _pinned_skill() -> str:
    """The ``precis-figure-svg`` skill body (frontmatter stripped), pinned into
    every turn prompt so editing the skill edits the guidance. Empty on any
    failure — a turn must never break because a doc file moved."""
    try:
        import precis

        path = (
            Path(precis.__file__).resolve().parent
            / "data"
            / "skills"
            / "precis-figure-svg.md"
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
    svg: str  # the current source after the turn (new if changed, else old)
    findings: list[Any]  # list[LintFinding] on the final source
    changed: bool  # did the source SVG change this turn?
    healed: bool  # did an auto-heal retry run?
    vocab: str = ""  # the shared vocabulary after the turn (for pane reload)
    notes: str = ""  # the implementation notes after the turn (for pane reload)


def _default_claude(prompt: str) -> dict[str, Any]:
    from precis.utils.claude_p import call_claude_p

    res = call_claude_p(
        prompt,
        model=_DEFAULT_MODEL,
        max_usd=float(os.environ.get("PRECIS_FIGURE_MAX_USD", "1.0")),
        timeout_s=float(os.environ.get("PRECIS_FIGURE_TIMEOUT_S", "300")),
    )
    return res.data


def _docs(store: _StoreLike, ref_id: int) -> tuple[Any | None, Any | None, Any | None]:
    """Return ``(source_chunk, vocab_chunk, notes_chunk)`` for a figure."""
    source = vocab = notes = None
    for c in store.reading_order(ref_id, kind="figure"):
        if c.chunk_kind == "figure_node" and source is None:
            source = c
        elif c.chunk_kind == "figure_vocab" and vocab is None:
            vocab = c
        elif c.chunk_kind == "figure_notes" and notes is None:
            notes = c
    return source, vocab, notes


def _viewbox(ref: Any, svg: str) -> tuple[float, float, float, float]:
    """The active viewBox: the SVG's own, then ref.meta, then the default."""
    from_doc = read_viewbox(svg)
    if from_doc is not None:
        return from_doc
    raw = (getattr(ref, "meta", None) or {}).get("viewbox")
    if isinstance(raw, (list, tuple)) and len(raw) == 4:
        try:
            return (float(raw[0]), float(raw[1]), float(raw[2]), float(raw[3]))
        except (TypeError, ValueError):
            pass
    return DEFAULT_VIEWBOX


def build_prompt(
    *,
    message: str,
    svg: str,
    vocab: str,
    notes: str = "",
    findings: list[Any],
    viewbox: tuple[float, float, float, float],
    skills: str = "",
) -> str:
    """Assemble the turn prompt. Pure — tested for carrying each part.

    The pinned skill (``skills``) carries the full guidance; the inline block
    below is the guaranteed floor (the admonishment + safety + JSON contract)
    so the prompt still steers even if the skill fails to load.
    """
    x, y, w, h = viewbox
    lint_block = (
        "\n".join(f"- [{f.kind}] {f.message}" for f in findings)
        if findings
        else "(none — the current drawing parses and fits the canvas)"
    )
    parts = []
    if skills.strip():
        parts.append(skills.strip())
    parts.append(
        "You are drawing an SVG figure WITH a human. You maintain three "
        "things: the SVG source; the shared VOCABULARY (high-level and "
        "human-facing — what the drawing is); and your private implementation "
        "NOTES (element ids, structure, conventions). Every turn: update the "
        "vocabulary and keep it high-level and concise (move any low-level "
        "detail into notes), keep notes accurate for consistent edits, and "
        "keep your chat reply short — the detail lives in the docs, not the "
        "chat. Edit by rewriting the whole <svg>; name elements with stable "
        "id= and <title>. Safety: no <script>/<foreignObject>/event handlers/"
        "external or data href (all stripped). Keep shapes inside the viewBox."
    )
    parts.append(f"## Canvas\nviewBox = {x} {y} {w} {h}")
    parts.append(
        f"## Shared vocabulary (for the human — high-level)\n"
        f"{vocab or '(empty — describe what the drawing is)'}"
    )
    parts.append(
        f"## Implementation notes (your private design log)\n"
        f"{notes or '(empty — record ids / structure / conventions here)'}"
    )
    parts.append(f"## Current SVG source\n{svg}")
    parts.append(f"## Lints on the current source\n{lint_block}")
    parts.append(f"## The human says\n{message}")
    parts.append(_JSON_CONTRACT)
    return "\n\n".join(parts)


def run_turn(
    store: _StoreLike,
    ref: Any,
    message: str,
    *,
    claude_fn: ClaudeFn | None = None,
    skills: str | None = None,
) -> TurnResult:
    """Run one draw-with-me turn and persist the result. See module docstring."""
    call = claude_fn or _default_claude
    skills_text = skills if skills is not None else _pinned_skill()
    source_chunk, vocab_chunk, notes_chunk = _docs(store, ref.id)
    current_svg = source_chunk.text if source_chunk is not None else ""
    vocab = vocab_chunk.text if vocab_chunk is not None else ""
    notes = notes_chunk.text if notes_chunk is not None else ""
    viewbox = _viewbox(ref, current_svg)
    findings = lint_svg(current_svg, viewbox) if current_svg else []

    prompt = build_prompt(
        message=message,
        svg=current_svg,
        vocab=vocab,
        notes=notes,
        findings=findings,
        viewbox=viewbox,
        skills=skills_text,
    )

    reply, new_svg, new_vocab, new_notes, healed = _ask_with_heal(call, prompt)

    changed = False
    final_svg = current_svg
    if new_svg is not None:
        # new_svg is already sanitized + compiles (guaranteed by _ask_with_heal).
        if source_chunk is not None:
            store.edit_text(source_chunk.handle, new_svg, kind="figure")
        else:
            store.add_chunks(
                ref_id=ref.id,
                chunk_kind="figure_node",
                text=new_svg,
                meta={"no_index": "true"},
                split=False,
                kind="figure",
            )
        final_svg = new_svg
        changed = True
        vb = read_viewbox(new_svg)
        if vb is not None:
            store.stamp_ref_meta(ref.id, {"viewbox": list(vb)})

    final_vocab = _persist_doc(
        store, ref.id, vocab_chunk, "figure_vocab", new_vocab, vocab, index=True
    )
    final_notes = _persist_doc(
        store, ref.id, notes_chunk, "figure_notes", new_notes, notes, index=False
    )

    _persist_turn(store, ref.id, message, reply)
    final_findings = lint_svg(final_svg, viewbox) if final_svg else []
    return TurnResult(
        reply=reply,
        svg=final_svg,
        findings=final_findings,
        changed=changed,
        healed=healed,
        vocab=final_vocab,
        notes=final_notes,
    )


def _persist_doc(
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
        store.edit_text(chunk.handle, new_text, kind="figure")
    else:
        store.add_chunks(
            ref_id=ref_id,
            chunk_kind=chunk_kind,
            text=new_text,
            meta=None if index else {"no_index": "true"},
            split=False,
            kind="figure",
        )
    return new_text


def _ask_with_heal(
    call: ClaudeFn,
    prompt: str,
) -> tuple[str, str | None, str | None, str | None, bool]:
    """Call the model; auto-heal one compile failure. Returns
    ``(reply, sanitized_svg_or_None, vocab_or_None, notes_or_None, healed)``.

    ``sanitized_svg`` is ``None`` when the model changed nothing OR never
    produced compilable SVG (the caller keeps the old source either way — a
    broken reply must not overwrite good work)."""
    data = _safe_call(call, prompt)
    reply = str(data.get("reply") or "").strip()
    vocab_out = _str_or_none(data.get("vocab"))
    notes_out = _str_or_none(data.get("notes"))

    raw_svg = data.get("svg")
    if not isinstance(raw_svg, str) or not raw_svg.strip():
        return reply, None, vocab_out, notes_out, False  # chat-only turn

    ok = _clean_if_valid(raw_svg)
    if ok is not None:
        return reply, ok, vocab_out, notes_out, False

    # One bounded auto-heal: re-prompt with the parse error.
    err = parse_error(raw_svg) or "the SVG did not parse"
    heal_prompt = (
        f"{prompt}\n\n## Your previous reply's SVG was rejected\n{err}\n"
        "Return corrected JSON (same shape). The <svg> must be well-formed XML."
    )
    data2 = _safe_call(call, heal_prompt)
    reply2 = str(data2.get("reply") or reply).strip()
    vocab_out2 = _str_or_none(data2.get("vocab")) or vocab_out
    notes_out2 = _str_or_none(data2.get("notes")) or notes_out
    raw2 = data2.get("svg")
    if isinstance(raw2, str) and raw2.strip():
        ok2 = _clean_if_valid(raw2)
        if ok2 is not None:
            return reply2, ok2, vocab_out2, notes_out2, True
    # Still broken — keep the old source, surface nothing new to storage.
    log.warning("figure turn: model SVG failed to compile after auto-heal")
    return reply2, None, vocab_out2, notes_out2, True


def _str_or_none(v: Any) -> str | None:
    return v if isinstance(v, str) else None


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
        log.warning("figure turn: model call failed: %s", exc)
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


def _clean_if_valid(raw_svg: str) -> str | None:
    """Sanitize ``raw_svg`` and return it iff it compiles, else ``None``."""
    if parse_error(raw_svg) is not None:
        return None
    try:
        cleaned = sanitize_svg(raw_svg)
    except SvgError:
        return None
    return cleaned if parse_error(cleaned) is None else None


def _persist_turn(store: _StoreLike, ref_id: int, message: str, reply: str) -> None:
    """Append the ``figure_turn`` chat chunk (embedded, resumable)."""
    text = f"user: {message.strip()}\n\nassistant: {reply.strip()}"
    store.add_chunks(
        ref_id=ref_id,
        chunk_kind="figure_turn",
        text=text,
        at={"last": True},
        split=False,
        kind="figure",
    )
