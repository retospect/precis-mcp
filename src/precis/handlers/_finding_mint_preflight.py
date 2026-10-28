"""``get(kind='finding', id='fi<id>', view='mint-preflight')`` — run the real
mint gates against a candidate payload, read-only.

`docs/backlog/claim-publication-nanopub-ots.md` §1 ("do this one"), measured
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
from precis.utils.next_block import render_next_section


def _default_payload(store: Store, ref: Ref, bundle: Any) -> tuple[dict[str, Any], str]:
    """The payload to gate when the caller supplied none, and its label.

    Preference order mirrors what a reviewer would see on the approve form:
    the frozen envelope once one exists, else an agent's parked proposal,
    else the passages derived from the hub's own chunk-grounded evidence
    (:func:`precis.nanopub.prefill.derived_payload`; ``fields`` stay empty,
    so field gates still speak for the human).
    """
    from precis.nanopub import prefill

    row = store.nanopub_publish_row(ref.id)
    if row is not None and row.grounding:
        return dict(row.grounding), "parked/frozen payload"
    parked = (ref.meta or {}).get(META_PROPOSED_PAYLOAD)
    if isinstance(parked, dict):
        return dict(parked), "parked/frozen payload"
    derived = prefill.derived_payload(store, bundle, source_fallback=False)
    n = len(derived["passages"])
    if not n:
        return derived, ""
    return derived, (
        f"derived from {n} chunk-grounded evidence edges (approve-form prefill)"
    )


def _derived_passage_lines(payload: dict[str, Any]) -> str:
    out = []
    for p in payload.get("passages") or []:
        out.append(
            f"  - pc{p.get('chunk_id')}: quote={p.get('quote', '')!r} "
            f"snip={p.get('snip', '')!r}"
        )
    return "\n".join(out)


def render_mint_preflight(store: Store, ref: Ref, *, payload: Any = None) -> Response:
    """Run `run_mint_gates` against ``payload`` and render the violations."""
    from precis.nanopub import evidence as ev
    from precis.nanopub.gates import resolve_artifact_type, run_mint_gates

    if payload is not None and not isinstance(payload, dict):
        raise BadInput(
            f"payload must be a dict, got {type(payload).__name__}",
            next=(
                "get(kind='finding', id='fi123', view='mint-preflight', "
                "args={'payload': {'hypothesis': True, 'passages': [], ...}})"
            ),
        )
    bundle = ev.load_bundle(store, ref.id)
    if payload is not None:
        resolved, label = payload, "supplied payload"
    else:
        resolved, label = _default_payload(store, ref, bundle)
    # hub_meta rides along so the meta-scoped gates (rejected-memo,
    # llm-attribution's parked-proposal signal) report here exactly as
    # they would at approve — omitting it made preflight silently softer.
    violations = run_mint_gates(store, bundle, resolved, hub_meta=ref.meta or {})

    handle = handle_registry.format_handle("finding", ref.id)
    # The gates' own resolver: the header must name the type the verdict
    # above was produced under, since the blocking set is scoped by it.
    artifact = resolve_artifact_type(bundle, resolved)
    header = f"mint-preflight {handle} ({artifact})"
    source = label or "no payload (sentence + hub state only)"
    if label.startswith("derived"):
        source += "\n" + _derived_passage_lines(resolved)
    hint = ""
    # Only a *derived* payload that found no passage is paper-level; a parked
    # / frozen one has its own (possibly empty) passages by the author's choice.
    if payload is None and label == "" and bundle.sources:
        # Routed through the shared Next renderer so the read-scope filter
        # drops this write recipe on read-only connections.
        section = render_next_section(
            [
                (
                    f"put(kind='finding', id='fi{ref.id}', supporters=[{{'paper': ..., "
                    "'source_handle': 'pc<N>'}])",
                    "the hub's evidence is paper-level (no chunk pinned), so no "
                    "passage can be derived — attach a chunk, then re-run",
                )
            ]
        )
        hint = f"\n{section}" if section else ""

    if not violations:
        return Response(
            body=(
                f"{header}\n"
                f"payload: {source}\n"
                "PASS — no blocking gate violations.\n"
                "This is admissibility, not truth: it means well-formed, "
                "sourced and traceable, never verified. A human still "
                "approves and signs."
                f"{hint}"
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
            f"{hint}"
        )
    )


__all__ = ["render_mint_preflight"]
