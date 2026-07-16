"""Task → requirement judge — the LLM half of model selection (llm-catalog slice 5).

The proposal's split of labor: a frontier model is **good at task → requirement**
("hard multi-file refactor under a big context → strong code, ≥150k window") but
**biased at requirement → model** (price/window-blind, a "just use opus" reflex).
So the LLM stays a *judge of fit*: it infers a :class:`~precis.utils.llm.policy.Requirement`
vector, and the deterministic :func:`~precis.utils.llm.policy.select_offering` maps
that to a concrete model. The raw catalog is **never** handed to the model to pick
from.

:func:`infer_requirement` runs a cheap one-shot judge (``CLOUD_SMALL`` — a small
model is enough to classify a task) and parses a small JSON requirement.
:func:`choose_model` chains it into the policy. The judge is injectable
(``judge=``) so callers + tests can supply a fixed vector without an LLM call.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from precis.llm_catalog import CAPABILITY_AXES
from precis.utils.llm.policy import Requirement, Selection, select_offering
from precis.utils.llm.router import Tier

if TYPE_CHECKING:
    from precis.store import Store

log = logging.getLogger(__name__)

#: The judge itself is cheap — classifying a task doesn't need the super tier.
_JUDGE_TIER = Tier.CLOUD_SMALL

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
"needs_structured": <true|false>, "max_input": <int or null>}}

min_ordinal = how strong the model must be on that axis (5 = the hardest tasks). \
max_input = your estimate of the context size in tokens, or null.

Task:
<<<
{task}
>>>
"""

#: A judge maps a task string to the raw requirement dict.
Judge = Callable[[str], dict[str, Any]]


def _extract_json(text: str) -> dict[str, Any]:
    """Tolerant: pull the first ``{...}`` block out of a model's reply."""
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not m:
        return {}
    try:
        obj = json.loads(m.group(0))
    except (ValueError, TypeError):
        return {}
    return obj if isinstance(obj, dict) else {}


def _default_judge(task: str) -> dict[str, Any]:
    from precis.utils.llm.router import LlmRequest, dispatch

    res = dispatch(
        LlmRequest(
            tier=_JUDGE_TIER,
            prompt=_PROMPT.format(task=task),
            source="llm:requirement-judge",
            tools_needed=False,
        )
    )
    if isinstance(res.data, dict):
        return res.data
    return _extract_json(res.text or "")


def infer_requirement(
    task: str,
    *,
    tier_floor: Tier,
    transport: str | None = None,
    judge: Judge | None = None,
) -> Requirement:
    """Infer a :class:`Requirement` for ``task`` via the judge (LLM by default).

    Every field is clamped/validated on the way out — an unknown axis becomes
    ``None`` (route on price + window alone), ``min_ordinal`` clamps to 1–5, and
    a non-numeric ``max_input`` becomes ``None`` — so a malformed judge reply can
    never produce an illegal requirement.
    """
    data = (judge or _default_judge)(task) or {}
    axis = data.get("axis")
    if axis not in CAPABILITY_AXES:
        axis = None
    try:
        min_ordinal = max(1, min(5, int(data.get("min_ordinal", 1))))
    except (TypeError, ValueError):
        min_ordinal = 1
    raw_max = data.get("max_input")
    try:
        max_input = int(raw_max) if raw_max is not None else None
    except (TypeError, ValueError):
        max_input = None
    return Requirement(
        tier_floor=tier_floor,
        axis=axis,
        min_ordinal=min_ordinal,
        max_input=max_input,
        needs_tools=bool(data.get("needs_tools")),
        needs_structured=bool(data.get("needs_structured")),
        transport=transport,
    )


def choose_model(
    store: Store,
    task: str,
    *,
    tier_floor: Tier,
    transport: str | None = None,
    judge: Judge | None = None,
) -> tuple[Requirement, Selection]:
    """Task → requirement (LLM) → model (deterministic policy). Returns both, so
    a caller can log the inferred requirement + the reason the policy chose."""
    req = infer_requirement(
        task, tier_floor=tier_floor, transport=transport, judge=judge
    )
    return req, select_offering(store, req)


__all__ = ["Judge", "choose_model", "infer_requirement"]
