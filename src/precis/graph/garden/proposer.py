"""The LLM proposer — one routed call per candidate.

The deterministic detectors say "this pattern is present"; the proposer
decides whether it is a real upkeep action worth a human's (or a
frontier judge's) attention and writes the rationale. Placement is
config-driven: the call carries ``source='graph_garden:propose'`` —
registered in :data:`precis.utils.llm.operations.LLM_OPERATIONS`, so an
``llm.op.graph_garden:propose`` settings row (or the tier's
``llm.chain.*`` rungs, all OpenAI-compatible endpoints) steers model
and placement live. No host or model is pinned in code.

A ``paused`` verdict (window-scoped quota/budget trip, not a defect) is
handed back to the runner — the job defers rather than failing.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from precis.graph.garden.candidates import GardenCandidate

if TYPE_CHECKING:
    from precis.store.store import Store

log = logging.getLogger(__name__)

#: Registered operation source — see ``LLM_OPERATIONS``; the
#: ``llm.op.graph_garden:propose`` row steers tier/model/chain at runtime.
OP_SOURCE = "graph_garden:propose"

_PROMPT_HEAD = """You are the graph gardener's proposal judge for a knowledge graph
(refs = typed nodes, links = typed edges, tags = closed namespaces).

You are given ONE maintenance candidate. Decide whether it is a real
upkeep action worth proposing — you are a gate, not a cheerleader:
false positives cost a human's review time, so accept only when the
evidence supports the action. You may refine the action's parameters
(e.g. pick the merge direction, the relink parent, the tag value, or —
for sibling_confusion — choose merge / sharpen / re-axis / re-parent).

Respond with a single JSON object:
{"accept": true|false,
 "rationale": "one or two sentences, naming the evidence that convinced you",
 "action": "merge|split|relink|prune|tag",   # optional: refined action
 "payload_overrides": {...}   # optional: fields merged into the proposal payload}

Rules: never invent a rationale that cites nothing in evidence; when the
signal is a hint (evidence.hint=true), accept only if the node's own
shape supports it; when unsure, accept=false.
"""


@dataclass(frozen=True, slots=True)
class Verdict:
    """The proposer's answer for one candidate."""

    accept: bool
    rationale: str = ""
    action: str | None = None  # proposer may refine (e.g. re-axis vs merge)
    payload_overrides: dict[str, Any] = field(default_factory=dict)
    model: str | None = None
    paused: bool = False  # window-scoped — the job should park, not fail
    error: str | None = None


def _prompt_for(candidate: GardenCandidate) -> str:
    return (
        _PROMPT_HEAD
        + "\nCandidate:\n"
        + json.dumps(
            {
                "detector": candidate.detector,
                "action": candidate.action,
                "target": f"{candidate.target_kind}:{candidate.target_id}",
                "other": candidate.other_id,
                "title": candidate.title,
                "evidence": candidate.evidence,
                "payload": candidate.payload,
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        + "\n"
    )


def _parse_verdict(text: str) -> dict[str, Any] | None:
    """First JSON object in the proposer's reply, or None."""
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        out = json.loads(text[start : end + 1])
    except ValueError:
        return None
    return out if isinstance(out, dict) else None


def propose_llm(store: Store, candidate: GardenCandidate) -> Verdict:
    """The default proposer — one routed LLM call. Deliberately takes
    ``store`` it never uses: the runner's ``propose=`` seam lets tests
    inject a deterministic verdict without standing up a model."""
    del store
    from precis.utils.llm.router import LlmRequest, Tier, route

    result = route(
        LlmRequest(
            tier=Tier.MEDIUM,
            prompt=_prompt_for(candidate),
            source=OP_SOURCE,
            ref_id=candidate.target_id if candidate.target_kind == "ref" else None,
            max_tokens=400,
            log_blobs=False,
        )
    )
    if getattr(result, "paused", False):
        return Verdict(accept=False, paused=True, error=result.error)
    if result.error is not None:
        return Verdict(accept=False, error=str(result.error))
    body = _parse_verdict(result.text or "")
    if body is None:
        return Verdict(
            accept=False, error="proposer returned no JSON object", model=result.model
        )
    overrides = body.get("payload_overrides")
    return Verdict(
        accept=bool(body.get("accept")),
        rationale=str(body.get("rationale") or ""),
        action=None
        if body.get("action") not in ("merge", "split", "relink", "prune", "tag")
        else str(body.get("action")),
        payload_overrides=overrides if isinstance(overrides, dict) else {},
        model=result.model,
    )


__all__ = ["OP_SOURCE", "Verdict", "propose_llm"]
