"""``cad_propose`` job_type — an LLM turns a natural-language instruction into a
**proposed CAD design source**, without applying it (web editor bundle).

The web "Further instructions" box mints one of these under a todo. It runs on
the agent-profile worker (which has ``claude`` auth) and its whole deliverable is
a *proposal*: a ``job_result`` chunk holding
``{source, rationale, valid, warnings, error?}``. The human reviews it in the
viewer and clicks Apply — a separate step (:meth:`CadHandler.derive`) that
branches a new design.

Unlike ``structure_propose`` (which returns incremental *ops*), a CAD design is
authored as **whole text** (:mod:`precis.cad.scene`), so the model returns a
complete rewritten source. We inline the current design as its
:func:`precis.cad.scene.spec_to_source` text, parse the reply back out, then
*dry-run* it: ``parse_source`` + ``build_design``, plus a geometry lint
(:func:`precis.cad.relate.connectivity` for a disconnected assembly, a
per-component :func:`precis.cad.bulk.volume` for an emptied/degenerate part)
so the proposal is marked valid / invalid — with non-fatal findings such as
intentional interference surfaced as ``warnings`` — before a human ever sees
it.

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

from precis.cad.bulk import volume as cad_volume
from precis.cad.graph import Design
from precis.cad.relate import ConnectivityResult, connectivity
from precis.cad.scene import (
    Resolver,
    SceneError,
    build_design,
    parse_source,
    spec_to_source,
)
from precis.cad_resolve import design_resolver
from precis.utils.llm.router import LlmRequest, Tier, route
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
    "'use <design-slug> as <name> [@x,y,z] [rot:..] [pattern]' instances "
    "ANOTHER stored design as a sub-assembly — keep such lines verbatim "
    "unless the instruction is about them; its parts arrive namespaced "
    "'<name>.<part>'. Never invent a slug that doesn't exist. "
    "'port <name> [@x,y,z] [rot:..]' declares a named frame on THIS design "
    "(an interface, not geometry). 'mate <instance>.<port> to <anchor> "
    "[flip] [spin:<deg>]' places an instance by making its port coincide "
    "with <anchor> — either this design's own '<port>' or another "
    "'<instance>.<port>'. Coincidence is the default; 'flip' adds 180 "
    "degrees about x. A mated instance must NOT also carry @/rot:. Keep "
    "port/mate lines verbatim unless the instruction is about them. "
    "Ports may carry 'type:<t>' (typed ports only mate like with like) and "
    "'of:<component>' (scopes the frame to a component). "
    "'joint <inst>.<port> to <anchor> "
    "<revolute|prismatic|cylindrical|screw|fixed> [limits:lo..hi] "
    "[pitch:<mm>]' is an articulated mate (motion about/along the anchor "
    "frame's z); 'joint <component> <kind> at:<port>' articulates a whole "
    "component about a port scoped of: that component. 'gear <a> to <b> "
    "ratio:<r>' couples two joint states. Keep joint/gear lines verbatim "
    "unless the instruction is about them. "
    "'payload <name> <add|cut> <config> at:<port> [@x,y,z] [rot:..]' is "
    "geometry the port splices into whatever it mates against (placement "
    "relative to the port frame; the far side's port must be of:-scoped). "
    "Keep payload lines verbatim unless the instruction is about them. "
    "config shapes: box:wWdDhH, cyl:rRhH, cone:rRhH, tcone:rBrThH, sphere:rR, "
    "torus:RRrr, hex:rRhH, ngon:nNrRhH, frustum:nNrBrThH, pyramid:nNrRhH, "
    "chamfer:SxA. "
    "Units mm; +Z up; box centred in x/y with base at z=0; cyl/cone axis +z, "
    "base at z=0. First node in a part is its base; later add merges, cut "
    "subtracts, intersect intersects. "
    "chamfer:SxA is an unbounded half-space bevel tool placed by the node's "
    "own @x,y,z/rot: like any other node (no anchor face); in its local "
    "frame the cutting plane is tilted A degrees off +z toward +x and set "
    "back S mm, with material on the +normal side — so it must be 'cut' or "
    "'intersect' (never 'add', which would be an infinite solid) and can "
    "never be a component's first (base) node."
)


def build_prompt(slug: str, source: str, instruction: str) -> str:
    """Assemble the propose-only directive prompt (no tools, JSON-only reply)."""
    return (
        "You are editing a parametric CAD design. You will PROPOSE a "
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


#: Grid side for the lint's per-component volume check — a coarse/cheap
#: quadrature (this runs inline in a dry-run, not a background job), fine
#: enough to tell "consumed to nothing" from "a real solid".
_LINT_VOLUME_GRID = 24
#: Below this, a component's quadrature volume reads as empty. The ray-grid
#: quadrature (:mod:`precis.cad.bulk`) is exact-per-ray, so a fully consumed
#: component integrates to exactly 0.0 — this just leaves headroom for a
#: sliver that's real but degenerate.
_EMPTY_VOLUME_MM3 = 1e-6


def _describe_disconnection(result: ConnectivityResult) -> str:
    """Name the split for a disconnected assembly's ``ConnectivityResult``."""
    groups = [", ".join(sorted(g)) for g in result.groups]
    # The common shape: one lone part floating free of an otherwise-connected
    # rest — read naturally as "X does not touch {the rest}".
    singletons = [i for i, g in enumerate(result.groups) if len(g) == 1]
    if len(groups) == 2 and singletons:
        i = singletons[0]
        lone, rest = groups[i], groups[1 - i]
        return f"disconnected: {{{lone}}} does not touch {{{rest}}}"
    bodies = " | ".join(f"{{{g}}}" for g in groups)
    return f"disconnected: assembly splits into {len(groups)} separate bodies: {bodies}"


def _empty_component_findings(design: Design) -> list[str]:
    """Per-component (near-)zero-volume check — a cut that consumed a whole
    component, or a degenerate shape. Components whose expression can't be
    bounded (e.g. an unresolved ``chamfer:`` half-space) are skipped, not
    flagged — we can't safely assume every primitive has a finite AABB."""
    findings: list[str] = []
    for name in design.components:
        try:
            vol = cad_volume(design, component=name, grid=_LINT_VOLUME_GRID)
        except Exception:  # unbounded / degenerate expr — not this check's job
            continue
        if vol.volume <= _EMPTY_VOLUME_MM3:
            findings.append(
                f"component {name!r} has (near-)zero volume — cuts consumed "
                "it or shapes are degenerate"
            )
    return findings


def _interference_warnings(result: ConnectivityResult) -> list[str]:
    """Overlapping-contact findings — not fatal (e.g. an intentional press
    fit), so they're surfaced as warnings rather than invalidating the design."""
    return [
        f"components {c.a!r} and {c.b!r} interpenetrate ({-c.gap:g} mm)"
        for c in result.contacts
        if c.interfering
    ]


def dry_run(
    source: str, *, resolve: Resolver | None = None
) -> tuple[str | None, list[str]]:
    """Parse + build the proposed source, then run cheap geometry lint on it,
    to catch errors before a human sees it.

    Returns ``(error, warnings)``. ``error`` is ``None`` iff the design
    parses, builds, has no empty/degenerate component, and — when it has ≥2
    parts — reads as one connected solid (:func:`precis.cad.relate.
    connectivity`); otherwise it names what's wrong. ``warnings`` carries
    non-fatal findings (currently: inter-part interference, which can be
    intentional — a press fit) that don't flip ``error``.
    """
    try:
        spec = parse_source(source)
    except SceneError as exc:
        return f"source error: {exc}", []
    if not spec.nodes:
        return "design has no nodes", []
    try:
        design = build_design(spec, resolve=resolve)
    except Exception as exc:  # kernel build error
        return f"build error: {exc}", []

    findings = _empty_component_findings(design)
    warnings: list[str] = []
    if len(design.components) >= 2:
        try:
            result = connectivity(design)
        except Exception:  # pragma: no cover - defensive
            log.debug("cad_propose lint: connectivity check failed", exc_info=True)
        else:
            if not result.connected:
                findings.append(_describe_disconnection(result))
            warnings.extend(_interference_warnings(result))

    return ("; ".join(findings) if findings else None), warnings


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
    # Routed through the LLM seam: tool-less agent call
    # (mcp_config=None) on FRONTIER, so PRECIS_LLM_BACKEND can switch it.
    # The broad except is kept and the folded res.error is checked too.
    try:
        res = route(
            LlmRequest(
                tier=Tier.FRONTIER,
                source="cad_propose",
                ref_id=cad_ref_id,  # attribute spend to the cad entity (gr162130)
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

    err, warnings = dry_run(proposal["source"], resolve=design_resolver(ctx.store))
    proposal["valid"] = err is None
    if err is not None:
        proposal["error"] = err
    proposal["warnings"] = warnings
    proposal["instruction"] = instruction
    proposal["cad_ref_id"] = cad_ref_id

    ctx.append_chunk("job_result", json.dumps(proposal))
    verdict = "valid" if proposal["valid"] else f"INVALID ({err})"
    warn_note = f" [{len(warnings)} warning(s)]" if warnings else ""
    ctx.append_chunk(
        "job_summary",
        f"Proposed a rewrite [{verdict}]{warn_note} for {slug}: "
        f"{proposal['rationale'][:300]}",
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
