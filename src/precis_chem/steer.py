"""STEER slice 1: rerank planned routes against a natural-language strategy.

After Bran et al. 2025 (finding fi259498, paper bran25 §4.2): the engine
proposes several routes, each route is rendered as text (every step's
SMILES, its reaction class, stock leaves), and an LLM scores how well the
route satisfies the chemist's strategy ("avoid protecting groups", "build
the ring late"). The score reranks the engine's candidates and never adds
or edits a route.

One routed call per route, as in the paper. Scoring routes one at a time
avoids the positional bias a single all-routes prompt would carry. Calls go
through :func:`precis.utils.llm.router.route` at :data:`TIER`, each capped
at :data:`MAX_USD_PER_ROUTE`. A failed or unparseable reply leaves that
route unscored, ranked last and marked. It is never given a default score.

Spec and later slices: ``docs/backlog/steer-strategy-route-planning.md``.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from precis_chem.ir import RouteGraph

#: Rerank candidates per call: the top route plus up to this many - 1.
MAX_CANDIDATES = 10
MAX_STRATEGY_CHARS = 500
MAX_USD_PER_ROUTE = 0.05
TIMEOUT_S = 120.0
LLM_SOURCE = "route-steer"
PROMPT_VERSION = "steer-v1"

#: ``ask(prompt) -> (text, error)``. Tests inject a fake; production uses
#: :func:`router_ask`.
Ask = Callable[[str], tuple[str, str | None]]

_SCORE_RE = re.compile(r"SCORE:\s*(\d+(?:\.\d+)?)", re.IGNORECASE)
_RATIONALE_RE = re.compile(r"RATIONALE:\s*(.+)", re.IGNORECASE | re.DOTALL)

_PROMPT = """\
You are an expert synthetic organic chemist reviewing one retrosynthetic \
route proposed by a planning engine.

Chemist's strategic requirement:
{strategy}

The route (target first; each step makes its product from the listed \
precursors; "in stock" means every precursor is buyable):
{route}

Judge only how well this route satisfies the strategic requirement, not \
general route quality unless the requirement asks for it. Reply in exactly \
this form:
SCORE: <0-10, 10 = fully satisfies the requirement>
RATIONALE: <two or three sentences naming the steps that decide the score>\
"""


@dataclass(frozen=True, slots=True)
class SteerScore:
    """One candidate's verdict. ``engine_rank`` is 1 for the engine's top route."""

    engine_rank: int
    score: float | None
    rationale: str
    error: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "engine_rank": self.engine_rank,
            "score": self.score,
            "rationale": self.rationale,
            "error": self.error,
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> SteerScore:
        return cls(
            engine_rank=int(d["engine_rank"]),
            score=d.get("score"),
            rationale=str(d.get("rationale") or ""),
            error=d.get("error"),
        )


def normalise_strategy(strategy: str | None) -> str:
    s = " ".join((strategy or "").split())
    if not s:
        raise ValueError("a strategy is required, e.g. 'avoid protecting groups'")
    if len(s) > MAX_STRATEGY_CHARS:
        raise ValueError(f"strategy longer than {MAX_STRATEGY_CHARS} characters")
    return s


def steer_key(strategy: str, graph: RouteGraph) -> str:
    """Cache key: strategy + the routes scored + prompt version."""
    h = hashlib.sha256()
    for part in (PROMPT_VERSION, strategy, graph.target):
        h.update(part.encode("utf-8"))
        h.update(b"\0")
    for cand in candidates(graph):
        h.update(route_text(cand).encode("utf-8"))
    return h.hexdigest()[:16]


def candidates(graph: RouteGraph) -> list[RouteGraph]:
    return [graph, *graph.alternatives][:MAX_CANDIDATES]


def route_text(graph: RouteGraph) -> str:
    """The plain-text route the LLM reads: one line per step."""
    lines = [
        f"target: {graph.target}",
        f"steps: {len(graph.steps)}; "
        f"{'all leaves buyable' if graph.solved else 'NOT all leaves buyable'}",
    ]
    longest = graph.metrics.get("longest_seq") if graph.metrics else None
    if longest is not None:
        lines.append(f"longest linear sequence: {longest}")
    for s in graph.steps:
        cls = f" [{s.class_name or s.reaction_class}]" if s.reaction_class else ""
        stock = " (in stock)" if s.in_stock else ""
        lines.append(
            f"step {s.id}: {s.product} <= {' + '.join(s.reactants) or '-'}{cls}{stock}"
        )
    return "\n".join(lines)


def build_prompt(strategy: str, graph: RouteGraph) -> str:
    return _PROMPT.format(strategy=strategy, route=route_text(graph))


def parse_reply(text: str) -> tuple[float | None, str]:
    """``(score clamped to 0-10, rationale)``; score None when absent."""
    m = _SCORE_RE.search(text or "")
    score = min(10.0, max(0.0, float(m.group(1)))) if m else None
    r = _RATIONALE_RE.search(text or "")
    rationale = " ".join((r.group(1) if r else text or "").split())[:600]
    return score, rationale


def router_ask(prompt: str) -> tuple[str, str | None]:
    """One routed call (model router; never a direct API key)."""
    from precis.utils.llm.router import LlmRequest, Tier, route

    res = route(
        LlmRequest(
            tier=Tier.MEDIUM,
            source=LLM_SOURCE,
            prompt=prompt,
            max_usd=MAX_USD_PER_ROUTE,
            timeout_s=TIMEOUT_S,
        )
    )
    return res.text or "", res.error


def steer(strategy: str, graph: RouteGraph, ask: Ask | None = None) -> list[SteerScore]:
    """Score every candidate route; return them best first. Unscored
    routes sort last, in engine order."""
    ask_fn = ask or router_ask
    scores: list[SteerScore] = []
    for rank, cand in enumerate(candidates(graph), 1):
        try:
            text, error = ask_fn(build_prompt(strategy, cand))
        except Exception as exc:
            text, error = "", f"{type(exc).__name__}: {exc}"
        if error:
            scores.append(SteerScore(rank, None, "", error))
            continue
        score, rationale = parse_reply(text)
        scores.append(
            SteerScore(
                rank, score, rationale, None if score is not None else "no SCORE line"
            )
        )
    return sorted(
        scores,
        key=lambda s: (s.score is None, -(s.score or 0.0), s.engine_rank),
    )


def render(
    strategy: str, graph: RouteGraph, scores: list[SteerScore], *, cached: bool
) -> str:
    cands = candidates(graph)
    lines = [
        f"# route → `{graph.target}` reranked by strategy",
        f"strategy: {strategy}",
        f"{len(cands)} candidate route(s) from {graph.engine}; scored 0-10 by an LLM "
        f"({PROMPT_VERSION}{', cached' if cached else ''}). "
        "The score ranks the engine's routes; it does not check feasibility.",
        "",
    ]
    for pos, s in enumerate(scores, 1):
        cand = cands[s.engine_rank - 1]
        head = (
            f"{pos}. engine route #{s.engine_rank}: "
            + (f"score {s.score:g}" if s.score is not None else f"UNSCORED ({s.error})")
            + f" · {len(cand.steps)} step(s) · {'solved' if cand.solved else 'unsolved'}"
        )
        lines.append(head)
        if s.rationale:
            lines.append(f"   why: {s.rationale}")
        for step in cand.steps:
            cls = f" — {step.class_name}" if step.class_name else ""
            lines.append(
                f"   {step.id}. `{step.product}` ⇐ "
                + " + ".join(f"`{r}`" for r in step.reactants)
                + cls
            )
    if len(cands) == 1:
        lines += [
            "",
            "Only one route was kept, so there was nothing to rerank. Routes planned "
            "before alternatives were stored have none; re-plan to get them.",
        ]
    return "\n".join(lines)


__all__ = [
    "MAX_CANDIDATES",
    "SteerScore",
    "build_prompt",
    "candidates",
    "normalise_strategy",
    "parse_reply",
    "render",
    "route_text",
    "router_ask",
    "steer",
    "steer_key",
]
