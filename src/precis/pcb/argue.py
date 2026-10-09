"""Argue with a pcb design: handle grammar, resolution, the LLM turn.

docs/backlog/pcb-argue-with-design.md (Reto, 2026-09-15): one text box
on the board page accumulates *handles* as the user clicks the render —
"this pad, not that one" typed in the user's own sentence order, in a
form an agent resolves without guessing. This module is the kind-side
half: what a handle is, how it resolves against the stored board, and
how the argument is put to the model. The web route
(:mod:`precis_web.routes.pcb`) and the fab renderer
(:func:`precis.pcb.gerber_view.render_fab_svg`, which stamps the same
grammar onto its elements as ``data-handle``) both lean on it.

**Grammar — one notation, no synonyms.** ``U_TEMP`` is
``pcb_instances.refdes``; ``U_TEMP.3`` / ``ARR1.R3C4`` is refdes + pin
(a generated array cell is an ordinary ``refdes.pin`` row — no separate
notation); ``net:HV_RAIL`` is ``pcb_nets.name``; ``feature:outline`` is
a ``pcb_features.ftype``. :data:`HANDLE_RE` is the tokenizer that picks
candidate handles out of free text; :func:`resolve` decides which of
them name something on the board. The rule the two sides share: a token
that resolves is a handle, a token that doesn't is just a word ("DRC",
"the") — so ``about`` never carries prose, and a refdes typed by hand
counts the same as one clicked in.

**Submit-time vs read-time.** Handles the *client* says were clicked
(``handles=[...]`` on the POST) must all resolve — a stale click is
answered with the list of valid handles, not stored. Once stored, a
note's ``about`` is name-keyed text resolved at read time, se_notes'
rule: a part retired after the argument reads as a dangling-anchor
report, never an error (:func:`dangling`).

**The LLM turn.** :func:`ask` hands the argument to
:func:`precis.utils.llm.router.route` on the MEDIUM tier with the
resolved context for every handle (instance row + pins + nets +
neighbours for a part; the pin's own net and neighbours for a pad; the
member list for a net) and the design's vitals. The reply is stored as
an ``answer`` note (``origin='proposed'``) re the question, anchored to
the same handles. The question is stored FIRST and verbatim — a model
outage degrades to "recorded, unanswered", never to a lost argument.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

#: Candidate-handle tokenizer. Prefixed forms take everything up to
#: whitespace (net names carry ``/``, ``+``, ``-``); bare forms are an
#: identifier with an optional ``.pin`` suffix.
HANDLE_RE = re.compile(
    r"(?<![\w.:])(?:net:\S+|feature:[A-Za-z_]+|[A-Za-z_][\w-]*(?:\.[\w-]+)?)"
)

#: Valid ``feature:`` suffixes — ``pcb_features.ftype``'s vocabulary.
FEATURE_TYPES = ("mounting_hole", "fiducial", "testpoint", "keepout", "outline")

#: Where the answer comes from, and the cap one argument may cost.
LLM_SOURCE = "pcb-argue"
LLM_MAX_USD = 0.25
LLM_TIMEOUT_S = 90.0


@dataclass
class Resolved:
    """One handle's resolution: ``kind`` ∈ part | pin | net | feature, and
    the context the model gets for it."""

    handle: str
    kind: str
    context: dict[str, Any] = field(default_factory=dict)


def pad_handle(refdes: str, pin: str) -> str:
    return f"{refdes}.{pin}"


def net_handle(name: str) -> str:
    return f"net:{name}"


def feature_handle(ftype: str) -> str:
    return f"feature:{ftype}"


def candidates(text: str) -> list[str]:
    """Every token in ``text`` that *could* be a handle, in order, deduped.
    Trailing sentence punctuation is not part of a handle."""
    out: list[str] = []
    for tok in HANDLE_RE.findall(text):
        tok = tok.rstrip(".,;:!?)")
        if tok and tok not in out:
            out.append(tok)
    return out


def classify(handle: str, valid: dict[str, Any]) -> str | None:
    """``part`` | ``pin`` | ``net`` | ``feature`` for a handle that names
    something in ``valid`` (:meth:`Store.pcb_handles`'s shape), else
    ``None``."""
    if handle.startswith("net:"):
        return "net" if handle[4:] in valid["nets"] else None
    if handle.startswith("feature:"):
        return "feature" if handle[8:] in valid["features"] else None
    if handle in valid["parts"]:
        return "part"
    refdes, dot, pin = handle.partition(".")
    if dot and pin in valid["pins"].get(refdes, ()):
        return "pin"
    return None


def all_handles(valid: dict[str, Any]) -> dict[str, list[str]]:
    """The full valid-handle roster, by class — what a rejected submit is
    answered with."""
    return {
        "parts": list(valid["parts"]),
        "pins": [pad_handle(r, p) for r, pins in valid["pins"].items() for p in pins],
        "nets": [net_handle(n) for n in valid["nets"]],
        "features": [feature_handle(f) for f in valid["features"]],
    }


def resolve(
    store: Any, ref_id: int, handles: list[str]
) -> tuple[list[Resolved], list[str]]:
    """``(resolved, unknown)`` for ``handles`` against the live board.
    Each resolved entry carries the context :func:`ask` quotes to the
    model; ``unknown`` keeps the caller's order."""
    valid = store.pcb_handles(ref_id)
    resolved: list[Resolved] = []
    unknown: list[str] = []
    for h in handles:
        kind = classify(h, valid)
        if kind is None:
            unknown.append(h)
        elif kind == "part":
            resolved.append(Resolved(h, kind, _part_context(store, ref_id, h)))
        elif kind == "pin":
            refdes, _, pin = h.partition(".")
            resolved.append(Resolved(h, kind, _pin_context(store, ref_id, refdes, pin)))
        elif kind == "net":
            resolved.append(
                Resolved(h, kind, store.pcb_net_members(ref_id, h[4:]) or {})
            )
        else:
            resolved.append(
                Resolved(
                    h,
                    kind,
                    {"ftype": h[8:], "rows": store.pcb_features_of_type(ref_id, h[8:])},
                )
            )
    return resolved, unknown


def handles_in(text: str, store: Any, ref_id: int) -> list[str]:
    """The handles ``text`` names: every candidate token that resolves.
    Unresolvable tokens are prose and silently dropped."""
    valid = store.pcb_handles(ref_id)
    return [h for h in candidates(text) if classify(h, valid) is not None]


def dangling(about: list[str], valid: dict[str, Any]) -> list[str]:
    """The anchors of a stored note that no longer resolve — the read-time
    report (never an error) for a part retired after the argument."""
    return [h for h in about if classify(h, valid) is None]


def _part_context(store: Any, ref_id: int, refdes: str) -> dict[str, Any]:
    design = store.pcb_load(ref_id)
    inst: dict[str, Any] = next(
        (i for i in design["instances"] if i["refdes"] == refdes), {}
    )
    hop = store.pcb_instance_neighbors(ref_id, refdes) or {}
    return {
        "instance": {
            k: inst.get(k)
            for k in ("label", "footprint", "layer", "x", "y", "rot", "roles", "note")
        },
        "pins": hop.get("pins", []),
    }


def _pin_context(store: Any, ref_id: int, refdes: str, pin: str) -> dict[str, Any]:
    hop = store.pcb_instance_neighbors(ref_id, refdes) or {}
    row: dict[str, Any] = next((p for p in hop.get("pins", []) if p["pin"] == pin), {})
    return {"refdes": refdes, "pin": row}


_PROMPT = """\
You are reviewing a printed-circuit-board design named {title!r} with a \
human who is arguing with it. The human names parts, pads and nets by \
HANDLE: REFDES (a part), REFDES.PIN (one pad), net:NAME (a net), \
feature:TYPE (board features). Every handle in the argument is listed \
below with what the design database says about it. Rely on that context \
and the vitals; do not invent parts, pins, nets or numbers that are not \
in it.

Design vitals:
{vitals}

Handles and their context:
{context}

The argument:
{text}

Answer in plain prose, at most 200 words, naming handles exactly as \
given. If it is a change request, say concretely what should change \
(which handle, what edit) and what it would affect. If it is a \
geometry or interference concern, say which check settles it and what \
the context already shows. If it is a question the context cannot \
settle, say so plainly — never guess a yes.
"""


def prompt(
    *, title: str, text: str, resolved: list[Resolved], vitals: dict[str, Any]
) -> str:
    ctx = (
        "\n".join(f"- {r.handle} ({r.kind}): {r.context!r}" for r in resolved)
        or "- (none — a whole-design argument)"
    )
    vit = "\n".join(f"- {k}: {v!r}" for k, v in vitals.items()) or "- (none)"
    return _PROMPT.format(title=title, vitals=vit, context=ctx, text=text)


def ask(
    *,
    title: str,
    text: str,
    resolved: list[Resolved],
    vitals: dict[str, Any],
    ref_id: int | None = None,
) -> str:
    """One MEDIUM-tier call; the model's prose answer. Raises on transport
    failure or an empty reply — the caller degrades, this never does."""
    from precis.utils.llm.router import LlmRequest, Tier, route

    res = route(
        LlmRequest(
            tier=Tier.MEDIUM,
            source=LLM_SOURCE,
            prompt=prompt(title=title, text=text, resolved=resolved, vitals=vitals),
            max_usd=LLM_MAX_USD,
            timeout_s=LLM_TIMEOUT_S,
            ref_id=ref_id,
        )
    )
    if res.error:
        raise RuntimeError(res.error)
    answer = (res.text or "").strip()
    if not answer:
        raise RuntimeError("the model returned no answer")
    return answer


def note_name(kind: str, text: str, taken: set[str]) -> str:
    """A short, unique, readable ledger name: ``q-``/``a-``/``d-`` prefix
    + the first few words, deduped with a numeric suffix (the se web
    route's convention)."""
    words = re.findall(r"[a-z0-9]+", text.lower())[:4]
    base = f"{kind[0]}-{'-'.join(words) or 'note'}"
    name, n = base, 2
    while name in taken:
        name = f"{base}-{n}"
        n += 1
    return name


__all__ = [
    "FEATURE_TYPES",
    "HANDLE_RE",
    "Resolved",
    "all_handles",
    "ask",
    "candidates",
    "classify",
    "dangling",
    "feature_handle",
    "handles_in",
    "net_handle",
    "note_name",
    "pad_handle",
    "prompt",
    "resolve",
]
