"""Plan node shapes on todo ``meta`` (dark-factory, td542092).

A plan is a todo subtree; what a node *is* lives in five optional meta keys
(``docs/backlog/dark-factory.md`` § "Node kinds, on existing fields"):

==============  ==========================================================
``op``          str job_type name, or ``{type, params?}`` -- a task
``auto_close``  bool -- a phase: the tick closes it when every child is done
``gate``        ``{kind, args}``: ``sha-in-prod {sha}``,
                ``tests-green {range}``, ``answer``, ``not-before {ts}``,
                ``drc-clean {board}``
``loop``        ``{max, until: <gate spec>, body: <template name>}``
``generate``    ``{role: slicer|spec-writer|triage|reviewer|analyst,
                from: <str>}``
==============  ==========================================================

A node carries at most one of ``gate`` / ``loop`` / ``generate``. A
``reviewer`` generator mints nothing itself: its session posts one
``review:`` verdict comment and the tick acts on it (``from`` is the
``td<id>`` of the node whose land it reviews). An ``analyst`` generator
also mints nothing: its session posts one ``analysis:`` comment (a
read-only look at an inbound pull request, ``from`` is ``pr:<n>``) and
the tick closes it once that comment is there.

This module only judges shape: :func:`validate_plan_meta` returns
human-readable problems (empty list = fine) and :func:`node_kind` names the
kind a meta dict describes. Evaluating gates, advancing loops and minting
children belong to the tick and the templates, not here.
"""

from __future__ import annotations

import re
from typing import Any, Literal, NotRequired, TypedDict

NodeKind = Literal["task", "phase", "gate", "loop", "generator"]

#: The meta keys this module owns -- the todo handler's edit allowlist
#: widens by exactly these.
PLAN_KEYS: tuple[str, ...] = ("op", "gate", "loop", "generate", "auto_close")

#: A node carries at most one of these (each makes it a different kind).
EXCLUSIVE_KEYS: tuple[str, ...] = ("gate", "loop", "generate")

GATE_KINDS: tuple[str, ...] = (
    "sha-in-prod",
    "tests-green",
    "answer",
    "not-before",
    "drc-clean",
)
GENERATE_ROLES: tuple[str, ...] = (
    "slicer",
    "spec-writer",
    "triage",
    "reviewer",
    "analyst",
)

#: job_type names that are accepted for ``op`` before they reach the
#: registry. ``run_slice`` lands with td542094 (the step 6 lane adapter);
#: drop it from here once ``get_job_type('run_slice')`` resolves.
_PENDING_JOB_TYPES: frozenset[str] = frozenset({"run_slice"})

#: Per gate kind: the args keys, all required. ``answer`` takes none.
_GATE_ARG_KEYS: dict[str, tuple[str, ...]] = {
    "sha-in-prod": ("sha",),
    "tests-green": ("range",),
    "answer": (),
    "not-before": ("ts",),
    "drc-clean": ("board",),
}

_SHA_RE = re.compile(r"[0-9a-f]{7,40}")
_TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")

#: The accepted shape per key, quoted in ``next=`` hints.
PLAN_SHAPES: dict[str, str] = {
    "op": "'<job_type name>' or {'type': '<job_type name>', 'params': {...}}",
    "auto_close": "true or false",
    "gate": (
        "{'kind': 'sha-in-prod', 'args': {'sha': '<hex sha>'}} | "
        "{'kind': 'tests-green', 'args': {'range': '<git range>'}} | "
        "{'kind': 'answer'} | "
        "{'kind': 'not-before', 'args': {'ts': 'YYYY-MM-DDTHH:MM:SSZ'}} | "
        "{'kind': 'drc-clean', 'args': {'board': 'pb<id>'}}"
    ),
    "loop": ("{'max': <int > 0>, 'until': <gate spec>, 'body': '<template name>'}"),
    "generate": (
        "{'role': 'slicer'|'spec-writer'|'triage'|'reviewer'|'analyst', "
        "'from': '<md section | gripe | board | td<id> reviewed>'}"
    ),
}


class GateSpec(TypedDict):
    kind: Literal["sha-in-prod", "tests-green", "answer", "not-before", "drc-clean"]
    args: NotRequired[dict[str, str]]


class OpSpec(TypedDict):
    type: str
    params: NotRequired[dict[str, Any]]


class LoopSpec(TypedDict):
    max: int
    until: GateSpec
    body: str


#: Functional form: ``from`` is a keyword, so a class body cannot name it.
GenerateSpec = TypedDict(
    "GenerateSpec",
    {
        "role": Literal["slicer", "spec-writer", "triage", "reviewer", "analyst"],
        "from": str,
    },
)


def _job_type_known(name: str) -> bool:
    if name in _PENDING_JOB_TYPES:
        return True
    from precis.workers.job_types import get_job_type

    return get_job_type(name) is not None


def _unknown_keys(spec: dict[str, Any], allowed: tuple[str, ...]) -> str | None:
    extra = sorted(set(spec) - set(allowed))
    if extra:
        return f"unknown key(s) {extra}; allowed keys are {list(allowed)}"
    return None


def _op_problems(op: Any) -> list[str]:
    name: Any
    params: Any
    if isinstance(op, str):
        name, params = op, None
    elif isinstance(op, dict):
        bad = _unknown_keys(op, ("type", "params"))
        if bad:
            return [f"meta.op: {bad}"]
        raw_name, params = op.get("type"), op.get("params")
        if not isinstance(raw_name, str):
            return ["meta.op.type must be a job_type name (string)"]
        name = raw_name
    else:
        return [f"meta.op must be a string or a dict, got {type(op).__name__}"]
    out: list[str] = []
    if not name.strip() or not _job_type_known(name):
        from precis.workers.job_types import known_job_types

        known = sorted({*known_job_types(), *_PENDING_JOB_TYPES})
        out.append(f"meta.op: job_type {name!r} is not registered; known: {known}")
    if params is not None and not isinstance(params, dict):
        out.append(f"meta.op.params must be a dict, got {type(params).__name__}")
    return out


def _gate_problems(gate: Any, where: str) -> list[str]:
    if not isinstance(gate, dict):
        return [f"{where} must be a dict, got {type(gate).__name__}"]
    bad = _unknown_keys(gate, ("kind", "args"))
    if bad:
        return [f"{where}: {bad}"]
    kind = gate.get("kind")
    if kind not in GATE_KINDS:
        return [f"{where}.kind={kind!r}: allowed values are {list(GATE_KINDS)}"]
    args = gate.get("args", {})
    if not isinstance(args, dict):
        return [f"{where}.args must be a dict, got {type(args).__name__}"]
    want = _GATE_ARG_KEYS[kind]
    if set(args) != set(want):
        return [
            f"{where}.args for kind {kind!r} takes exactly "
            f"{list(want) or 'no keys'}, got {sorted(args)}"
        ]
    for key in want:
        value = args[key]
        if not isinstance(value, str) or not value.strip():
            return [f"{where}.args.{key} must be a non-empty string"]
    if kind == "sha-in-prod" and not _SHA_RE.fullmatch(args["sha"]):
        return [f"{where}.args.sha={args['sha']!r} must be 7-40 lowercase hex chars"]
    if kind == "not-before" and not _TS_RE.fullmatch(args["ts"]):
        return [
            f"{where}.args.ts={args['ts']!r} must be ISO-8601 UTC, YYYY-MM-DDTHH:MM:SSZ"
        ]
    return []


def _loop_problems(loop: Any) -> list[str]:
    if not isinstance(loop, dict):
        return [f"meta.loop must be a dict, got {type(loop).__name__}"]
    bad = _unknown_keys(loop, ("max", "until", "body", "count"))
    if bad:
        return [f"meta.loop: {bad}"]
    out: list[str] = []
    cap = loop.get("max")
    if isinstance(cap, bool) or not isinstance(cap, int) or cap <= 0:
        out.append(f"meta.loop.max={cap!r} must be an integer > 0")
    if "until" not in loop:
        out.append("meta.loop.until is required (a gate spec)")
    else:
        out.extend(_gate_problems(loop["until"], "meta.loop.until"))
    body = loop.get("body")
    if not isinstance(body, str) or not body.strip():
        out.append("meta.loop.body must be a template name (non-empty string)")
    if "count" in loop:
        n = loop["count"]
        if isinstance(n, bool) or not isinstance(n, int) or n < 0:
            out.append(
                f"meta.loop.count={n!r} must be an integer >= 0 "
                "(the tick stores the iterations minted so far)"
            )
    return out


def _generate_problems(gen: Any) -> list[str]:
    if not isinstance(gen, dict):
        return [f"meta.generate must be a dict, got {type(gen).__name__}"]
    bad = _unknown_keys(gen, ("role", "from"))
    if bad:
        return [f"meta.generate: {bad}"]
    out: list[str] = []
    if gen.get("role") not in GENERATE_ROLES:
        out.append(
            f"meta.generate.role={gen.get('role')!r}: allowed values are "
            f"{list(GENERATE_ROLES)}"
        )
    src = gen.get("from")
    if not isinstance(src, str) or not src.strip():
        out.append("meta.generate.from must be a non-empty string")
    return out


def plan_problems(meta: dict[str, Any] | None) -> list[tuple[str, str]]:
    """``(key, problem)`` pairs for the plan keys in ``meta``.

    ``key`` is the offending meta key, or ``'gate/loop/generate'`` for the
    at-most-one rule. Keys outside :data:`PLAN_KEYS` are not looked at.
    """
    if not meta:
        return []
    out: list[tuple[str, str]] = []
    if "op" in meta:
        out += [("op", p) for p in _op_problems(meta["op"])]
    if "auto_close" in meta and not isinstance(meta["auto_close"], bool):
        out.append(
            (
                "auto_close",
                f"meta.auto_close must be true or false, got {meta['auto_close']!r}",
            )
        )
    if "gate" in meta:
        out += [("gate", p) for p in _gate_problems(meta["gate"], "meta.gate")]
    if "loop" in meta:
        out += [("loop", p) for p in _loop_problems(meta["loop"])]
    if "generate" in meta:
        out += [("generate", p) for p in _generate_problems(meta["generate"])]
    held = [k for k in EXCLUSIVE_KEYS if meta.get(k) is not None]
    if len(held) > 1:
        out.append(
            (
                "gate/loop/generate",
                f"a node carries at most one of gate / loop / generate, got {held}",
            )
        )
    return out


def validate_plan_meta(meta: dict[str, Any] | None) -> list[str]:
    """Human-readable problems with the plan keys in ``meta`` (empty = fine)."""
    return [msg for _, msg in plan_problems(meta)]


def node_kind(meta: dict[str, Any] | None) -> NodeKind | None:
    """The plan node kind ``meta`` describes, or ``None`` for a plain todo.

    ``gate`` / ``loop`` / ``generate`` win over ``auto_close`` (a generator
    "afterwards behaves as a phase" -- that is the tick's business), and
    ``auto_close`` wins over ``op``. A phase is also any todo with children,
    but children are not in meta, so only ``auto_close`` marks one here.
    """
    if not meta:
        return None
    if meta.get("gate") is not None:
        return "gate"
    if meta.get("loop") is not None:
        return "loop"
    if meta.get("generate") is not None:
        return "generator"
    if meta.get("auto_close") is True:
        return "phase"
    if meta.get("op") is not None:
        return "task"
    return None
