"""The MCP surface of graph trust state, slice 1 (migration 0214;
docs/backlog/graph-node-trust-state.md).

Two doors, both on existing verbs (no new verb):

* ``args={'trust': {...}}`` on ``put`` / ``edit`` — the typed provenance
  payload. ``origin`` overrides the kind's producer origin (``extracted`` |
  ``computed`` | ``generated``; ``human`` is refused: an MCP caller shares
  one credential and cannot prove a human typed the payload — human origin
  comes only from an authenticated human ingress, the web UI).
  ``confidence='considered'`` with a ``reason`` on ``put`` is the explicit
  create-time consider act, bound to the new ref of the verb's kind.
* ``edit(kind=K, id=X, mode='trust', args={'trust': {...}})`` — a trust-only
  act on an existing ref, or on a link with ``args={'trust': {..., 'link':
  N}}`` (the ``link`` tool has no ``args=``, so a link is considered here
  after it exists): ``confidence='considered'`` (consider) or ``'hunch'``
  (demote), with ``reason`` and ``expected_sha`` (the content sha the
  decision was made on; ``get`` renders it). ``correct-origin`` and
  ``legacy-import`` are operator acts (``precis trust``), not MCP ones.

Writes made through a verb on a kind whose handler declares
:attr:`precis.protocol.Handler.trust_origin` run as that producer (``link``
included: it runs on the source kind's handler); other kinds stay
unconverted (unknown/unknown version 0) until they declare one.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal

from precis.errors import BadInput, NotFound
from precis.filing_origin import writer_label
from precis.response import Response
from precis.store.revision_context import (
    KG_REVIEW_PREFIX,
    TRUST_ORIGINS,
    RevisionContext,
    TrustAct,
    parse_kg_review,
)

#: Keys the ``trust`` payload accepts.
_PAYLOAD_KEYS = frozenset(
    {"origin", "confidence", "reason", "expected_sha", "link", "admit"}
)

#: Verbs that accept the payload.
TRUST_VERBS = frozenset({"put", "edit"})

#: Verbs that accept ``args={'writer': {...}}``: the ones with an ``args=``
#: that change a ref's content or lifecycle.
WRITER_VERBS = frozenset({"put", "edit", "delete"})

#: A model id as clients name it (``claude-opus-5-5``, ``openai/gpt-oss-120b``,
#: ``us.anthropic.claude-sonnet-5-5[1m]``); a person as a short label.
_WRITER_PATTERNS = {
    "model": re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/\[\]-]{0,127}"),
    "human": re.compile(r"[\w][\w .:@-]{0,63}"),
}

#: The once-per-session ack line on a memory write with no known model.
WRITER_NUDGE = (
    "writer: model unknown — pass args={'writer': {'model': '<your model id>'}} "
    "on memory writes; add 'human': '<who>' when the content is a person's "
    "direct input (precis-memory-help)"
)


def pop_trust_payload(args: dict[str, Any], extras_key: str) -> dict[str, Any] | None:
    """Remove and validate ``args={'trust': {...}}`` before the handler's
    kwargs gate sees it."""
    extras = args.get(extras_key)
    if not isinstance(extras, dict) or "trust" not in extras:
        return None
    payload = extras.pop("trust")
    if not extras:
        args.pop(extras_key)
    if not isinstance(payload, dict):
        raise BadInput(
            "args={'trust': ...} is a dict: {'confidence': 'considered', "
            "'reason': '...'} or {'origin': 'extracted'}"
        )
    unknown = sorted(set(payload) - _PAYLOAD_KEYS)
    if unknown:
        raise BadInput(
            f"trust payload keys {unknown!r} not accepted",
            options=sorted(_PAYLOAD_KEYS),
        )
    origin = payload.get("origin")
    if origin is not None:
        if origin == "human":
            raise BadInput(
                "an MCP call cannot assert human origin: it shares one "
                "credential, so nothing proves a human typed the payload",
                next="human notes come from the web UI; an agent's own text is "
                "'generated', a quotation 'extracted', a calculation 'computed'",
            )
        if origin not in TRUST_ORIGINS:
            raise BadInput(
                f"unknown trust origin {origin!r}",
                options=[o for o in TRUST_ORIGINS if o != "human"],
            )
    confidence = payload.get("confidence")
    if confidence is not None and confidence not in ("considered", "hunch"):
        raise BadInput(
            f"trust confidence is 'considered' or 'hunch', not {confidence!r}",
            options=["considered", "hunch"],
        )
    if confidence == "considered" and not str(payload.get("reason") or "").strip():
        raise BadInput(
            "considered is an explicit act: give a reason",
            next="args={'trust': {'confidence': 'considered', 'reason': 'why'}}",
        )
    reason = str(payload.get("reason") or "").strip()
    if reason.startswith(KG_REVIEW_PREFIX) and parse_kg_review(reason) is None:
        raise BadInput(
            f"a kg-review reason is 'kg-review:<model>:<YYYY-MM-DD>:ok|fixed'"
            f", got {reason!r}",
            next="reason='kg-review:claude-opus-5-5:2026-10-22:ok' "
            "(reviewer model, UTC review date, ok|fixed)",
        )
    return payload


def pop_writer_payload(args: dict[str, Any], extras_key: str) -> dict[str, str] | None:
    """Remove and validate ``args={'writer': {...}}`` before the handler's
    kwargs gate sees it. The values are labelled claims, not proof."""
    extras = args.get(extras_key)
    if not isinstance(extras, dict) or "writer" not in extras:
        return None
    payload = extras.pop("writer")
    if not extras:
        args.pop(extras_key)
    if not isinstance(payload, dict):
        raise BadInput(
            "args={'writer': ...} is a dict: {'model': '<your model id>'} and/or "
            "{'human': '<who said it>'}"
        )
    unknown = sorted(set(payload) - set(_WRITER_PATTERNS))
    if unknown:
        raise BadInput(
            f"writer payload keys {unknown!r} not accepted",
            options=sorted(_WRITER_PATTERNS),
        )
    for key, value in payload.items():
        if not isinstance(value, str) or not _WRITER_PATTERNS[key].fullmatch(value):
            raise BadInput(
                f"writer {key} {value!r} is not a plain label",
                next="args={'writer': {'model': 'claude-opus-5-5', 'human': 'reto'}}",
            )
    return payload


def written_line(store: Any, kind: str, ident: Any) -> str | None:
    """``written: <first>`` or ``written: <first> · last: <last>`` for one
    ref, or None when no revision carries a stamp."""
    try:
        ref_id = _target_ref_id(store, kind, ident)
    except (NotFound, ValueError):
        return None
    stamps = store.first_last_writer("ref", ref_id)
    if stamps is None:
        return None
    first, last = (writer_label(stamp) for stamp in stamps)
    return f"written: {first}" if first == last else f"written: {first} · last: {last}"


def producer_kwargs(
    verb: str,
    kind: str,
    handler_origin: str | None,
    payload: dict[str, Any] | None,
    outer: RevisionContext | None,
) -> dict[str, Any]:
    """The trust part of the verb's revision context.

    An outer context's origin wins (the web UI's human ingress, a worker
    calling the runtime) — except that a human ingress does not relabel a
    kind whose content the person did not type; then the payload's
    ``origin``; then the kind's declared producer. A create-time consider
    act rides along when the payload asks for considered on ``put``."""
    kwargs: dict[str, Any] = {}
    origin = None if outer is None else outer.trust_origin
    if origin == "human" and handler_origin != "generated":
        # The person typed the request, not the content: an acquisition or
        # computation keeps its own producer, an unconverted kind stays
        # unconverted ("" clears the inherited origin for this call).
        origin = handler_origin
        kwargs["trust_origin"] = handler_origin or ""
    elif not origin:
        origin = (payload or {}).get("origin") or handler_origin
        if origin is not None:
            kwargs["trust_origin"] = origin
            kwargs["trust_actor_type"] = "agent"
    if payload and payload.get("confidence") == "considered":
        if verb == "put":
            act = TrustAct(
                action="consider",
                target_kind="ref",
                create_match=kind,
                reason=str(payload["reason"]),
            )
        else:
            raise BadInput(
                "considered on an existing node is edit(mode='trust') with "
                "expected_sha, not a content edit",
                next=f"edit(kind={kind!r}, id=..., mode='trust', args={{'trust': "
                "{'confidence': 'considered', 'reason': '...', 'expected_sha': '...'}})",
            )
        if not origin:
            raise BadInput(
                f"kind {kind!r} has no trust producer yet; considered on create "
                "needs one",
                next="add args={'trust': {'origin': 'generated', ...}}",
            )
        kwargs["trust_act"] = act
        review = parse_kg_review(act.reason)
        if review is not None:
            kwargs["model"] = review["model"]
    return kwargs


def create_receipt(store: Any, act: TrustAct) -> str:
    """One line naming where a create-time act landed, or that it did not
    (the put deduplicated onto an existing row, so nothing new was made)."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT r.target_kind, r.target_id FROM revisions r "
            " WHERE r.trust_acts @> %s::jsonb ORDER BY r.revision_id LIMIT 1",
            (json.dumps([{"op_id": act.op_id}]),),
        ).fetchone()
    if row is None:
        return (
            f"trust: considered NOT recorded (op {act.op_id}): no new "
            f"{act.create_match} row was created — consider the existing one "
            "with edit(mode='trust')"
        )
    return f"trust: {row[0]} {row[1]} recorded considered (op {act.op_id})"


def _target_ref_id(store: Any, kind: str, ident: Any) -> int:
    raw = str(ident).strip()
    resolved = store.resolve_handle(raw)
    if resolved is not None:
        return int(resolved.ref_id)
    ref = store.get_ref(kind=kind, id=int(raw) if raw.isdigit() else raw)
    if ref is None:
        raise NotFound(f"no {kind} {raw!r} to record a trust act on")
    return int(ref.id)


@dataclass(frozen=True, slots=True)
class TrustFooter:
    """The resolved stamp behind one ``get`` trust line. ``line`` is the
    text an agent reads — the consider call spelled out and runnable; the
    parts let a surface link that call instead of printing it. ``recipe``
    is that runnable call alone — ``None`` when there is no sha to name —
    so a serve that already showed it can print only :attr:`short`, the
    stamp and sha ``edit(mode='trust')`` still needs (gr499041)."""

    kind: str
    ref_id: int
    stamp: str
    sha: str | None
    line: str
    recipe: str | None = None

    @property
    def short(self) -> str:
        """``line`` without the `` — edit(...)`` recipe (~50 bytes)."""
        if self.recipe is None:
            return self.line
        return self.line.removesuffix(f" — {self.recipe}")


def trust_footer(store: Any, kind: str, ident: Any) -> TrustFooter | None:
    """Resolve ``ident`` to a ref and read its trust state, or ``None``
    when it names no ref of ``kind``."""
    try:
        ref_id = _target_ref_id(store, kind, ident)
    except (NotFound, ValueError):
        return None
    state = store.trust_state("ref", ref_id)
    if state is None:
        return None
    stamp = state.label if state.version else f"{state.label} (unstamped)"
    try:
        label = store.definition_label("ref", ref_id)
    except Exception:
        label = None
    if label:
        stamp += f" · {label}"
    short = f"trust: {stamp}"
    recipe = None
    if state.sha:
        short += f" · sha {state.sha}"
        recipe = (
            f"edit(kind={kind!r}, id={ref_id}, mode='trust', "
            "args={'trust': {'confidence': 'considered', 'reason': "
            f"'<why you trust it>', 'expected_sha': '{state.sha}'}}}})"
        )
    line = short if recipe is None else f"{short} — {recipe}"
    return TrustFooter(
        kind=kind,
        ref_id=ref_id,
        stamp=stamp,
        sha=state.sha,
        line=line,
        recipe=recipe,
    )


def trust_line(store: Any, kind: str, ident: Any) -> str | None:
    """``trust: generated · hunch · sha 1a2b… — edit(<the consider call,
    runnable)>`` for one ref, or None when ``ident`` names no ref of
    ``kind``. Version 0 reads ``unknown · unknown (unstamped)``: legacy
    or an unconverted writer, never a guess."""
    footer = trust_footer(store, kind, ident)
    return None if footer is None else footer.line


def edit_trust(
    store: Any,
    *,
    kind: str,
    ident: Any,
    payload: dict[str, Any] | None,
    outer: RevisionContext | None,
    actor: str,
) -> Response:
    """``edit(mode='trust')``: one consider/demote act on an existing ref or
    link, answered with its receipt."""
    if store is None:
        raise BadInput("edit(mode='trust') needs a database-backed server")
    if not payload:
        raise BadInput(
            "edit(mode='trust') needs args={'trust': {'confidence': "
            "'considered'|'hunch', 'reason': ..., 'expected_sha': ...}}"
        )
    if payload.get("origin") is not None:
        raise BadInput(
            "origin corrections are operator acts (precis trust correct-origin), "
            "not an MCP edit"
        )
    if payload.get("admit"):
        reason = str(payload.get("reason") or "").strip()
        if not reason:
            raise BadInput("the admit act needs a reason")
        if ident is None:
            raise BadInput("admit needs id= of the ref to admit")
        ref_id = _target_ref_id(store, kind, ident)
        actor_type = (outer.trust_actor_type if outer is not None else None) or "agent"
        out = store.admit_ref(ref_id, reason=reason, actor=actor, actor_type=actor_type)
        act = out["act"]
        return Response(
            body=f"trust: ref {ref_id} admitted — act admit op "
            f"{act.get('op_id')}"
            + (" (replay: already recorded)" if out.get("replayed") else "")
        )
    confidence = payload.get("confidence")
    if confidence is None:
        raise BadInput("edit(mode='trust') names a confidence: considered or hunch")
    reason = str(payload.get("reason") or "").strip()
    if not reason:
        raise BadInput("a trust act needs a reason")
    expected = str(payload.get("expected_sha") or "").strip()
    if not expected:
        raise BadInput(
            "a trust act names the content sha it was decided on (expected_sha)",
            next=f"get(kind={kind!r}, id={ident!r}) renders it on the trust line",
        )
    target_kind: Literal["ref", "link"] = "ref"
    if payload.get("link") is not None:
        target_kind, target_id = "link", int(payload["link"])
    else:
        if ident is None:
            raise BadInput("edit(mode='trust') needs id=")
        target_id = _target_ref_id(store, kind, ident)
    act = TrustAct(
        action="consider" if confidence == "considered" else "demote",
        target_kind=target_kind,
        target_id=target_id,
        expected_sha=expected,
        confidence=None if confidence == "considered" else "hunch",
        reason=reason,
    )
    actor_type = (outer.trust_actor_type if outer is not None else None) or "agent"
    review = parse_kg_review(reason)
    receipt = store.record_trust_act(
        act,
        actor=actor,
        actor_type=actor_type,
        model=review["model"] if review is not None else None,
    )
    before = receipt.act.get("before") or {}
    lines = [
        f"trust: {target_kind} {target_id} {before.get('confidence', '?')} -> "
        f"{receipt.state.confidence} ({receipt.state.origin}), "
        f"act {receipt.act.get('action')} op {receipt.act.get('op_id')}"
        + (" (replay: already recorded)" if receipt.replayed else ""),
        f"by {receipt.act.get('actor')} ({receipt.act.get('actor_type')}) "
        f"at {receipt.act.get('at')}; sha {receipt.state.sha}",
    ]
    return Response(body="\n".join(lines))
