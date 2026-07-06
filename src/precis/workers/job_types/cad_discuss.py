"""``cad_discuss`` job_type — a *conversation* about a CAD design, not a rewrite.

The web editor's "Discuss" box mints one of these. Unlike ``cad_propose`` (which
returns a whole rewritten source to Apply), a discuss turn just *answers* — "what
is this?", "why isn't it functional?", "how would I connect the rim to the hub?"
— and returns prose. No source, no dry-run, nothing to Apply.

Two things make it useful:

* **It is fed the model's measured facts.** The prompt carries the current design
  source *plus* a precomputed facts block — the coordinate convention, the
  connectivity verdict (which parts touch, whether it is one solid, which are
  floating), interference, bbox and volume, and **per-feature world bounds** (the
  real x/y/z extent of every node). So "why isn't it functional?" is answered
  from real geometry — and the model doesn't have to *guess* where a part's zero
  is (a cyl at ``loc.z=-8 h16`` spans z −8..+8, not −16..0). (Live MCP probing is
  a later enhancement; today the facts are precomputed and inlined.)
* **It is threaded.** Each turn's prompt includes the prior turns for the same
  design (their questions + answers), so it is a real back-and-forth. The thread
  *is* the sequence of ``cad_discuss`` jobs for the design.

**Read-only by construction.** The ``claude -p`` call is given no MCP tools
(``mcp_config=None``); a discussion can only produce text. The module-level
:data:`AGENT` hook is swapped for a stub in tests.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from precis.cad.scene import build_design, spec_to_source
from precis.utils.claude_agent import call_claude_agent
from precis.workers.job_types import JobTypeSpec

log = logging.getLogger(__name__)

PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "cad_ref_id": {"type": "integer"},
        "slug": {"type": ["string", "null"]},
        "instruction": {"type": "string", "minLength": 1},
    },
    "required": ["cad_ref_id", "instruction"],
    "additionalProperties": True,
}
COMPATIBLE_EXECUTORS = frozenset({"claude_inproc"})
#: Satisfied by EXECUTOR_PROVIDES['claude_inproc'] ⊇ {'claude_bin'}. No
#: mcp_config — a discussion is text-only by design.
REQUIRES = frozenset({"claude_bin"})
DESCRIPTION = (
    "Discuss a CAD design with the engineer (tool-less claude -p, threaded, "
    "read-only) — answers questions about the model, proposes nothing to apply."
)

#: The claude boundary — tests monkeypatch this to run offline.
AGENT = call_claude_agent

#: One-line coordinate convention, inlined into the facts so the model reads
#: where each part actually sits instead of guessing its local zero (the
#: cyl-base-at-0 vs centred-on-loc trap that produced a wrong z-extent).
_CONVENTION = (
    "Coordinates: +Z up, mm. cyl/cone/frustum have their BASE at z=0 and "
    "extend +z; box/ngon/hex are centred in x/y with base at z=0; `loc` "
    "translates the primitive and `rot` is degrees. So a part's z-extent is "
    "loc.z .. loc.z+h — it is NOT centred on loc.z. Use the per-feature world "
    "bounds below rather than inferring positions from the source."
)


def _feature_bounds(design: Any) -> dict[str, tuple[Any, Any]]:
    """World-space AABB per node name (patterns like ``spoke#3`` folded back to
    ``spoke``), so the discussion can cite real extents, not guessed ones."""
    import numpy as np

    acc: dict[str, tuple[Any, Any]] = {}
    for inst in design.instances.values():
        base = str(inst.label).split("#", 1)[0]
        lo, hi = inst.placed.aabb()
        if not np.all(np.isfinite(lo)):
            continue
        if base in acc:
            plo, phi = acc[base]
            acc[base] = (np.minimum(plo, lo), np.maximum(phi, hi))
        else:
            acc[base] = (lo, hi)
    return acc


def _design_facts(store: Any, cad_ref_id: int) -> tuple[str, str]:
    """Return ``(source, facts)`` — the design source plus a measured-facts
    block (connectivity / interference / bbox / volume / node tree) so the
    discussion is grounded in real geometry, not a guess. ``facts`` is a
    best-effort string; a build failure degrades it to a short note."""
    scene_spec, _handles = store.cad_load(cad_ref_id)
    source = spec_to_source(scene_spec)
    lines: list[str] = []
    try:
        from precis.cad.bulk import _expr_aabb
        from precis.cad.bulk import volume as cad_volume
        from precis.cad.relate import connectivity as cad_connectivity

        design = build_design(scene_spec)
        lines.append(_CONVENTION)
        lo, hi = _expr_aabb(design, design.whole())
        lines.append(
            f"Bounding box (mm): {hi[0] - lo[0]:.3g} × {hi[1] - lo[1]:.3g} × "
            f"{hi[2] - lo[2]:.3g}"
        )
        try:
            vol = cad_volume(design)
            lines.append(f"Volume (mm³): {vol.volume:.4g} (±{vol.rel_err * 100:.1f}%)")
        except Exception:  # pragma: no cover - volume is best-effort
            pass
        if len(dict.fromkeys(scene_spec.components)) >= 2:
            conn = cad_connectivity(design)
            if conn.connected:
                lines.append("Connectivity: ONE connected solid (all parts touch).")
            else:
                bodies = " | ".join("+".join(g) for g in conn.groups)
                lines.append(
                    f"Connectivity: {len(conn.groups)} SEPARATE bodies: {bodies}"
                )
                iso = conn.isolated()
                if iso:
                    lines.append(f"Floating (touch nothing): {', '.join(iso)}")
            contacts = [
                f"{c.a}↔{c.b} ({'interfere' if c.interfering else 'touch'}, {c.gap:g} mm)"
                for c in conn.contacts
            ]
            lines.append("Contacts: " + (", ".join(contacts) if contacts else "none"))
        else:
            lines.append("Connectivity: single component.")
        # Real per-feature world bounds — the fix for the model guessing where a
        # part's zero is (e.g. a cyl at loc.z=-8 h16 spans z −8..+8, not −16..0).
        bounds = _feature_bounds(design)
        if bounds:
            lines.append("Per-feature world bounds (mm):")
            for node in scene_spec.nodes:
                b = bounds.get(node.name)
                if b is None:
                    continue
                blo, bhi = b
                lines.append(
                    f"  {node.name} [{node.component}] {node.op}: "
                    f"x[{blo[0]:.3g}..{bhi[0]:.3g}] "
                    f"y[{blo[1]:.3g}..{bhi[1]:.3g}] "
                    f"z[{blo[2]:.3g}..{bhi[2]:.3g}]"
                )
    except Exception as exc:  # pragma: no cover - a bad build shouldn't blank facts
        lines.append(f"(geometry facts unavailable: {exc})")
    return source, "\n".join(lines)


def _prior_turns(
    store: Any, cad_ref_id: int, exclude_job_id: int
) -> list[dict[str, str]]:
    """The design's earlier discussion turns (oldest first): each a
    ``{instruction, answer}`` from a succeeded ``cad_discuss`` job."""
    sql = """
        SELECT r.ref_id,
               (SELECT c.text FROM chunks c
                 WHERE c.ref_id = r.ref_id AND c.chunk_kind = 'job_result'
                 ORDER BY c.ord DESC LIMIT 1)                                  AS result,
               r.meta->'params'->>'instruction'                               AS instruction
          FROM refs r
         WHERE r.kind = 'job'
           AND r.meta->>'job_type' = 'cad_discuss'
           AND (r.meta->'params'->>'cad_ref_id')::int = %s
           AND r.ref_id <> %s
           AND r.deleted_at IS NULL
         ORDER BY r.ref_id ASC
    """
    with store.pool.connection() as conn:
        rows = conn.execute(sql, (cad_ref_id, exclude_job_id)).fetchall()
    turns: list[dict[str, str]] = []
    for _rid, result_text, instruction in rows:
        answer = ""
        if result_text:
            try:
                answer = str(json.loads(result_text).get("answer") or "")
            except (json.JSONDecodeError, TypeError):
                answer = ""
        if instruction and answer:
            turns.append({"instruction": instruction, "answer": answer})
    return turns


def build_prompt(
    slug: str,
    source: str,
    facts: str,
    instruction: str,
    prior: list[dict[str, str]],
) -> str:
    """Assemble the discussion prompt: model facts + thread + the question."""
    parts = [
        "You are a CAD design assistant discussing a parametric solid model "
        "(ADR 0041) with the engineer who is building it. Answer their question. "
        "This is a DISCUSSION — do NOT output a full rewritten design source or a "
        "diff unless they explicitly ask for one; explain in prose, referencing "
        "part and node names and the measured facts below. If they ask how to fix "
        "or change something, describe the approach and name the specific parts / "
        "nodes to touch.\n",
        f"# Current design {slug!r}\n{source}\n",
        f"# Measured facts\n{facts}\n",
    ]
    if prior:
        thread = "\n\n".join(f"Q: {t['instruction']}\nA: {t['answer']}" for t in prior)
        parts.append(f"# Conversation so far\n{thread}\n")
    parts.append(f"# The engineer asks\n{instruction.strip()}\n")
    parts.append(
        "# Output\nReply with a helpful, concrete answer in plain prose "
        "(markdown ok). No JSON, no code fences around the whole reply."
    )
    return "\n".join(parts)


def _dispatch(ctx: Any, spec: Any) -> None:
    """Plugin dispatcher (claude_inproc): gather facts + thread, run tool-less
    claude, and write the prose answer as a ``job_result`` chunk."""
    params = (ctx.meta or {}).get("params") or {}
    try:
        cad_ref_id = int(params["cad_ref_id"])
        instruction = str(params["instruction"]).strip()
    except (KeyError, TypeError, ValueError) as exc:
        ctx.record_failure(f"cad_discuss: malformed params ({exc})")
        return
    if not instruction:
        ctx.record_failure("cad_discuss: empty instruction")
        return

    try:
        source, facts = _design_facts(ctx.store, cad_ref_id)
    except Exception as exc:  # design vanished / bad id
        ctx.record_failure(f"cad_discuss: cannot load design: {exc}")
        return
    slug = str(params.get("slug") or cad_ref_id)
    prior = _prior_turns(ctx.store, cad_ref_id, ctx.ref_id)

    prompt = build_prompt(slug, source, facts, instruction, prior)
    model = os.environ.get("PRECIS_CAD_DISCUSS_MODEL")
    timeout_s = float(os.environ.get("PRECIS_CAD_DISCUSS_TIMEOUT_S", "1800"))
    ctx.append_chunk("job_event", f"discuss: {instruction[:200]}")
    try:
        result = AGENT(
            prompt,
            model=model,
            mcp_config=None,  # read-only: a discussion produces text only
            disallowed_tools=("WebFetch", "WebSearch"),
            output_format="stream-json",
            timeout_s=timeout_s,
            extra_args=("--verbose",),
            log_event=(ctx.store, ctx.ref_id, "cad_discuss"),
        )
    except Exception as exc:
        ctx.record_failure(f"cad_discuss: agent failed: {exc}")
        return

    answer = (result.final_text or "").strip()
    if not answer:
        ctx.record_failure("cad_discuss: empty answer")
        return

    payload = {"answer": answer, "instruction": instruction, "cad_ref_id": cad_ref_id}
    ctx.append_chunk("job_result", json.dumps(payload))
    ctx.append_chunk("job_summary", f"Discussed {slug}: {instruction[:200]}")
    ctx.set_status("succeeded")


def _run(*_a: Any, **_k: Any) -> Any:
    raise NotImplementedError("cad_discuss runs via dispatch(), not run()")


SPEC = JobTypeSpec(
    name="cad_discuss",
    params_schema=PARAMS_SCHEMA,
    compatible_executors=COMPATIBLE_EXECUTORS,
    requires=REQUIRES,
    description=DESCRIPTION,
    run=_run,
    dispatch=_dispatch,
)


def load() -> JobTypeSpec:
    return SPEC


__all__ = ["SPEC", "build_prompt", "load"]
