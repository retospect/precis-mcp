"""``get(kind='finding', id='fi<id>', view='mint-preflight')`` — run the real
mint gates against a candidate payload, read-only.

`docs/backlog/nanopub-mcp-surface-gaps.md` §1 ("do this one"), measured
during the 124-hub nanobud campaign: `nanopub/gates.py::run_mint_gates` was
callable only from `mint.py::approve` and the CLI, so agents preparing
approve payloads **reimplemented the gates locally** — verbatim-quote
containment, the citation-marker regex, snip validity and uniqueness across
body chunks, the pdf-sha pin. That mirror got batch B to 21/21 approvals with
zero refusals, and it is exactly the kind of copy that rots silently: the
2026-08-16 citation-marker gate would have invalidated any older one.

This door runs the gates themselves. No state change, no policy conflict
with the human-only approve line — a violation list is the whole output. The
CLI sibling already exists for publish-time gates (`nanopub preflight`); this
is the mint-time half.

It is also how an agent proposing a hypothesis
(:mod:`precis.handlers._finding_hypothesis`) checks its own work before
leaving it in the queue, so a proposal that cannot pass is dropped rather
than filed. The gates are severe by design — measured over the live corpus,
only 21 of 1,524 hubs lint clean, and `no-epistemic-mode` alone hits 1,419
— so a refusal here is the normal outcome, not a malfunction.
"""

from __future__ import annotations

from typing import Any

from precis.errors import BadInput
from precis.handlers._finding_hypothesis import META_PROPOSED_PAYLOAD
from precis.response import Response
from precis.store import Store
from precis.store.types import Ref
from precis.utils import handle_registry


def _default_payload(store: Store, ref: Ref) -> dict[str, Any]:
    """The payload to gate when the caller supplied none.

    Preference order mirrors what a reviewer would see on the approve form:
    the frozen envelope once one exists, else an agent's parked proposal,
    else an empty payload (which gates the sentence and the hub's own state
    — still the most useful answer for a hub nobody has prepared yet).
    """
    row = store.nanopub_publish_row(ref.id)
    if row is not None and row.grounding:
        return dict(row.grounding)
    parked = (ref.meta or {}).get(META_PROPOSED_PAYLOAD)
    return dict(parked) if isinstance(parked, dict) else {}


def render_mint_preflight(store: Store, ref: Ref, *, payload: Any = None) -> Response:
    """Run `run_mint_gates` against ``payload`` and render the violations."""
    from precis.nanopub import evidence as ev
    from precis.nanopub.gates import run_mint_gates

    if payload is not None and not isinstance(payload, dict):
        raise BadInput(
            f"payload must be a dict, got {type(payload).__name__}",
            next=(
                "get(kind='finding', id='fi123', view='mint-preflight', "
                "args={'payload': {'hypothesis': True, 'passages': [], ...}})"
            ),
        )
    resolved = payload if payload is not None else _default_payload(store, ref)

    bundle = ev.load_bundle(store, ref.id)
    violations = run_mint_gates(store, bundle, resolved)

    handle = handle_registry.format_handle("finding", ref.id)
    artifact = "hypothesis" if resolved.get("hypothesis") else bundle.artifact_type
    header = f"mint-preflight {handle} ({artifact})"
    source = (
        "supplied payload"
        if payload is not None
        else "parked/frozen payload"
        if resolved
        else "no payload (sentence + hub state only)"
    )

    if not violations:
        return Response(
            body=(
                f"{header}\n"
                f"payload: {source}\n"
                "PASS — no blocking gate violations.\n"
                "This is admissibility, not truth: it means well-formed, "
                "sourced and traceable, never verified. A human still "
                "approves and signs."
            )
        )
    lines = "\n".join(f"  - [{v.gate}] {v.message}" for v in violations)
    return Response(
        body=(
            f"{header}\n"
            f"payload: {source}\n"
            f"BLOCKED — {len(violations)} gate violation(s):\n{lines}\n"
            "Fix the sentence/payload and re-run; approve would refuse "
            "these identically."
        )
    )


__all__ = ["render_mint_preflight"]
