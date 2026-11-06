"""Task → requirement judge — the LLM half of model selection (llm-catalog slice 5).

The proposal's split of labor: a frontier model is **good at task → requirement**
("hard multi-file refactor under a big context → strong code, ≥150k window") but
**biased at requirement → model** (price/window-blind, a "just use opus" reflex).
So the LLM stays a *judge of fit*: it infers a :class:`~precis.utils.llm.policy.Requirement`
vector, and the deterministic :func:`~precis.utils.llm.policy.select_offering` maps
that to a concrete model. The raw catalog is **never** handed to the model to pick
from.

:func:`infer_requirement` runs a cheap one-shot judge (``MEDIUM`` — a small
model is enough to classify a task) and validates a complete JSON requirement
(:class:`ClassifierOutput`, strict: unknown keys, wrong types and model names
are refused): the policy's capability fields plus ``difficulty`` (1–5),
``domain`` (exactly one of ``code``, ``chemistry``, ``geometry``, ``pcb``,
``text``, ``graph``, ``research``, ``other``; default ``other``) and
``consequence`` (``low`` / ``medium`` / ``high``), all carried on the
policy-compatible :class:`Classification`. :func:`choose_model` chains it into
the policy. The judge is injectable (``judge=``) so callers + tests can supply
a fixed vector without an LLM call.

Cache. A default-judge result is kept in-process for 24 hours, at most
``_CACHE_MAX`` entries (the entries nearest expiry go first when full), behind
one lock — callers are threaded. The key is ``cache_scope`` (a namespace; it
never reaches the router) plus the SHA-256 of the canonical JSON of
``features`` — static task features: prompt-chars bucket, files touched,
context-size bucket, expected tool steps, validators — or of the task text when
none are given. Only a validated result is cached: a judge error or a schema
miss never is, and an injected judge neither reads nor writes the cache. Caller
policy constraints (``tier_floor``, ``transport``) are applied after recall, so
a cached demand never overwrites them.

Placement. The judge routes as source ``llm:requirement-judge``; its
operator-owned ``llm.op.llm:requirement-judge`` chain selects castor
gpt-oss-120b (local, ``max_tokens`` ≥2048, ``reasoning_effort`` low,
``timeout_s`` 300) before metered cloud glm-4.7-flash.
:func:`~precis.utils.llm.router.route` refuses the call
(``reason_class='subscription_op_refused'``) when that chain holds any
subscription profile or Claude/Codex CLI rung, so a missing operator row cannot
silently fall back to MEDIUM's Claude default.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from time import monotonic
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from precis.llm_catalog import CAPABILITY_AXES
from precis.utils.llm.json_reply import extract_json_object
from precis.utils.llm.policy import Requirement, Selection, select_offering
from precis.utils.llm.router import Tier

if TYPE_CHECKING:
    from precis.store import Store

log = logging.getLogger(__name__)

#: The judge itself is cheap — classifying a task doesn't need the super tier.
_JUDGE_TIER = Tier.MEDIUM

#: The fixed domain vocabulary; the judge picks one, anything else is refused.
Domain = Literal[
    "code", "chemistry", "geometry", "pcb", "text", "graph", "research", "other"
]


class ClassifierOutput(BaseModel):
    """The complete judge wire contract; model names are never accepted."""

    model_config = ConfigDict(extra="forbid", strict=True)
    axis: str | None
    min_ordinal: int = Field(ge=1, le=5)
    needs_tools: bool
    needs_structured: bool
    max_input: int | None = Field(ge=0)
    difficulty: int = Field(ge=1, le=5)
    domain: Domain = "other"
    consequence: Literal["low", "medium", "high"]

    @field_validator("axis")
    @classmethod
    def valid_axis(cls, value: str | None) -> str | None:
        if value == "none":
            return None
        if value is not None and value not in CAPABILITY_AXES:
            raise ValueError("unknown capability axis")
        return value


@dataclass(frozen=True, slots=True)
class Classification(Requirement):
    """A policy-compatible requirement plus inferred task demand."""

    difficulty: int = 1
    domain: Domain = "other"
    consequence: str = "medium"


_CACHE_TTL = 24 * 60 * 60
#: Entry cap; when full the entries nearest expiry are dropped first.
_CACHE_MAX = 4096
_cache: dict[tuple[str, str], tuple[float, ClassifierOutput]] = {}
#: Guards every read, sweep and insert on ``_cache`` (callers are threaded). It
#: is never held across a judge call — that is an LLM round trip.
_cache_lock = threading.Lock()

_PROMPT = """You are routing a task to the right LLM from a catalog. Infer the \
CAPABILITY the task needs — NEVER a model name (you are price- and window-blind; a \
deterministic policy picks the model from your requirement).

Pick the ONE dominant capability axis (or "none"):
- code — writing / refactoring / debugging code
- long-context-recall — finding facts in a large context
- tool-structured — reliable tool calls / structured output
- reasoning-convergence — hard multi-step reasoning that must converge
- summarize-extract — summarizing or extracting from text

Output ONLY a JSON object, nothing else:
{{"axis": "<axis or none>", "min_ordinal": <1-5>, "needs_tools": <true|false>, \
"needs_structured": <true|false>, "max_input": <int or null>, \
"difficulty": <1-5>, \
"domain": "<code|chemistry|geometry|pcb|text|graph|research|other>", \
"consequence": "<low|medium|high>"}}

min_ordinal = how strong the model must be on that axis (5 = the hardest tasks). \
max_input = your estimate of the context size in tokens, or null.
difficulty = overall task complexity, 1 (trivial) to 5 (hardest).
domain = the field the task works in; exactly one of code, chemistry, geometry, \
pcb, text, graph, research, other (other = none of the rest).
consequence = impact of an incorrect result. Include every field.

Task:
<<<
{task}
>>>
"""

#: A judge maps a task string to the raw requirement dict.
Judge = Callable[[str], dict[str, Any]]


def _default_judge(task: str) -> dict[str, Any]:
    from precis.utils.llm.router import LlmRequest, route

    res = route(
        LlmRequest(
            tier=_JUDGE_TIER,
            prompt=_PROMPT.format(task=task),
            source="llm:requirement-judge",
            tools_needed=False,
        )
    )
    if res.error:
        raise ValueError(f"requirement judge failed: {res.error}")
    if isinstance(res.data, dict):
        return res.data
    return extract_json_object(res.text or "") or {}


def _cache_key(
    cache_scope: str, task: str, features: Mapping[str, Any] | None
) -> tuple[str, str]:
    """``(cache_scope, SHA-256 of the canonical JSON of the features)``.

    The task text stands in when no features are given; an empty mapping cannot
    tell tasks apart, so it counts as none.
    """
    canonical = json.dumps(
        dict(features) if features else {"task": task},
        sort_keys=True,
        separators=(",", ":"),
    )
    return cache_scope, hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _cache_get(key: tuple[str, str]) -> ClassifierOutput | None:
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None and monotonic() < hit[0]:
            return hit[1]
    return None


def _cache_put(key: tuple[str, str], data: ClassifierOutput) -> None:
    now = monotonic()
    with _cache_lock:
        for expired in [k for k, (until, _) in _cache.items() if until <= now]:
            del _cache[expired]
        while key not in _cache and len(_cache) >= _CACHE_MAX:
            del _cache[min(_cache, key=lambda k: _cache[k][0])]
        _cache[key] = (now + _CACHE_TTL, data)


def infer_requirement(
    task: str,
    *,
    tier_floor: Tier,
    transport: str | None = None,
    judge: Judge | None = None,
    cache_scope: str = "llm:choose",
    features: Mapping[str, Any] | None = None,
) -> Classification:
    """Infer a :class:`Requirement` for ``task`` via the judge (LLM by default).

    Validate the complete JSON contract before caching. Cache only default
    judge calls: injected judges remain isolated. ``cache_scope`` namespaces
    the cache entries (it never reaches the router); ``features`` — static,
    JSON-serialisable task features — key them in place of the task text.
    Caller policy constraints are applied after recall so cached demand cannot
    overwrite them.
    """
    key = _cache_key(cache_scope, task, features) if judge is None else None
    data = _cache_get(key) if key is not None else None
    if data is None:
        data = ClassifierOutput.model_validate((judge or _default_judge)(task))
        if key is not None:
            _cache_put(key, data)
    return Classification(
        tier_floor=tier_floor,
        axis=data.axis,
        min_ordinal=data.min_ordinal,
        max_input=data.max_input,
        needs_tools=data.needs_tools,
        needs_structured=data.needs_structured,
        transport=transport,
        difficulty=data.difficulty,
        domain=data.domain,
        consequence=data.consequence,
    )


def choose_model(
    store: Store,
    task: str,
    *,
    tier_floor: Tier,
    transport: str | None = None,
    judge: Judge | None = None,
    features: Mapping[str, Any] | None = None,
) -> tuple[Classification, Selection]:
    """Task → requirement (LLM) → model (deterministic policy). Returns both, so
    a caller can log the inferred requirement + the reason the policy chose."""
    req = infer_requirement(
        task,
        tier_floor=tier_floor,
        transport=transport,
        judge=judge,
        features=features,
    )
    return req, select_offering(store, req)


__all__ = [
    "Classification",
    "ClassifierOutput",
    "Domain",
    "Judge",
    "choose_model",
    "infer_requirement",
]
