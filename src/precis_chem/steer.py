"""STEER: rerank planned routes against a natural-language strategy.

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

Slice 2: ``put(kind='route', strategy='…')`` records the strategy on the
route (``meta.steer_strategies``, ordered + deduped — the last entry is the
page's lead) and the rerank runs inside the ``retrosynth`` job when the
plan lands (inline in the ``put`` when no route node is configured;
``refresh=True`` re-asks). Scores land in the ``meta.steer`` cache the
``view='steer'`` render reads; that view is read-only — a get never calls
the model or writes the ref (gr491367). A second ``put`` that differs only in strategy hits the
content-addressed cache and mints a steer-only job — the dispatch sees the
plan already landed (``cache_key`` match) and skips the engine.

Pre-rank (gr491450): before any LLM call, :func:`prerank` orders every
candidate on deterministic factors (reagent/intermediate hazard alerts,
step count, longest linear sequence, stock, protecting-group steps; cost
has no data source and stays neutral). It never drops a route. The LLM
then scores only the top :data:`LLM_TOP_K` of that order; the rest keep
their pre-rank place below the scored ones. Both orders land in the cache
entry (:func:`cache_entry`) and show in ``view='steer'``. Weights live in
:data:`PRERANK_WEIGHTS`. The pre-rank runs only on the put/job path,
inside :func:`steer`; the view renders what was cached.

Spec and later slices: ``docs/backlog/steer-strategy-route-planning.md``.
"""

from __future__ import annotations

import functools
import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from precis_chem.ir import RouteGraph

#: Rerank candidates per call: the top route plus up to this many - 1.
MAX_CANDIDATES = 10
MAX_STRATEGY_CHARS = 500
MAX_USD_PER_ROUTE = 0.05
TIMEOUT_S = 120.0
LLM_SOURCE = "route-steer"
PROMPT_VERSION = "steer-v1"
#: Bump when the pre-rank logic (not only a weight) changes; part of the
#: cache key together with :data:`PRERANK_WEIGHTS`.
PRERANK_VERSION = "prerank-v1"
#: The LLM scores this many routes from the top of the pre-rank order; the
#: rest stay listed, unscored, in pre-rank order.
LLM_TOP_K = 5

#: Pre-rank weights: penalty points per unit of each factor (lower total
#: penalty ranks higher). The one place the weights live.
#:
#: - ``hazard``: per hazard point; a :data:`FLAGGED_SPECIES` match is 2
#:   points, a :data:`HAZARD_ALERTS` substructure 1. Each distinct hit over
#:   every molecule in the route (target, intermediates, reagents) counts once.
#:   No GHS data is reachable in-repo, so these tables are the hazard source.
#: - ``steps``: per reaction step.
#: - ``longest_seq``: per step on the longest linear sequence
#:   (``metrics['longest_seq']``; neutral when the engine gave none).
#: - ``unsolved``: once when some leaf is not in stock (``graph.solved``).
#: - ``protecting``: per step whose reaction class names a protection or
#:   deprotection.
#: - ``cost``: 0. No price source is reachable in-repo (the AiZynth stock
#:   is a yes/no set without prices), so cost is neutral until one exists.
PRERANK_WEIGHTS: dict[str, float] = {
    "hazard": 3.0,
    "steps": 1.0,
    "longest_seq": 0.5,
    "unsolved": 4.0,
    "protecting": 1.5,
    "cost": 0.0,
}

#: Species flagged by name (SMILES → name): banned or substitute-first
#: solvents and reagents (gr491450 names benzene, HMPA, phosgene). 2 hazard
#: points each.
FLAGGED_SPECIES: dict[str, str] = {
    "c1ccccc1": "benzene",
    "CN(C)P(=O)(N(C)C)N(C)C": "HMPA",
    "O=C(Cl)Cl": "phosgene",
    "ClC(Cl)(Cl)Cl": "carbon tetrachloride",
    "ClCCCl": "1,2-dichloroethane",
    "S=C=S": "carbon disulfide",
    "C=[N+]=[N-]": "diazomethane",
}

#: Structural hazard alerts (name → SMARTS): toxic, explosive or alkylating
#: groups. 1 hazard point each.
HAZARD_ALERTS: dict[str, str] = {
    "acyl halide": "[CX3](=O)[Cl,Br,I]",
    "isocyanate": "N=C=O",
    "azide": "N=[N+]=[N-]",
    "diazo": "[CX3]=[N+]=[N-]",
    "hydrazine": "[NX3][NX3]",
    "peroxide": "[OX2][OX2]",
    "N-nitroso": "N-N=O",
    "methylating agent": "[$([CH3][I,Br]),$([CH3]OS(=O)=O)]",
    "cyanide": "[$([C-]#N),$([CH1]#N)]",
    "toxic metal": "[Hg,Tl,Cd,Pb,As,Cr,Sn,Be,Os]",
}

_PROTECT_RE = re.compile(r"protect", re.IGNORECASE)

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
    #: The route's pre-rank verdict; None on a cache entry from before it.
    pre: PreRank | None = None

    @property
    def below_fold(self) -> bool:
        """Not sent to the LLM: outside the pre-rank's top :data:`LLM_TOP_K`."""
        return self.score is None and self.error is None

    def to_json(self) -> dict[str, Any]:
        return {
            "engine_rank": self.engine_rank,
            "score": self.score,
            "rationale": self.rationale,
            "error": self.error,
            "pre": self.pre.to_json() if self.pre is not None else None,
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> SteerScore:
        pre = d.get("pre")
        return cls(
            engine_rank=int(d["engine_rank"]),
            score=d.get("score"),
            rationale=str(d.get("rationale") or ""),
            error=d.get("error"),
            pre=PreRank.from_json(pre) if pre else None,
        )


@dataclass(frozen=True, slots=True)
class PreRank:
    """One candidate's deterministic pre-rank. ``position`` is 1 for the
    lowest penalty; ``factors`` holds the raw inputs behind ``penalty``."""

    engine_rank: int
    position: int
    penalty: float
    factors: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "engine_rank": self.engine_rank,
            "position": self.position,
            "penalty": self.penalty,
            "factors": self.factors,
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> PreRank:
        return cls(
            engine_rank=int(d["engine_rank"]),
            position=int(d["position"]),
            penalty=float(d["penalty"]),
            factors=dict(d.get("factors") or {}),
        )

    def summary(self) -> str:
        """``penalty 7 — 1 step(s), solved, hazards: acyl halide, cost: no data``."""
        f = self.factors
        parts = [f"{f.get('steps', '?')} step(s)"]
        if f.get("longest_seq") is not None:
            parts.append(f"longest {f['longest_seq']}")
        parts.append("solved" if f.get("solved") else "NOT solved")
        if f.get("protecting"):
            parts.append(f"{f['protecting']} protecting-group step(s)")
        hazards = f.get("hazards") or []
        parts.append("hazards: " + ", ".join(hazards) if hazards else "no hazard alert")
        parts.append("cost: no data")
        return f"penalty {self.penalty:g} — " + ", ".join(parts)


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
    # Every input that changes the order or the set the LLM sees, so a
    # table or weight edit re-ranks instead of serving a stale order.
    prerank_inputs = json.dumps(
        [PRERANK_WEIGHTS, FLAGGED_SPECIES, HAZARD_ALERTS, LLM_TOP_K], sort_keys=True
    )
    for part in (
        PROMPT_VERSION,
        PRERANK_VERSION,
        prerank_inputs,
        strategy,
        graph.target,
    ):
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


@functools.cache
def _alert_patterns() -> tuple[tuple[str, Any], ...]:
    from rdkit import Chem

    out = []
    for name, smarts in HAZARD_ALERTS.items():
        patt = Chem.MolFromSmarts(smarts)
        if patt is not None:  # a bad table entry must not take the rerank down
            out.append((name, patt))
    return tuple(out)


def _canonical(smiles: str) -> str | None:
    from rdkit import Chem, rdBase

    with rdBase.BlockLogs():
        mol = Chem.MolFromSmiles(smiles)
    return Chem.MolToSmiles(mol) if mol is not None else None


@functools.cache
def _flagged_canonical() -> dict[str, str]:
    return {_canonical(smi) or smi: name for smi, name in FLAGGED_SPECIES.items()}


@functools.cache
def molecule_hazards(smiles: str) -> tuple[tuple[str, int], ...]:
    """``(name, points)`` for each flag and alert ``smiles`` hits. Empty when
    rdkit cannot parse it: unknown is neutral, not certified safe. A
    multi-fragment SMILES is checked per fragment."""
    from rdkit import Chem, rdBase

    hits: dict[str, int] = {}
    for frag in smiles.split("."):
        with rdBase.BlockLogs():
            mol = Chem.MolFromSmiles(frag)
        if mol is None:
            continue
        name = _flagged_canonical().get(Chem.MolToSmiles(mol))
        if name is not None:
            hits[name] = 2
        for alert, patt in _alert_patterns():
            if mol.HasSubstructMatch(patt):
                hits.setdefault(alert, 1)
    return tuple(sorted(hits.items()))


def route_factors(graph: RouteGraph) -> dict[str, Any]:
    """The raw pre-rank inputs for one route, before weights."""
    mols: list[str] = [graph.target]
    for st in graph.steps:
        mols += [st.product, *st.reactants]
    hazards: dict[str, int] = {}
    for smi in dict.fromkeys(mols):
        for name, pts in molecule_hazards(smi):
            hazards[name] = max(hazards.get(name, 0), pts)
    longest = (graph.metrics or {}).get("longest_seq")
    return {
        "hazards": sorted(hazards),
        "hazard_points": sum(hazards.values()),
        "steps": len(graph.steps),
        "longest_seq": int(longest)
        if isinstance(longest, int | float) and not isinstance(longest, bool)
        else None,
        "solved": bool(graph.solved),
        "protecting": sum(
            1
            for st in graph.steps
            if _PROTECT_RE.search(f"{st.class_name or ''} {st.reaction_class or ''}")
        ),
        "cost": None,
    }


def _penalty(f: dict[str, Any]) -> float:
    w = PRERANK_WEIGHTS
    return round(
        w["hazard"] * f["hazard_points"]
        + w["steps"] * f["steps"]
        + w["longest_seq"] * (f["longest_seq"] or 0)
        + w["unsolved"] * (0 if f["solved"] else 1)
        + w["protecting"] * f["protecting"],
        3,
    )


def prerank(graph: RouteGraph) -> list[PreRank]:
    """Every candidate, lowest penalty first; ties keep engine order. Never
    drops a route. Deterministic for a fixed graph and weight table."""
    rows = []
    for rank, cand in enumerate(candidates(graph), 1):
        f = route_factors(cand)
        rows.append((_penalty(f), rank, f))
    rows.sort(key=lambda r: (r[0], r[1]))
    return [
        PreRank(engine_rank=rank, position=pos, penalty=pen, factors=f)
        for pos, (pen, rank, f) in enumerate(rows, 1)
    ]


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
    """Pre-rank every candidate, LLM-score the top :data:`LLM_TOP_K` of that
    order, and return all of them best first: scored routes by score (the
    pre-rank breaks ties), then the unscored ones (failed or below the
    fold) in pre-rank order. Only the put/job path calls this."""
    ask_fn = ask or router_ask
    cands = candidates(graph)
    scores: list[SteerScore] = []
    for pre in prerank(graph):
        rank = pre.engine_rank
        if pre.position > LLM_TOP_K:
            scores.append(SteerScore(rank, None, "", None, pre))
            continue
        try:
            text, error = ask_fn(build_prompt(strategy, cands[rank - 1]))
        except Exception as exc:
            text, error = "", f"{type(exc).__name__}: {exc}"
        if error:
            scores.append(SteerScore(rank, None, "", error, pre))
            continue
        score, rationale = parse_reply(text)
        missing = None if score is not None else "no SCORE line"
        scores.append(SteerScore(rank, score, rationale, missing, pre))
    return sorted(
        scores,
        key=lambda s: (
            s.score is None,
            -(s.score or 0.0),
            s.pre.position if s.pre is not None else s.engine_rank,
        ),
    )


def cache_entry(strategy: str, scores: list[SteerScore]) -> dict[str, Any]:
    """The ``meta.steer[key]`` record. ``scores`` holds the final order;
    ``prerank`` the deterministic order as engine ranks."""
    pre = sorted((s.pre for s in scores if s.pre is not None), key=lambda p: p.position)
    return {
        "strategy": strategy,
        "prompt": PROMPT_VERSION,
        "prerank_version": PRERANK_VERSION,
        "weights": dict(PRERANK_WEIGHTS),
        "prerank": [p.engine_rank for p in pre],
        "scores": [s.to_json() for s in scores],
        "at": datetime.now(UTC).isoformat(),
    }


def _verdict(s: SteerScore) -> str:
    if s.score is not None:
        return f"score {s.score:g}"
    if s.below_fold:
        return f"not LLM-scored (below the pre-rank top {LLM_TOP_K})"
    return f"UNSCORED ({s.error})"


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
    ]
    pre = sorted((s.pre for s in scores if s.pre is not None), key=lambda p: p.position)
    if pre:
        lines.append(
            f"pre-rank order ({PRERANK_VERSION}, deterministic: hazard, steps, "
            "stock, protecting groups; cost has no data): "
            + ", ".join(f"#{p.engine_rank}" for p in pre)
            + f" — the LLM scored the top {LLM_TOP_K}."
        )
    lines.append("")
    for pos, s in enumerate(scores, 1):
        cand = cands[s.engine_rank - 1]
        head = (
            f"{pos}. engine route #{s.engine_rank}: "
            + _verdict(s)
            + f" · {len(cand.steps)} step(s) · {'solved' if cand.solved else 'unsolved'}"
        )
        lines.append(head)
        if s.pre is not None:
            lines.append(f"   pre-rank #{s.pre.position}: {s.pre.summary()}")
        if s.rationale:
            lines.append(f"   why: {s.rationale}")
        lines += _route_lines(cand)
    if len(cands) == 1:
        lines += [
            "",
            "Only one route was kept, so there was nothing to rerank. Routes planned "
            "before alternatives were stored have none; re-plan to get them.",
        ]
    return "\n".join(lines)


def _route_lines(cand: RouteGraph) -> list[str]:
    """One indented line per step — the per-route block shared by the steer
    render and the route page."""
    out = []
    for step in cand.steps:
        cls = f" — {step.class_name}" if step.class_name else ""
        out.append(
            f"   {step.id}. `{step.product}` ⇐ "
            + " + ".join(f"`{r}`" for r in step.reactants)
            + cls
        )
    return out


def render_page(strategy: str, graph: RouteGraph, scores: list[SteerScore]) -> str:
    """The route page for a route put with a strategy (slice 2): lead with
    the best route *for the strategy*, the engine's own top route beside it.

    ``scores`` come best-first from :func:`steer` (or its ``meta.steer``
    cache entry). All-unscored falls back to the engine's order with the
    failure named — never a silent default."""
    cands = candidates(graph)
    best = next((s for s in scores if s.score is not None), None)
    lines = [
        f"# route → `{graph.target}`",
        f"engine: {graph.engine} ({graph.engine_version}) · "
        f"{'solved' if graph.solved else 'unsolved'} · "
        f"{len(graph.steps)} step(s)",
        f"strategy: {strategy} — LLM rerank of the pre-rank's top "
        f"{min(LLM_TOP_K, len(cands))} of {len(cands)} candidate(s) "
        f"({PROMPT_VERSION})",
    ]
    if best is None:
        err = scores[0].error if scores else "no candidates"
        lines += [
            "",
            f"all candidates unscored for this strategy ({err}) — "
            "the engine's own order follows.",
            "",
            *_route_lines(graph),
        ]
        return "\n".join(lines) + _render_constraints(graph)

    winner = cands[best.engine_rank - 1]
    lines += [
        "",
        f"## strategy pick — engine route #{best.engine_rank}, "
        f"score {best.score:g}/10 · "
        f"{len(winner.steps)} step(s) · {'solved' if winner.solved else 'unsolved'}",
    ]
    if best.rationale:
        lines.append(f"why: {best.rationale}")
    if best.pre is not None:
        lines.append(f"pre-rank #{best.pre.position}: {best.pre.summary()}")
    lines += _route_lines(winner)
    if best.engine_rank == 1:
        lines += ["", "the engine's own top route is also the best fit."]
        return "\n".join(lines) + _render_constraints(graph)

    top = next((s for s in scores if s.engine_rank == 1), None)
    pos = scores.index(top) + 1 if top is not None else None
    if top is not None and top.score is not None:
        note = f"ranked #{pos} for this strategy, score {top.score:g}/10"
    elif top is not None:
        note = f"{_verdict(top)} for this strategy"
    else:
        note = "not among the scored candidates"
    lines += [
        "",
        f"## engine's top route — {note}",
        *_route_lines(graph),
    ]
    return "\n".join(lines) + _render_constraints(graph)


def _render_constraints(graph: RouteGraph) -> str:
    """The declared-constraint ledger, identical to ``graph.render()``'s tail."""
    return graph.constraints_tail()


__all__ = [
    "LLM_TOP_K",
    "MAX_CANDIDATES",
    "PRERANK_WEIGHTS",
    "PreRank",
    "SteerScore",
    "build_prompt",
    "cache_entry",
    "candidates",
    "normalise_strategy",
    "parse_reply",
    "prerank",
    "render",
    "render_page",
    "route_text",
    "router_ask",
    "steer",
    "steer_key",
]
