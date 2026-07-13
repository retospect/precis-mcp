"""``cad_propose`` job_type — an LLM turns a natural-language instruction into a
**proposed CAD design source**, without applying it (ADR 0041 web editor bundle).

The web "Further instructions" box mints one of these under a todo. It runs on
the agent-profile worker (which has ``claude`` auth) and its whole deliverable is
a *proposal*: a ``job_result`` chunk holding ``{source, rationale, valid}``. The
human reviews it in the viewer and clicks Apply — a separate step
(:meth:`CadHandler.derive`) that branches a new design.

Unlike ``structure_propose`` (which returns incremental *ops*), a CAD design is
authored as **whole text** (:mod:`precis.cad.scene`), so the model returns a
complete rewritten source. We inline the current design as its
:func:`precis.cad.scene.spec_to_source` text, parse the reply back out, then
*dry-run* it (``parse_source`` + ``build_design``) so the proposal is marked
valid / invalid before a human ever sees it.

**Propose-only by construction.** The ``claude -p`` call is given **no MCP tools**
(``mcp_config=None``), so the agent physically cannot mutate anything — it can
only return text. The one external boundary is the module-level :data:`AGENT`
hook, swapped for a stub in tests.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from precis.cad.scene import SceneError, build_design, parse_source, spec_to_source
from precis.utils.llm.router import LlmRequest, Tier, dispatch
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
#: mcp_config — the proposal is tool-less on purpose.
REQUIRES = frozenset({"claude_bin"})
DESCRIPTION = (
    "Turn a natural-language instruction into a proposed CAD design source "
    "(tool-less claude -p; the human applies it separately)."
)

#: The design-language crib shown to the model (kept in sync with cad/scene.py +
#: cad/dsl.py). Enough to author a valid rewrite without reading the skill.
_DSL_CRIB = (
    "One node per line: '<name> <op> <config> [@x,y,z] [rot:rx,ry,rz] "
    "[polar:nNrR | linear:nNdx..dy..dz..]'. op ∈ add|cut|intersect. "
    "'component <name>' opens a part. 'desc:'/'use:' lines record intent. "
    "config shapes: box:wWdDhH, cyl:rRhH, cone:rRhH, tcone:rBrThH, sphere:rR, "
    "torus:RRrr, hex:rRhH, ngon:nNrRhH, frustum:nNrBrThH, pyramid:nNrRhH. "
    "Units mm; +Z up; box centred in x/y with base at z=0; cyl/cone axis +z, "
    "base at z=0. First node in a part is its base; later add merges, cut "
    "subtracts, intersect intersects."
)


def build_prompt(slug: str, source: str, instruction: str) -> str:
    """Assemble the propose-only directive prompt (no tools, JSON-only reply)."""
    return (
        "You are editing a parametric CAD design (ADR 0041). You will PROPOSE a "
        "complete rewritten design source that carries out the instruction below. "
        "You are NOT applying anything — output a proposal only.\n\n"
        f"# Current design {slug!r}\n{source}\n\n"
        f"# Design language\n{_DSL_CRIB}\n\n"
        f"# Instruction\n{instruction.strip()}\n\n"
        "# Output contract\n"
        "Reply with ONE JSON object and nothing else:\n"
        '{"source": "<the full new design source, newline-separated lines>", '
        '"rationale": "one or two sentences on what changed and why"}\n'
        "The source must be the WHOLE design (not a diff) — keep the parts you "
        "aren't changing. Do not wrap the JSON in prose or markdown fences."
    )


def parse_proposal(text: str) -> dict[str, Any]:
    """Extract ``{source, rationale}`` from the model's reply.

    Tolerates a stray ```json fence or leading prose by scanning for the first
    balanced ``{ … }``. Raises ``ValueError`` if no source string is found.
    """
    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("no JSON object in the model reply")
    obj = json.loads(raw[start : end + 1])
    source = obj.get("source")
    if not isinstance(source, str) or not source.strip():
        raise ValueError("proposal has no 'source' text")
    return {"source": source, "rationale": str(obj.get("rationale") or "").strip()}


def dry_run(source: str) -> str | None:
    """Parse + build the proposed source to catch errors before a human sees it.
    Returns an error string, or ``None`` when it is a valid buildable design."""
    try:
        spec = parse_source(source)
    except SceneError as exc:
        return f"source error: {exc}"
    if not spec.nodes:
        return "design has no nodes"
    try:
        build_design(spec)
    except Exception as exc:  # kernel build error
        return f"build error: {exc}"
    return None


def _dispatch(ctx: Any, spec: Any) -> None:
    """Plugin dispatcher (claude_inproc): build the prompt, run tool-less claude,
    parse + dry-run the proposal, and write it as a ``job_result`` chunk."""
    params = (ctx.meta or {}).get("params") or {}
    try:
        cad_ref_id = int(params["cad_ref_id"])
        instruction = str(params["instruction"]).strip()
    except (KeyError, TypeError, ValueError) as exc:
        ctx.record_failure(f"cad_propose: malformed params ({exc})")
        return
    if not instruction:
        ctx.record_failure("cad_propose: empty instruction")
        return

    try:
        scene_spec, _handles = ctx.store.cad_load(cad_ref_id)
    except Exception as exc:  # design vanished / bad id
        ctx.record_failure(f"cad_propose: cannot load design: {exc}")
        return
    slug = str(params.get("slug") or cad_ref_id)
    source = spec_to_source(scene_spec)

    prompt = build_prompt(slug, source, instruction)
    model = os.environ.get("PRECIS_CAD_PROPOSE_MODEL")
    # A whole-design rewrite on opus overruns the shared 600s agent default,
    # so give cad_propose the same 30-min wall-clock the other agent jobs get
    # (plan_tick / fix_gripe = 1800s). Override with PRECIS_CAD_PROPOSE_TIMEOUT_S.
    timeout_s = float(os.environ.get("PRECIS_CAD_PROPOSE_TIMEOUT_S", "1800"))
    ctx.append_chunk("job_event", f"propose: {instruction[:200]}")
    # Routed through the LLM seam (ADR 0046 unit 4b): tool-less agent call
    # (mcp_config=None) on CLOUD_SUPER, so PRECIS_LLM_BACKEND can switch it.
    # The broad except is kept and the folded res.error is checked too.
    try:
        res = dispatch(
            LlmRequest(
                tier=Tier.CLOUD_SUPER,
                prompt=prompt,
                tools_needed=True,  # the agent wrapper; no MCP tools wired
                model=model,
                mcp_config=None,  # tool-less: the agent cannot mutate anything
                disallowed_tools=("WebFetch", "WebSearch"),
                output_format="stream-json",
                timeout_s=timeout_s,
                extra_args=("--verbose",),
                log_event=(ctx.store, ctx.ref_id, "cad_propose"),
            )
        )
    except Exception as exc:
        ctx.record_failure(f"cad_propose: agent failed: {exc}")
        return
    if res.error:
        ctx.record_failure(f"cad_propose: agent failed: {res.error}")
        return

    try:
        proposal = parse_proposal(res.text)
    except ValueError as exc:
        ctx.append_chunk("job_event", f"unparseable reply:\n{res.text[:2000]}")
        ctx.record_failure(f"cad_propose: {exc}")
        return

    err = dry_run(proposal["source"])
    proposal["valid"] = err is None
    if err is not None:
        proposal["error"] = err
    proposal["instruction"] = instruction
    proposal["cad_ref_id"] = cad_ref_id

    ctx.append_chunk("job_result", json.dumps(proposal))
    verdict = "valid" if proposal["valid"] else f"INVALID ({err})"
    ctx.append_chunk(
        "job_summary",
        f"Proposed a rewrite [{verdict}] for {slug}: {proposal['rationale'][:300]}",
    )
    ctx.set_meta(proposal_valid=proposal["valid"])
    ctx.set_status("succeeded")


def _run(*_a: Any, **_k: Any) -> Any:
    raise NotImplementedError("cad_propose runs via dispatch(), not run()")


SPEC = JobTypeSpec(
    name="cad_propose",
    params_schema=PARAMS_SCHEMA,
    compatible_executors=COMPATIBLE_EXECUTORS,
    requires=REQUIRES,
    description=DESCRIPTION,
    run=_run,
    dispatch=_dispatch,
)


def load() -> JobTypeSpec:
    return SPEC


__all__ = ["SPEC", "build_prompt", "dry_run", "load", "parse_proposal"]
