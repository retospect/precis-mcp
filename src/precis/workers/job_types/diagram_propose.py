"""``diagram_propose`` job_type — one autonomous diagram draw-with-me turn
(ADR 0057, slice 5).

Given a target diagram (a ``figure`` or ``mermaid`` ref), an instruction, and an
optional set of **seed chunk handles**, this runs one turn of the shared
:func:`precis.diagram.turn.run_turn` loop against the real model. Unlike
``cad_propose`` / ``structure_propose`` (propose-only — they write a proposal a
human applies later), the diagram turn loop **is** the apply mechanism: it
edits the diagram source in place, reconciles the node→chunk bindings, and
appends a turn chunk. So a ``diagram_propose`` job **builds or verifies the
diagram directly**, owned by the diagram artifact (compute lane, ADR 0044 —
figure/mermaid opt in via ``KindSpec.can_own_jobs``).

The two driving scenarios (design doc §"How a tick builds it"):

- **Build from scratch** — seeds are the reading material ("here's another
  view, a CAD cross-section, 5 chunks"); the model drafts the diagram and emits
  the bindings in one turn.
- **Verify as it stands** — no/updated seeds; the model checks the diagram
  against the linked sources (already in the turn's prepared context) and fixes
  drift.

The model call is the turn loop's own (the figure/mermaid shim's ``_default_claude``
→ the ADR 0046 LLM router), so ``PRECIS_LLM_BACKEND`` switches it; a model
failure degrades to a chat-only turn (the loop's ``_safe_call``), never a crash.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from precis.workers.job_types import JobTypeSpec

log = logging.getLogger(__name__)

#: Cap on an inlined seed body — keep the prompt bounded.
_SEED_CHARS = 1500

PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["figure", "mermaid"]},
        "ref_id": {"type": "integer"},
        "instruction": {"type": "string", "minLength": 1},
        "seeds": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["kind", "ref_id", "instruction"],
    "additionalProperties": True,
}
COMPATIBLE_EXECUTORS = frozenset({"claude_inproc"})
REQUIRES = frozenset({"claude_bin"})
DESCRIPTION = (
    "Run one figure/mermaid draw-with-me turn against the model — build or "
    "verify the diagram from seed chunks and reconcile node→chunk bindings."
)


def compose_message(store: Any, instruction: str, seeds: list[str]) -> str:
    """The turn message: the instruction, then the seed chunks inlined as
    reading material. Chunk handles (``dc…``/``pc…``) inline their text; other
    handles are listed as titled references. Empty ``seeds`` ⇒ just the
    instruction."""
    instruction = instruction.strip()
    if not seeds:
        return instruction
    parts = [
        instruction,
        "",
        "## Reading material (build the diagram faithfully from these)",
    ]
    for h in seeds:
        h = h.strip()
        if not h:
            continue
        chunk = None
        try:
            chunk = store.universal_chunk(h)
        except Exception:
            chunk = None
        if chunk and (chunk.get("text") or "").strip():
            parts.append(f"### {h}\n{str(chunk['text']).strip()[:_SEED_CHARS]}")
            continue
        ref = _resolve_ref(store, h)
        if ref is not None:
            parts.append(f"### {h} — {ref}")
        else:
            parts.append(f"### {h} (could not resolve)")
    return "\n".join(parts)


def _resolve_ref(store: Any, handle: str) -> str | None:
    try:
        rh = store.resolve_handle(handle)
    except Exception:
        return None
    if rh is None:
        return None
    return f"{rh.kind}:{rh.public_id}"


def _run_turn(kind: str, store: Any, ref: Any, message: str) -> Any:
    """Dispatch to the right turn shim (each supplies the real model call)."""
    if kind == "figure":
        from precis.figure.turn import run_turn
    else:
        from precis.mermaid.turn import run_turn
    return run_turn(store, ref, message)


def _dispatch(ctx: Any, spec: Any) -> None:
    """Plugin dispatcher (claude_inproc): resolve the diagram, compose the
    seed-augmented message, run one turn (which mutates the diagram + bindings),
    and record the outcome."""
    params = (ctx.meta or {}).get("params") or {}
    try:
        kind = str(params["kind"]).strip()
        ref_id = int(params["ref_id"])
        instruction = str(params["instruction"]).strip()
    except (KeyError, TypeError, ValueError) as exc:
        ctx.record_failure(f"diagram_propose: malformed params ({exc})")
        return
    if kind not in ("figure", "mermaid"):
        ctx.record_failure(f"diagram_propose: unsupported kind {kind!r}")
        return
    if not instruction:
        ctx.record_failure("diagram_propose: empty instruction")
        return
    seeds = [str(s) for s in (params.get("seeds") or [])]

    ref = ctx.store.get_ref(kind=kind, id=ref_id)
    if ref is None:
        ctx.record_failure(f"diagram_propose: {kind} id={ref_id} not found")
        return

    message = compose_message(ctx.store, instruction, seeds)
    ctx.append_chunk(
        "job_event",
        f"diagram_propose[{kind}] {getattr(ref, 'slug', ref_id)}: "
        f"{instruction[:200]}" + (f" (+{len(seeds)} seed(s))" if seeds else ""),
    )

    try:
        result = _run_turn(kind, ctx.store, ref, message)
    except Exception as exc:  # the turn loop degrades internally; this is belt
        ctx.record_failure(f"diagram_propose: turn failed: {exc}")
        return

    findings = [
        {"kind": f.kind, "node": f.node, "message": f.message} for f in result.findings
    ]
    out = {
        "kind": kind,
        "ref_id": ref_id,
        "instruction": instruction,
        "reply": result.reply,
        "changed": result.changed,
        "healed": result.healed,
        "findings": findings,
        "bindings": [
            {"element": b["element"], "handle": b["handle"]} for b in result.bindings
        ],
    }
    ctx.append_chunk("job_result", json.dumps(out))
    verb = "edited" if result.changed else "left unchanged"
    ctx.append_chunk(
        "job_summary",
        f"{verb} {kind} {getattr(ref, 'slug', ref_id)}: {result.reply[:200]} "
        f"({len(out['bindings'])} binding(s), {len(findings)} lint(s))",
    )
    ctx.set_meta(changed=result.changed, findings=len(findings))
    ctx.set_status("succeeded")


def _run(*_a: Any, **_k: Any) -> Any:
    raise NotImplementedError("diagram_propose runs via dispatch(), not run()")


SPEC = JobTypeSpec(
    name="diagram_propose",
    params_schema=PARAMS_SCHEMA,
    compatible_executors=COMPATIBLE_EXECUTORS,
    requires=REQUIRES,
    description=DESCRIPTION,
    run=_run,
    dispatch=_dispatch,
)


def load() -> JobTypeSpec:
    return SPEC


__all__ = ["SPEC", "compose_message", "load"]
