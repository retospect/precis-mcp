"""One interactive turn on a figure — the draw-with-me loop.

Each turn the model sees the *current* state (the SVG source with its named
``id``s), the two mechanical **lints** (compile + out-of-bounds), the shared
**vocabulary** ("green circles are foos"), and the user's message; it emits
a JSON reply carrying a chat ``reply``, the whole rewritten ``svg`` (cad-
style whole-source rewrite, ADR-simplest for slice 1), and optionally an
updated ``vocab``.

Robustness seams (from the design discussion):

- **Sanitize** every model-authored SVG before it touches storage or a DOM
  (:func:`precis.figure.svg.sanitize_svg`).
- **Bounded auto-heal**: if the reply's SVG doesn't compile, re-prompt once
  with the parse error rather than surfacing a broken canvas; if it still
  fails, keep the *old* source and report the finding (never overwrite good
  source with broken).
- **Conventions are the model's job**: they live in ``vocab`` (in-context
  every turn), not in a checker.

The model call is injected (``claude_fn``) so the whole loop is testable
without a real ``claude`` — the default wraps :func:`call_claude_p`.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
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
    'Reply with ONE JSON object and nothing else: {"reply": "<a short chat '
    'message to the human>", "svg": "<the COMPLETE new <svg>…</svg> source, '
    'or omit/empty to leave the drawing unchanged>", "vocab": "<the updated '
    'shared vocabulary, or omit to leave it unchanged>"}.'
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


@dataclass(frozen=True, slots=True)
class TurnResult:
    """Outcome of one :func:`run_turn`."""

    reply: str
    svg: str  # the current source after the turn (new if changed, else old)
    findings: list[Any]  # list[LintFinding] on the final source
    changed: bool  # did the source SVG change this turn?
    healed: bool  # did an auto-heal retry run?


def _default_claude(prompt: str) -> dict[str, Any]:
    from precis.utils.claude_p import call_claude_p

    res = call_claude_p(
        prompt,
        model=_DEFAULT_MODEL,
        max_usd=float(os.environ.get("PRECIS_FIGURE_MAX_USD", "1.0")),
        timeout_s=float(os.environ.get("PRECIS_FIGURE_TIMEOUT_S", "300")),
    )
    return res.data


def _docs(store: _StoreLike, ref_id: int) -> tuple[Any | None, Any | None]:
    """Return ``(source_chunk, vocab_chunk)`` — the figure's two documents."""
    source = vocab = None
    for c in store.reading_order(ref_id, kind="figure"):
        if c.chunk_kind == "figure_node" and source is None:
            source = c
        elif c.chunk_kind == "figure_vocab" and vocab is None:
            vocab = c
    return source, vocab


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
    findings: list[Any],
    viewbox: tuple[float, float, float, float],
    skills: str = "",
) -> str:
    """Assemble the turn prompt. Pure — tested for carrying each part.

    Ordering mirrors the cached/variable split (skills + contract are stable;
    state + lint + message are per-turn), though call_claude_p sends it as
    one blob.
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
        "You are drawing an SVG figure together with a human. You own two "
        "documents: the SVG source and the shared vocabulary. Edit by "
        "rewriting the whole <svg>. Name elements with stable id= attributes "
        "and <title> (NOT XML comments — those are stripped). Never use "
        "<script>, <foreignObject>, event handlers, or external/data href — "
        "they are stripped for safety. Keep shapes inside the viewBox."
    )
    parts.append(f"## Canvas\nviewBox = {x} {y} {w} {h}")
    parts.append(f"## Shared vocabulary\n{vocab or '(empty — define terms as you go)'}")
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
    skills: str = "",
) -> TurnResult:
    """Run one draw-with-me turn and persist the result. See module docstring."""
    call = claude_fn or _default_claude
    source_chunk, vocab_chunk = _docs(store, ref.id)
    current_svg = source_chunk.text if source_chunk is not None else ""
    vocab = vocab_chunk.text if vocab_chunk is not None else ""
    viewbox = _viewbox(ref, current_svg)
    findings = lint_svg(current_svg, viewbox) if current_svg else []

    prompt = build_prompt(
        message=message,
        svg=current_svg,
        vocab=vocab,
        findings=findings,
        viewbox=viewbox,
        skills=skills,
    )

    reply, new_svg, new_vocab, healed = _ask_with_heal(call, prompt, viewbox)

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

    if new_vocab is not None and new_vocab.strip() and new_vocab != vocab:
        if vocab_chunk is not None:
            store.edit_text(vocab_chunk.handle, new_vocab, kind="figure")
        else:
            store.add_chunks(
                ref_id=ref.id,
                chunk_kind="figure_vocab",
                text=new_vocab,
                split=False,
                kind="figure",
            )

    _persist_turn(store, ref.id, message, reply)
    final_findings = lint_svg(final_svg, viewbox) if final_svg else []
    return TurnResult(
        reply=reply,
        svg=final_svg,
        findings=final_findings,
        changed=changed,
        healed=healed,
    )


def _ask_with_heal(
    call: ClaudeFn,
    prompt: str,
    viewbox: tuple[float, float, float, float],
) -> tuple[str, str | None, str | None, bool]:
    """Call the model; auto-heal one compile failure. Returns
    ``(reply, sanitized_svg_or_None, vocab_or_None, healed)``.

    ``sanitized_svg`` is ``None`` when the model changed nothing OR when it
    never produced compilable SVG (the caller keeps the old source either
    way — a broken reply must not overwrite good work)."""
    data = _safe_call(call, prompt)
    reply = str(data.get("reply") or "").strip()
    vocab = data.get("vocab")
    vocab_out = str(vocab) if isinstance(vocab, str) else None

    raw_svg = data.get("svg")
    if not isinstance(raw_svg, str) or not raw_svg.strip():
        return reply, None, vocab_out, False  # chat-only turn

    ok = _clean_if_valid(raw_svg)
    if ok is not None:
        return reply, ok, vocab_out, False

    # One bounded auto-heal: re-prompt with the parse error.
    err = parse_error(raw_svg) or "the SVG did not parse"
    heal_prompt = (
        f"{prompt}\n\n## Your previous reply's SVG was rejected\n{err}\n"
        "Return corrected JSON (same shape). The <svg> must be well-formed XML."
    )
    data2 = _safe_call(call, heal_prompt)
    reply2 = str(data2.get("reply") or reply).strip()
    vocab2 = data2.get("vocab")
    vocab_out2 = str(vocab2) if isinstance(vocab2, str) else vocab_out
    raw2 = data2.get("svg")
    if isinstance(raw2, str) and raw2.strip():
        ok2 = _clean_if_valid(raw2)
        if ok2 is not None:
            return reply2, ok2, vocab_out2, True
    # Still broken — keep the old source, surface nothing new to storage.
    log.warning("figure turn: model SVG failed to compile after auto-heal")
    return reply2, None, vocab_out2, True


def _safe_call(call: ClaudeFn, prompt: str) -> dict[str, Any]:
    """Invoke the model; coerce any non-dict / error into an empty dict so a
    turn never crashes the request (the caller degrades to a chat-only turn)."""
    try:
        out = call(prompt)
    except Exception as exc:  # model/parse/subprocess failure
        log.warning("figure turn: model call failed: %s", exc)
        return {"reply": f"(the model call failed: {exc})"}
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
