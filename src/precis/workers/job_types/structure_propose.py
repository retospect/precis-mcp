"""``structure_propose`` job_type — an LLM turns a natural-language instruction
into **proposed structure ops**, without applying them (ADR 0043 viewer bundle).

The web "Further instructions" box mints one of these under a todo. It runs on
the agent-profile worker (which has ``claude`` auth) and its whole deliverable is
a *proposal*: a ``job_result`` chunk holding ``{ops, rationale, valid}``. The
human reviews it in the viewer (each op hover-highlights its atoms) and clicks
Apply — a separate step that derives a new design.

**Propose-only by construction.** The ``claude -p`` call is given **no MCP
tools** (``mcp_config=None``), so the agent physically cannot call ``edit`` /
``put`` — it can only return text. We inline the current design into the prompt
and parse the ops JSON back out, then *dry-run* them against a scene copy so the
proposal is marked valid / invalid before a human ever sees it. The one external
boundary — the ``claude`` subprocess — is the module-level :data:`AGENT` hook,
swapped for a stub in tests so the parse + dry-run + write-back run offline.
"""

from __future__ import annotations

import copy
import json
import logging
import os
from typing import Any

from precis.structure import apply_ops
from precis.structure.ops import OpError
from precis.structure.probe import toc as _toc
from precis.utils.llm.router import LlmRequest, Tier, dispatch
from precis.workers.job_types import JobTypeSpec

log = logging.getLogger(__name__)

PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "structure_ref_id": {"type": "integer"},
        "slug": {"type": ["string", "null"]},
        "instruction": {"type": "string", "minLength": 1},
    },
    "required": ["structure_ref_id", "instruction"],
    "additionalProperties": True,
}
COMPATIBLE_EXECUTORS = frozenset({"claude_inproc"})
#: Satisfied by EXECUTOR_PROVIDES['claude_inproc'] ⊇ {'claude_bin'}. No
#: mcp_config — the proposal is tool-less on purpose.
REQUIRES = frozenset({"claude_bin"})
DESCRIPTION = (
    "Turn a natural-language instruction into proposed structure ops "
    "(tool-less claude -p; the human applies them separately)."
)

#: Op vocabulary shown to the model (kept in sync with structure/ops.py).
_OP_VOCAB = (
    "add_atom{element,frac:[x,y,z]} · set_element{atom,element} · vacancy{atom} · "
    "displace{atom,vector:[dx,dy,dz],cartesian?} · add_bond{i,j,order?,image?} · "
    "remove_bond{i,j} · constrain{atoms:[…],kind:fixed-x|y|z|all} · "
    "set_cell{a,b,c,pbc?} · cursor{name,atoms:[…],reach?,for?} · "
    "measure{kind:distance|angle|coordination|bond_length,atoms:[…],"
    "direction?,goal?,strength?,for?} · unmark{name} · remove_measure{kind,atoms:[…]}"
)


def _scene_digest(slug: str, scene: Any) -> str:
    """A compact, token-cheap rendering of the current design for the prompt."""
    t = _toc(scene)
    atoms = "\n".join(
        f"  {a.label} {a.element} frac=[{a.frac[0]:.3f},{a.frac[1]:.3f},{a.frac[2]:.3f}]"
        f"{' fixed' if a.fixed else ''}"
        for a in scene.atoms.values()
    )
    bonds = (
        "\n".join(
            f"  {b.i}-{b.j} order={b.order} {b.kind}/{b.provenance}"
            for b in scene.bonds
        )
        or "  (none)"
    )
    marks = (
        "\n".join(
            f"  {m.name or m.kind} [{m.kind}] over {','.join(m.operands)}"
            + (f" for={m.for_!r}" if m.for_ else "")
            for m in scene.measures
        )
        or "  (none)"
    )
    return (
        f"Design {slug!r}: {t['formula']} · {t['natoms']} atoms · pbc {t['pbc']} · "
        f"{t['nbonds']} bonds · {t['nfragments']} fragment(s)\n"
        f"Atoms:\n{atoms}\nBonds:\n{bonds}\nMarkers:\n{marks}"
    )


def build_prompt(slug: str, scene: Any, instruction: str) -> str:
    """Assemble the propose-only directive prompt (no tools, JSON-only reply)."""
    return (
        "You are editing an atomistic structure design (ADR 0043). You will "
        "PROPOSE a sequence of typed ops that carry out the instruction below. "
        "You are NOT applying anything — output a proposal only.\n\n"
        f"# Current design\n{_scene_digest(slug, scene)}\n\n"
        f"# Op vocabulary\n{_OP_VOCAB}\n\n"
        f"# Instruction\n{instruction.strip()}\n\n"
        "# Output contract\n"
        "Reply with ONE JSON object and nothing else:\n"
        '{"ops": [ {"op": "...", ...}, ... ], '
        '"rationale": "one or two sentences on what these ops do and why"}\n'
        "Reference existing atoms by their labels (e.g. aPd12). New atoms you add "
        "get auto-assigned labels. Do not include a 'relax' op. Do not wrap the "
        "JSON in prose or markdown fences."
    )


def parse_proposal(text: str) -> dict[str, Any]:
    """Extract ``{ops, rationale}`` from the model's reply.

    Tolerates a stray ```json fence or leading prose by scanning for the first
    balanced ``{ … }``. Raises ``ValueError`` if no ops list is found.
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
    ops = obj.get("ops")
    if not isinstance(ops, list) or not ops:
        raise ValueError("proposal has no 'ops' list")
    return {"ops": ops, "rationale": str(obj.get("rationale") or "").strip()}


def dry_run(scene: Any, ops: list[dict[str, Any]]) -> str | None:
    """Apply ``ops`` to a **copy** of the scene to catch errors before a human
    sees the proposal. Returns an error string, or ``None`` when the ops apply
    cleanly. A proposed ``relax`` is rejected (a proposal is a graph/marker edit)."""
    if any(o.get("op") == "relax" for o in ops):
        return "a proposal may not include a 'relax' op"
    try:
        apply_ops(copy.deepcopy(scene), ops)
    except OpError as exc:
        return f"op error: {exc}"
    return None


def _dispatch(ctx: Any, spec: Any) -> None:
    """Plugin dispatcher (claude_inproc): build the prompt, run tool-less claude,
    parse + dry-run the proposal, and write it as a ``job_result`` chunk."""
    params = (ctx.meta or {}).get("params") or {}
    try:
        structure_ref_id = int(params["structure_ref_id"])
        instruction = str(params["instruction"]).strip()
    except (KeyError, TypeError, ValueError) as exc:
        ctx.record_failure(f"structure_propose: malformed params ({exc})")
        return
    if not instruction:
        ctx.record_failure("structure_propose: empty instruction")
        return

    try:
        scene, _handles = ctx.store.structure_load(structure_ref_id)
    except Exception as exc:  # design vanished / bad id
        ctx.record_failure(f"structure_propose: cannot load design: {exc}")
        return
    slug = str(params.get("slug") or structure_ref_id)

    prompt = build_prompt(slug, scene, instruction)
    model = os.environ.get("PRECIS_STRUCTURE_PROPOSE_MODEL")
    ctx.append_chunk("job_event", f"propose: {instruction[:200]}")
    # Routed through the LLM seam (ADR 0046 unit 4b): tool-less agent call on
    # CLOUD_SUPER, so PRECIS_LLM_BACKEND can switch it. Broad except kept +
    # the folded res.error checked.
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
                extra_args=("--verbose",),
                log_event=(ctx.store, ctx.ref_id, "structure_propose"),
            )
        )
    except Exception as exc:
        ctx.record_failure(f"structure_propose: agent failed: {exc}")
        return
    if res.error:
        ctx.record_failure(f"structure_propose: agent failed: {res.error}")
        return

    try:
        proposal = parse_proposal(res.text)
    except ValueError as exc:
        ctx.append_chunk("job_event", f"unparseable reply:\n{res.text[:2000]}")
        ctx.record_failure(f"structure_propose: {exc}")
        return

    err = dry_run(scene, proposal["ops"])
    proposal["valid"] = err is None
    if err is not None:
        proposal["error"] = err
    proposal["instruction"] = instruction
    proposal["structure_ref_id"] = structure_ref_id

    ctx.append_chunk("job_result", json.dumps(proposal))
    n = len(proposal["ops"])
    verdict = "valid" if proposal["valid"] else f"INVALID ({err})"
    ctx.append_chunk(
        "job_summary",
        f"Proposed {n} op(s) [{verdict}] for {slug}: {proposal['rationale'][:300]}",
    )
    ctx.set_meta(proposed_ops=n, proposal_valid=proposal["valid"])
    ctx.set_status("succeeded")


def _run(*_a: Any, **_k: Any) -> Any:
    raise NotImplementedError("structure_propose runs via dispatch(), not run()")


SPEC = JobTypeSpec(
    name="structure_propose",
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
