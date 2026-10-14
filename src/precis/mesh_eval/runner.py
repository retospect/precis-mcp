"""Run a mesh-eval set against any OpenAI-compatible endpoint and report.

The candidate model is exercised through ``/v1/chat/completions`` directly
(:class:`precis.workers.llm_summarize.LlmClient` under the hood) — any
endpoint by config, so a local vLLM/llama-swap bench, a remote OpenRouter
arm and a dev-box slot all run the same code path. No router, no catalog,
no DB writes: the harness measures, it does not book.

Per lane the report carries what ``local-mesh-upkeep.md`` §1 asks for:

- **accuracy** — mean deterministic score vs the frozen gold, with a
  Wilson 95% CI (plus per-mention agreement for ``categorise``);
- **acceptance rate** — share of replies the judge callable accepts, when
  a judge is wired (the reviewed bar's input);
- **reviewer cost per accepted item** — judge tokens/cost over accepted;
- **transport errors** — any error voids the run's mean like
  ``llm_eval``'s axes (an unmet call is not a wrong answer);
- **the two bars** — auto-apply (accuracy ≥ the set's ``frontier_retest``
  reference *and* the holdout clean; categorise/link-add only, never
  merge) and reviewed (reviewer cost/accepted < frontier cost/accepted,
  when the frontier cost is known).

Packing (``--pack N``) mirrors the frontier's packed categorise runs:
N hub items share one call via
:func:`precis.taxonomy.discovery.build_packed_prompt`, and the reply
splits per hub for scoring — so a local run measures against the same
input shape the gold came from.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from precis.errors import BadInput
from precis.mesh_eval.scorers import score_item
from precis.mesh_eval.sets import EvalSet, MeshItem
from precis.mesh_eval.stats import wilson_interval

log = logging.getLogger(__name__)

#: Lanes whose gold is a frontier verdict and whose action is reversible —
#: eligible for the auto-apply bar at all. Merge never auto-applies
#: (``graph-gardener.md``: "probably not ever for merge and split").
AUTO_APPLY_LANES: frozenset[str] = frozenset({"categorise", "link-add"})


@dataclass(frozen=True, slots=True)
class EndpointConfig:
    """An OpenAI-compatible endpoint: base url + served model id.

    ``api_key`` defaults to the loopback ``"dummy"``; pass a vault-resolved
    key for a hosted endpoint. ``max_tokens=None`` leaves the server's
    default; an item's own ``max_tokens`` (the production caller's cap,
    e.g. the summariser's) overrides per call.
    """

    base_url: str
    model: str
    api_key: str = "dummy"
    max_tokens: int | None = None
    temperature: float | None = 0.0
    timeout_s: float = 120.0


@dataclass(frozen=True, slots=True)
class CallResult:
    """One endpoint call, scored or errored."""

    text: str
    data: dict[str, Any] | None = None
    error: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None


#: ``call_fn(messages, max_tokens) -> CallResult`` — the candidate arm.
CallFn = Callable[[list[dict[str, str]], int | None], CallResult]


@dataclass(frozen=True, slots=True)
class JudgeVerdict:
    """One judge call: accepted or not, plus what the judging cost."""

    accepted: bool
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None


#: ``judge_fn(item, response_text) -> JudgeVerdict | bool`` — a bare bool
#: is an accepted flag with no accounting. The frontier judge on a hosted
#: endpoint is the intended callable; tests inject a stub.
JudgeFn = Callable[[MeshItem, str], "JudgeVerdict | bool"]


@dataclass(frozen=True, slots=True)
class ItemResult:
    item_id: str
    score: float | None  # None when the item could not be scored (transport)
    error: str | None
    detail: dict[str, Any]
    response: str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    judge_accepted: bool | None = None
    judge_error: str | None = None


@dataclass(frozen=True, slots=True)
class LaneReport:
    """One lane's outcome for one candidate model."""

    lane: str
    model: str
    n: int
    errors: int
    accuracy: float | None  # mean score over scored items; None if none
    ci_lo: float
    ci_hi: float
    per_item: list[ItemResult]
    n_judged: int = 0
    n_accepted: int = 0
    judge_errors: int = 0
    judge_tokens: int = 0
    judge_cost_usd: float | None = None
    candidate_tokens: int = 0
    candidate_cost_usd: float | None = None

    @property
    def acceptance_rate(self) -> float | None:
        """Share of judged replies the reference accepted."""
        return self.n_accepted / self.n_judged if self.n_judged else None

    @property
    def acceptance_ci(self) -> tuple[float, float]:
        return wilson_interval(self.n_accepted, self.n_judged)

    @property
    def reviewer_cost_per_accepted(self) -> float | None:
        """Judge tokens spent per accepted item (the reviewed-bar input)."""
        if not self.n_accepted:
            return None
        return self.judge_tokens / self.n_accepted


def endpoint_caller(cfg: EndpointConfig) -> CallFn:
    """A ``CallFn`` over an OpenAI-compatible endpoint — any vLLM, llama-swap,
    litellm or OpenRouter wire."""

    def _call(messages: list[dict[str, str]], max_tokens: int | None) -> CallResult:

        from precis.workers.llm_summarize import LlmClient, LlmConfig

        c = LlmConfig(
            url=cfg.base_url,
            model=cfg.model,
            api_key=cfg.api_key,
            max_tokens=max_tokens or cfg.max_tokens or 1024,
            temperature=cfg.temperature,
            timeout=cfg.timeout_s,
        )
        try:
            res = LlmClient(c).complete(messages)
        except (RuntimeError, OSError) as exc:
            return CallResult(text="", error=str(exc))
        text = res.text or ""
        err = None
        if not text.strip() and not res.has_tool_calls and not res.has_refusal:
            err = f"empty completion (finish_reason={res.finish_reason})"
        return CallResult(
            text=text,
            error=err,
            input_tokens=res.prompt_tokens,
            output_tokens=res.completion_tokens,
            cost_usd=res.cost_usd,
        )

    return _call


def _item_messages(item: MeshItem) -> list[dict[str, str]]:
    if item.messages:
        return item.messages
    return [{"role": "user", "content": item.prompt}]


def _score_reply(item: MeshItem, res: CallResult, ctx: dict[str, Any]) -> ItemResult:
    if res.error:
        return ItemResult(item.item_id, None, res.error, {})
    try:
        scored = score_item(item, res.text, res.data, ctx)
    except Exception as exc:  # a scorer bug is the item's error, not a crash
        return ItemResult(item.item_id, None, f"scorer raised: {exc}", {})
    return ItemResult(
        item.item_id,
        scored.score,
        None,
        scored.detail,
        response=res.text,
        input_tokens=res.input_tokens,
        output_tokens=res.output_tokens,
        cost_usd=res.cost_usd,
    )


def _run_packed_categorise(
    items: list[MeshItem],
    call_fn: CallFn,
    ctx: dict[str, Any],
    pack: int,
) -> list[ItemResult]:
    """Group ``categorise`` items ``pack`` per call, split + score per hub."""
    from precis.taxonomy.config import load_campaign
    from precis.taxonomy.discovery import build_packed_prompt, parse_packed_response

    campaign = str(ctx.get("campaign") or "")
    if not campaign:
        raise BadInput(
            "mesh eval: packed categorise needs scorer_ctx.campaign on the set"
        )
    config = load_campaign(campaign)

    results: dict[str, ItemResult] = {}
    for off in range(0, len(items), pack):
        group = items[off : off + pack]
        hubs = [
            (int(it.meta["ref_id"]), str(it.meta["sentence"]), _meta_mentions(it))
            for it in group
        ]
        prompt = build_packed_prompt(hubs, config)
        res = call_fn([{"role": "user", "content": prompt}], None)
        if res.error:
            for it in group:
                results[it.item_id] = ItemResult(it.item_id, None, res.error, {})
            continue
        # parse_packed_response hands each hub its JSON-array slice — exactly
        # the shape the single-hub scorer expects.
        per_ref, _warns = parse_packed_response(
            res.text, [(r, m, "A") for r, _s, m in hubs]
        )
        for it in group:
            ref_id = int(it.meta["ref_id"])
            _terms, hub_warns, slice_ = per_ref.get(
                ref_id, ((), ("claim absent from packed reply",), res.text)
            )
            # A hub the packed reply never addressed is a transport-shaped
            # failure for that item (the run gave it nothing to score);
            # a present-but-imperfect slice scores normally.
            unaddressed = any("not addressed" in w for w in hub_warns)
            sub = CallResult(
                text=slice_ if isinstance(slice_, str) else "",
                error="claim not addressed in packed reply" if unaddressed else None,
                input_tokens=res.input_tokens,
                output_tokens=res.output_tokens,
                cost_usd=res.cost_usd,
            )
            results[it.item_id] = _score_reply(it, sub, ctx)
    return [results[it.item_id] for it in items]


def _meta_mentions(item: MeshItem) -> list[Any]:
    from precis.mesh_eval.scorers import _mentions_from_meta

    return _mentions_from_meta(item)


def run_set(
    eset: EvalSet,
    call_fn: CallFn,
    *,
    model: str,
    judge_fn: JudgeFn | None = None,
    limit: int | None = None,
    pack: int = 1,
) -> LaneReport:
    """Run one set's graded items through ``call_fn`` and score each reply.

    The holdout items are never touched. ``pack`` > 1 (categorise only)
    sends ``pack`` hubs per call. With ``judge_fn`` every reply is also
    judged — the acceptance rate feeds the reviewed bar.
    """
    lane = eset.lane
    items = eset.graded()
    if limit is not None:
        items = items[:limit]
    ctx = eset.scorer_ctx

    if pack > 1 and lane == "categorise":
        per_item = _run_packed_categorise(items, call_fn, ctx, pack)
    else:
        per_item = []
        for it in items:
            res = call_fn(_item_messages(it), it.max_tokens)
            per_item.append(_score_reply(it, res, ctx))

    n_judged = n_accepted = judge_errors = judge_tokens = 0
    judge_cost: float | None = None
    if judge_fn is not None:
        judged: list[ItemResult] = []
        from dataclasses import replace as _replace

        for it, r in zip(items, per_item, strict=True):
            if r.error:
                judged.append(r)
                continue
            try:
                v = judge_fn(it, r.response)
            except Exception as exc:
                judged.append(_replace(r, judge_error=str(exc)))
                judge_errors += 1
                continue
            verdict = v if isinstance(v, JudgeVerdict) else JudgeVerdict(bool(v))
            n_judged += 1
            n_accepted += bool(verdict.accepted)
            judge_tokens += (verdict.input_tokens or 0) + (verdict.output_tokens or 0)
            if verdict.cost_usd is not None:
                judge_cost = (judge_cost or 0.0) + verdict.cost_usd
            judged.append(_replace(r, judge_accepted=verdict.accepted))
        per_item = judged

    scored = [r for r in per_item if r.error is None and r.score is not None]
    n_err = len(per_item) - len(scored)
    accuracy = (
        sum(r.score for r in scored if r.score is not None) / len(scored)
        if scored
        else None
    )
    # Scores are fractional (a lane can half-answer an item), so the
    # interval is over the share of *fully* correct items — the binary read
    # the two bars phrase ("error rate under the bar").
    n_perfect = sum(1 for r in scored if (r.score or 0) >= 1.0)
    ci_lo, ci_hi = wilson_interval(n_perfect, len(scored))
    cand_tokens = sum(
        int(r.input_tokens or 0) + int(r.output_tokens or 0) for r in per_item
    )
    cand_costs = [r.cost_usd for r in per_item if r.cost_usd is not None]
    return LaneReport(
        lane=lane,
        model=model,
        n=len(per_item),
        errors=n_err,
        accuracy=accuracy,
        ci_lo=ci_lo,
        ci_hi=ci_hi,
        per_item=per_item,
        n_judged=n_judged,
        n_accepted=n_accepted,
        judge_errors=judge_errors,
        judge_tokens=judge_tokens,
        judge_cost_usd=judge_cost,
        candidate_tokens=cand_tokens,
        candidate_cost_usd=sum(cand_costs) if cand_costs else None,
    )


def render_report(
    reports: list[LaneReport],
    *,
    references: dict[str, dict[str, Any]] | None = None,
) -> str:
    """Human-readable per-lane table: accuracy + CI, the two bars, costs."""
    references = references or {}
    lines: list[str] = []
    for r in reports:
        head = f"{r.lane} — {r.model}: n={r.n} errors={r.errors}"
        if r.errors:
            head += "  (VOID — transport errors present)"
        lines.append(head)
        if r.accuracy is not None:
            lines.append(
                f"  accuracy (mean score) {r.accuracy:.3f}   "
                f"all-correct share [{r.ci_lo:.3f}, {r.ci_hi:.3f}] (Wilson 95)"
            )
        acc_ref = (references.get(r.lane) or {}).get("frontier_retest")
        if r.lane not in AUTO_APPLY_LANES:
            lines.append("  auto-apply bar: n/a — this lane never auto-applies")
        elif acc_ref is None:
            lines.append(
                "  auto-apply bar: unmeasurable — no frontier_retest "
                "reference on the set"
            )
        else:
            verdict = "PASS" if (r.accuracy or 0) >= acc_ref else "FAIL"
            lines.append(
                f"  auto-apply bar: {verdict} — accuracy "
                f"{(r.accuracy or 0):.3f} vs frontier test-retest {acc_ref:.3f} "
                f"(also needs a clean human-holdout read)"
            )
        if r.n_judged:
            lo, hi = r.acceptance_ci
            lines.append(
                f"  judged acceptance {r.acceptance_rate:.3f} "
                f"[{lo:.3f}, {hi:.3f}] over {r.n_judged} "
                f"({r.judge_errors} judge errors)"
            )
            if r.reviewer_cost_per_accepted is not None:
                lines.append(
                    f"  reviewer cost per accepted item: "
                    f"{r.reviewer_cost_per_accepted:.0f} judge tokens"
                )
        lines.append(
            f"  candidate tokens: {r.candidate_tokens}"
            + (
                f" (${r.candidate_cost_usd:.4f})"
                if r.candidate_cost_usd is not None
                else ""
            )
        )
    return "\n".join(lines)


__all__ = [
    "AUTO_APPLY_LANES",
    "CallResult",
    "EndpointConfig",
    "ItemResult",
    "LaneReport",
    "endpoint_caller",
    "render_report",
    "run_set",
]
