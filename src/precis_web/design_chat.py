"""The design-chat panel's web glue — what ``/se/{slug}`` and
``/structure/{slug}`` share around :mod:`precis_web.design_turn`
(the design-workbench build, slice 3 (2026-09-18)).

Two POSTs per kind, registered in each kind's own route module:

* ``POST /{kind}/{slug}/chat`` — form ``message`` (required) +
  ``handles`` (the clicked block names / atom labels, comma- or
  space-separated); runs :func:`design_turn.run_turn` off the event loop
  and 303s back to the page with the outcome in the query string
  (:func:`redirect_after`): ``?turn=N`` for an accepted turn (the panel
  highlights block N), ``?chat_error=…`` for a rejected one (a rejected
  turn writes no transcript block, so the message has nowhere else to
  live), ``?chat_note=…`` for the valid "cannot be expressed as ops"
  reply (no ops, nothing written, the rationale still worth reading).
  ``?rev=N`` naming a past revision is refused with a 409 — a turn edits
  the CURRENT tree, never the one on screen.
* ``POST /{kind}/{slug}/chat/apply`` — form ``ops`` (JSON list) + ``turn``
  (the proposing handle); :func:`design_turn.apply_proposal`. The human
  Apply for the propose-only op classes.

:func:`panel_context` is the template context for the shared partial
``templates/_design_chat.html.j2``: the transcript newest-last, the
pending proposal (:func:`design_turn.pending_proposal`) with its ops
pretty-printed one per line, and the two form URLs. No session state —
everything is re-read from the store on each GET, so a reload after the
redirect shows exactly what was written.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import quote, urlencode

from fastapi import Request
from fastapi.responses import RedirectResponse

from precis.design import history as design_history
from precis.dispatch import Hub
from precis_web import design_turn
from precis_web.deps import get_runtime, get_store, templates
from precis_web.design_turn import DesignKind, TranscriptTurn, TurnResult
from precis_web.timefmt import ago as _ago

#: A flashed error/note is a URL query value — cap it so a validator
#: message that lists a whole roster can't make an unusable URL.
_FLASH_MAX = 600

_HANDLE_SPLIT = re.compile(r"[,\s]+")


def hub_for(request: Request) -> Hub:
    """The app's :class:`Hub` — the runtime's when it has one (the served
    app, ``runtime_with_store`` in tests), else a bare store-backed hub
    (the web fakes' runtime has none)."""
    runtime = get_runtime(request)
    hub = getattr(runtime, "hub", None)
    if isinstance(hub, Hub):
        return hub
    return Hub(store=get_store(request))


def chat_error(
    request: Request, *, kind: DesignKind, slug: str, detail: str, status: int
) -> Any:
    """The chat POSTs' non-redirect error response — ``routes/structure.py``
    and ``routes/blocktree_view.py`` each had their own near-identical copy
    of this (the title string was the only difference); shared here."""
    return templates.TemplateResponse(
        request,
        "error.html.j2",
        {"title": f"{kind} {slug!r}: design chat", "detail": detail, "status": status},
        status_code=status,
    )


def parse_handles(raw: str) -> list[str]:
    """``"@fork, #12 aPd1"`` → ``["fork", "#12", "aPd1"]``: split on commas
    and whitespace, drop the ``@`` the textarea mention carries, dedupe
    keeping first-seen order."""
    out: list[str] = []
    for tok in _HANDLE_SPLIT.split(raw or ""):
        tok = tok.strip()
        if tok.startswith("@"):
            tok = tok[1:]
        if tok and tok not in out:
            out.append(tok)
    return out


def parse_ops_form(raw: str) -> tuple[list[dict[str, Any]] | None, str | None]:
    """The Apply form's ``ops`` field: ``(ops, None)`` or ``(None, why)``."""
    try:
        parsed = json.loads(raw or "")
    except json.JSONDecodeError as exc:
        return None, f"ops is not JSON: {exc}"
    if not isinstance(parsed, list) or not all(
        isinstance(op, dict) and isinstance(op.get("op"), str) for op in parsed
    ):
        return None, 'ops must be a JSON list of {"op": ...} objects'
    return parsed, None


def page_url(kind: DesignKind, slug: str) -> str:
    return f"/{kind}/{quote(slug, safe='')}"


def redirect_flash(
    kind: DesignKind,
    slug: str,
    *,
    turn: str | None = None,
    error: str | None = None,
    note: str | None = None,
) -> RedirectResponse:
    """303 back to the design page, anchored on the panel, with at most one
    of ``turn`` (a transcript handle — the panel highlights its block),
    ``error`` or ``note`` in the query string (module docstring)."""
    params: dict[str, str] = {}
    if turn is not None:
        params["turn"] = turn.rsplit("~", 1)[-1]
    elif error:
        params["chat_error"] = error[:_FLASH_MAX]
    elif note:
        params["chat_note"] = note[:_FLASH_MAX]
    url = page_url(kind, slug)
    if params:
        url += "?" + urlencode(params)
    return RedirectResponse(url=url + "#design-chat", status_code=303)


def redirect_after(kind: DesignKind, slug: str, result: TurnResult) -> RedirectResponse:
    """The redirect after :func:`design_turn.run_turn`: a transcript block
    exists (applied, or a proposal — valid or not; the badge carries the
    dry-run error) → ``turn``; a rejection → ``error``; the no-ops
    "cannot be expressed" reply → ``note``."""
    if result.turn is not None:
        return redirect_flash(kind, slug, turn=result.turn)
    if result.error is not None:
        return redirect_flash(kind, slug, error=result.error)
    return redirect_flash(kind, slug, note=result.rationale or None)


def redirect_after_apply(
    kind: DesignKind, slug: str, result: TurnResult
) -> RedirectResponse:
    """After :func:`design_turn.apply_proposal`: the new revision's turn on
    success (the transcript block already exists), else the error."""
    if result.applied:
        return redirect_flash(kind, slug, turn=result.turn)
    return redirect_flash(kind, slug, error=result.error or "apply failed")


# ── panel context ──────────────────────────────────────────────────────


def _op_atoms(op: dict[str, Any]) -> list[str]:
    """The existing-atom labels a structure op touches — the page's
    ``data-atoms`` hover-highlight targets (mirrors the proposal box's
    ``opAtoms`` in ``structure/detail.html.j2``)."""
    out: list[str] = []
    for key in ("atom", "i", "j", "from", "to"):
        v = op.get(key)
        if isinstance(v, str):
            out.append(v)
    atoms = op.get("atoms")
    if isinstance(atoms, list):
        out.extend(str(a) for a in atoms)
    return out


def _op_rows(kind: DesignKind, ops: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "name": str(op.get("op", "?")),
            "json": json.dumps(op, sort_keys=True),
            "atoms": ",".join(_op_atoms(op)) if kind == "structure" else "",
        }
        for op in ops
    ]


def _turn_row(
    kind: DesignKind, t: TranscriptTurn, applied: dict[str, int]
) -> dict[str, Any]:
    """``applied`` maps a turn handle to the revision that carries it — a
    proposal the human applied shows its revision link even though its
    transcript block (written at propose time) says "proposal"."""
    return {
        "ord": t.ord,
        "handle": t.handle,
        "message": t.message,
        "handles": t.handles,
        "rationale": t.rationale,
        "ops": _op_rows(kind, t.ops),
        "outcome": t.outcome,
        "revision": t.revision if t.revision is not None else applied.get(t.handle),
        "proposal": t.proposal,
        "valid": t.valid,
        "error": t.error,
        "created": _ago(t.created_at) if t.created_at else None,
    }


def panel_context(
    request: Request,
    *,
    kind: DesignKind,
    slug: str,
    read_only: bool,
    rev_qs: str = "",
) -> dict[str, Any]:
    """Everything ``_design_chat.html.j2`` renders. On a past revision
    (``read_only``) the transcript is not read at all — the panel is one
    line pointing at the current revision."""
    store = get_store(request)
    q = request.query_params
    base = page_url(kind, slug)
    ctx: dict[str, Any] = {
        "kind": kind,
        "slug": slug,
        "read_only": read_only,
        "conv_slug": design_turn.conv_slug(slug),
        "chat_url": f"{base}/chat{rev_qs}",
        "chat_apply_url": f"{base}/chat/apply",
        "chat_error": (q.get("chat_error") or "")[:_FLASH_MAX],
        "chat_note": (q.get("chat_note") or "")[:_FLASH_MAX],
        "chat_turn": _int_or_none(q.get("turn")),
        "turns": [],
        "pending": None,
    }
    if read_only:
        return ctx
    turns = design_turn.transcript(store, slug)
    applied = _applied_turns(store, kind, slug)
    ctx["turns"] = [_turn_row(kind, t, applied) for t in turns]
    pending = design_turn.pending_proposal(store, kind=kind, slug=slug, turns=turns)
    if pending is not None:
        ctx["pending"] = {
            **_turn_row(kind, pending, applied),
            # The Apply form posts the ops back verbatim — the route re-gates
            # them against the roster before anything is written.
            "ops_json": json.dumps(pending.ops),
        }
    return ctx


def _applied_turns(store: Any, kind: DesignKind, slug: str) -> dict[str, int]:
    """``{turn handle: rev}`` over the design's revision rows."""
    ref = store.get_ref(kind=kind, id=slug)
    if ref is None:
        return {}
    return {
        r.turn: r.rev for r in design_history.list_revisions(store, ref.id) if r.turn
    }


def _int_or_none(raw: str | None) -> int | None:
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


__all__ = [
    "chat_error",
    "hub_for",
    "page_url",
    "panel_context",
    "parse_handles",
    "parse_ops_form",
    "redirect_after",
    "redirect_after_apply",
    "redirect_flash",
]
