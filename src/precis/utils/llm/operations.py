"""Per-operation model routing — the declared operation registry (Phase 1).

The *operation* rung of LLM routing,
sitting between the per-**tier** default (:func:`~precis.utils.llm.router.resolve_model`)
and a call-site ``req.model`` pin. An *operation* is one LLM call site, identified
by its ``req.source`` tag (``reading_brief``, ``meditation``, …). This module owns
**which operations are steerable** and **their code defaults**; the runtime override
lives in ``app_settings`` (:func:`precis.utils.llm.live_config.op_override`) and the
resolution is applied inside :func:`~precis.utils.llm.router.route`.

**Opt-in allow-list, by design.** :data:`LLM_OPERATIONS` is *not* every source that
runs — it is the curated set that is **safe to steer**: an operation is here only if
it (a) routes through ``route()`` and (b) carries no *functional* ``req.model``
pin. Two classes are deliberately **excluded** (:data:`EXCLUDED_OPERATIONS`), so the
override layer never touches them:

- **router-bypassers** — ``fix_gripe`` calls ``resolve_model`` + a raw ``claude -p``
  subprocess, never ``route()``; an ``llm.op.fix_gripe`` override would be a
  silent no-op (routing it through ``route()`` is a named follow-up).
- **functional pins** — ``classify_topics`` pins ``model="summarizer"`` to hit
  the *local-serving* alias (``glm-fleet-flip-safety.md`` Part 1), and
  ``classify:escalate`` pins ``PRECIS_CLASSIFY_ESCALATE_MODEL`` because the pin
  is the point (the Tier-2 re-judge must be a *different* model than tier 1);
  a blanket override beating those pins would silently change semantics.

Both are surfaced **read-only, with the reason**, in the UI (Phase 2); the resolver
here returns ``None`` for any non-registered source, so today's path is byte-identical
for them.

**Ships dark.** With :data:`LLM_OPERATIONS` mirroring each migrated call site's former
``model=`` literal and no ``llm.op.*`` row written, :func:`resolve_op` returns the same
model the call site pinned before, so ``route()`` resolves byte-identically.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from precis.utils.llm.router import (
    Backend,
    Rung,
    Tier,
    filter_tool_rungs,
    parse_chain_rungs,
)

log = logging.getLogger(__name__)

#: ``app_settings`` key prefix for a per-operation override (JSON
#: ``{"tier": <str>?, "model": <str>?, "chain": [<rung>…]?}``). Kept in sync with
#: :data:`precis.utils.llm.live_config.OP_KEY_PREFIX` (the reader), which owns
#: the actual read; re-exported here for the resolver's callers.
OP_KEY_PREFIX = "llm.op."


@dataclass(frozen=True)
class OpDefault:
    """A steerable operation's code default + human-facing metadata.

    ``tier`` / ``model`` are the *default* placement (``model=None`` means "use
    the tier default"); ``env`` names a legacy per-op ``PRECIS_*_MODEL`` escape
    hatch that still overrides the literal (deploy-time fallback, below the
    runtime DB override) so migrating a call site off its ``model=`` arg loses
    no capability. ``label`` / ``description`` are the "defaults visible" UI
    surface; ``note`` is a mouseover shown when the default or grouping took
    real judgment.
    """

    tier: Tier
    model: str | None
    label: str
    description: str
    env: str | None = None
    note: str | None = None


@dataclass(frozen=True)
class ExcludedOp:
    """An observed operation deliberately kept *out* of the steerable allow-list,
    with the reason the override layer must not touch it (surfaced read-only)."""

    reason: str


#: The steerable allow-list — one entry per operation whose model the operator
#: may retune at runtime. Phase 1 seeded the two daily casts (their former
#: ``model="claude-sonnet-5"`` call-site literals migrated here, so the default
#: lives in one place); more routed, pin-free operations join as they're vetted.
#: Both casts have since moved off that claude pin to the BIG chain — see each
#: entry's ``note`` for the incident that forced it.
LLM_OPERATIONS: dict[str, OpDefault] = {
    "reading_brief": OpDefault(
        tier=Tier.BIG,
        model=None,
        label="Morning brief",
        description=(
            "The daily situational-awareness cast compose (voice bm_george). "
            "Prose composition, on the BIG chain (local-first, cloud fallback) "
            "so an unattended daily deliverable never waits on the Claude "
            "subscription quota."
        ),
        env="PRECIS_READING_BRIEF_MODEL",
        note=(
            "Was FRONTIER + an explicit claude-sonnet-5 pin, which put a "
            "scheduled cast on the OAuth quota lane: when the seven-day window "
            "was spent the compose simply failed, and the morning episode "
            "landed hours late or not at all (2026-08-05/06/07). BIG has no "
            "quota gate and its chain starts local, so the cast composes for "
            "~$0 and degrades to a cloud rung instead of to nothing."
        ),
    ),
    "meditation": OpDefault(
        tier=Tier.BIG,
        model=None,
        label="Evening meditation",
        description=(
            "The nightly concept-graph nidra cast compose (voice af_nicole), a "
            "segmented long-form walk. Same BIG chain as the morning brief."
        ),
        env="PRECIS_MEDITATION_MODEL",
        note=(
            "Off the quota lane for the same reason as the morning brief — a "
            "quota-killed compose on 2026-08-05 failed its job, which tagged "
            "the tick child-failed and left collision-skip blocking every "
            "later nidra tick, so one exhausted quota window silently cost "
            "days of meditations rather than one."
        ),
    ),
    "briefing": OpDefault(
        tier=Tier.BIG,
        model=None,
        label="Morning briefing",
        description=(
            "The daily situational-awareness prose briefing (news kind), "
            "distinct from the `reading_brief` cast. Free-text composition "
            "with no tools advertised."
        ),
        env="PRECIS_BRIEFING_MODEL",
        note=(
            "Was FRONTIER/Opus at the call site. Its true cost was invisible "
            "until the 2026-08-07 metering fix — 723 of 724 calls in the "
            "preceding week logged no cost at all. Prose with no tool use is "
            "the easiest thing to serve locally."
        ),
    ),
    "plan_tick": OpDefault(
        tier=Tier.BIG,
        model=None,
        label="Planner tick",
        description=(
            "One LLM tick of the planner coroutine — the single largest LLM "
            "line item on the fleet. Agentic: mints children, yields, halts."
        ),
        env="PRECIS_PLAN_TICK_MODEL",
        note=(
            "The one entry here whose tier ALSO steers a harness switch: "
            "plan_tick picks `_run_claude_tick` (Claude Code + MCP) vs "
            "`_run_oss_tick` (in-process tools loop) from the resolved "
            "chain's rung 0, so a BIG chain moves both the model and the "
            "harness. Revert with an `llm.op.plan_tick` tier override — one "
            "row, no deploy — if the local endpoint can't hold the ~100 "
            "ticks/day. Note the tradeoff registering this makes: a todo's "
            "per-item `meta.llm_tier` (opus|sonnet|haiku) no longer picks the "
            "placement, only the prompt's self-description — an operator cost "
            "control deliberately outranks a per-todo preference."
        ),
    ),
    "taproot:extract-medium": OpDefault(
        tier=Tier.MEDIUM,
        model=None,
        label="Taproot strict extraction (MEDIUM)",
        description=(
            "The migration runner's strict claim extractor "
            "(`extract_claim_strict_medium`), with an in-function "
            "format-flake retry guard on top of the MEDIUM dispatch."
        ),
        note=(
            "Was a deliberate `call_claude_p` router bypass pinned to haiku "
            "(the 2026-08-14 4-hub probe found the then-current "
            "`llm.chain.medium` rung's OSS model intermittently broke the "
            "JSON contract). The 2026-08-15 chain cutover made "
            "`llm.chain.medium` haiku-via-`claude_p` — the same model the "
            "bypass pinned to — so it now dispatches through the router "
            "and logs to `llm_call_log` like every other op."
        ),
    ),
    "chase:verify": OpDefault(
        tier=Tier.BIG,
        model=None,
        label="Taproot evidence verify (BIG)",
        description=(
            "`_chase_llm._verify_support_with_caveats` — the does-this-passage-"
            "support-this-claim verdict behind every taproot evidence edge. "
            "Shared by the `chase` and `hub_refine` passes."
        ),
        note=(
            "The single highest-leverage tier in the taproot lane: its verdict "
            "decides what becomes a citable evidence edge, and a `no`/`partial` "
            "is memoed rather than attached, so a weak verifier silently shapes "
            "the corpus. Reto ruled on 2026-10-22 to route this plain "
            "completion to castor's local gpt-oss lane. BIG is the capability "
            "tier; the per-operation `llm.op.chase:verify` chain selects the "
            "local model and resolves its endpoint through the model card."
        ),
    ),
    "taproot:dedup": OpDefault(
        tier=Tier.MEDIUM,
        model=None,
        label="Taproot dedup judgment",
        description="Deduplicate taproot claims via the `taproot:dedup` operation.",
    ),
    "taproot:merge-confirm": OpDefault(
        tier=Tier.BIG,
        model=None,
        label="Taproot merge confirmation",
        description="Re-check low-confidence dedup merges via `taproot:merge-confirm`.",
    ),
    "taproot:reword": OpDefault(
        tier=Tier.MEDIUM,
        model=None,
        label="Taproot reword sweep",
        description=(
            "`propose_reword` — the per-claim rewrite proposal inside the "
            "`precis taproot reword-sweep` batch job. MEDIUM mirrors the call "
            "site's literal, so default behaviour is unchanged; the prod "
            "`llm.op.taproot:reword` row will pin it for the sweep."
        ),
    ),
    "survey_extract": OpDefault(
        tier=Tier.BIG,
        model=None,
        label="Survey extractor",
        description=(
            "The per-chunk reader inside the `survey_extract` job — reads "
            "one paper chunk against the measure vocabulary and proposes "
            "measure rows. The acceptance sampling (`measure_accept`) is a "
            "separate call and is not covered by this op."
        ),
        note=(
            "BIG by default, as the job's own `tier` param. The prod "
            "`llm.op.survey_extract` row steers it to the castor local "
            "chain for the gardener-value slice 5 trial."
        ),
    ),
    "survey_extract:table_recipe": OpDefault(
        tier=Tier.MEDIUM,
        model=None,
        label="Survey extractor — table recipe",
        description=(
            "The per-table recipe call inside the `survey_extract` job — "
            "maps one parsed grid's columns/rows to measurands, units, the "
            "subject axis and the table-level conditions. One call per "
            "table chunk whose reply grows with the column count, not the "
            "cell count; the cells are read verbatim by the parser."
        ),
        note=(
            "MEDIUM by default — a one-shot JSON mapping of ~10 headers "
            "needs only a haiku-class model, and every value still goes "
            "through the deterministic parse/anchor checks, so a cheap "
            "mapping cannot fabricate a number. Steer with an "
            "`llm.op.survey_extract:table_recipe` row."
        ),
    ),
    "graph_garden:propose": OpDefault(
        tier=Tier.MEDIUM,
        model=None,
        label="Graph gardener proposer",
        description=(
            "The per-candidate proposal verdict inside the `graph_garden` "
            "job — decides whether a deterministic upkeep candidate "
            "(duplicate pair, orphan, stale node, dangling edge, untagged "
            "ref, polysemy/sibling signal) is worth a `proposed` reviews row "
            "for a human/frontier judge. Writes proposals only, never graph "
            "edits."
        ),
        note=(
            "MEDIUM by default — candidate volume is daily-cadence small and "
            "the accepted row only proposes (the second bar is the human or "
            "frontier review of the proposal itself). Steer with an "
            "`llm.op.graph_garden:propose` row when the judge should run "
            'bigger (e.g. `{"tier": "big"}` for a weekly frontier pass).'
        ),
    ),
    "graph_garden:review": OpDefault(
        tier=Tier.FRONTIER,
        model=None,
        label="Graph gardener sampled reviewer",
        description=(
            "The sampled burn-down over open `graph_garden` proposals "
            "(`precis garden review`) — an approve/reject verdict plus a "
            "one-line why per sampled proposal, recorded as a "
            "non-terminal `garden-reviewer` reviews row. The "
            "per-detector agreement rate is the proposer's quality bar."
        ),
        note=(
            "FRONTIER by default — the sample is bounded (per_day, "
            "global, oldest-first) and its verdicts are the yardstick "
            "the cheaper `graph_garden:propose` judge is measured "
            "against, so a weak reviewer would poison the bar rather "
            "than just the sample. Steer with "
            "`llm.op.graph_garden:review` when the sample should run "
            "cheaper or bigger."
        ),
    ),
    "graph_garden:review_fable": OpDefault(
        tier=Tier.FRONTIER,
        model="claude-fable-5",
        label="Graph gardener sampled reviewer (Fable)",
        description=(
            "The Fable re-review beside `graph_garden:review` — an "
            "independent approve/reject verdict on a sample of the same "
            "proposals Opus saw, recorded as non-terminal "
            "`garden-reviewer-fable` rows. The Opus–Fable agreement "
            "rate cross-checks the reviewer's bar."
        ),
        note=(
            "Model is the point here: the op exists so the re-review "
            "lands on a *different* model than the Opus sample — "
            "otherwise the agreement column measures nothing. Steer "
            "with `llm.op.graph_garden:review_fable`; pinning the same "
            "model as `graph_garden:review` degenerates the check."
        ),
    ),
    "graph_garden:consume": OpDefault(
        tier=Tier.FRONTIER,
        model=None,
        label="Graph gardener proposal consumer",
        description=(
            "The terminal review step after each `graph_garden` run "
            "(gr506044): an approve/reject verdict with a reason code on "
            "each pending merge/relink proposal. Approve applies a merge "
            "through the undoable apply door; reject closes the proposal "
            "with a machine-readable reason."
        ),
        note=(
            "FRONTIER (Opus) by Reto's 2026-10-23 ruling — this verdict "
            "applies merges, so it gets the strongest default. Bounded by "
            "the job's `review_limit` (default 10 per run)."
        ),
    ),
    "llm_summarize": OpDefault(
        tier=Tier.SMALL,
        model=None,
        label="Chunk summariser",
        description=(
            "The per-chunk `llm_summarize` backfill (SMALL tier, lite call "
            "log). Registered so `llm.op.llm_summarize` can carry its own "
            "`chain` (e.g. local-first) while the rest of SMALL stays on "
            "`llm.chain.small`."
        ),
        note=(
            "model=None is today's behaviour exactly: the worker's client pins "
            "no model, so route() resolves resolve_model(SMALL). No row ⇒ "
            "byte-identical routing."
        ),
    ),
    "classify": OpDefault(
        tier=Tier.SMALL,
        model=None,
        label="Chunk classifier (junk→role3)",
        description=(
            "The per-chunk `classify` cascade — a `junk` gate then the "
            "own/background/furniture `role3` call — the fleet's "
            "highest-volume LLM lane (the ~1.3M-chunk corpus drain). Lite "
            "call-log rows."
        ),
        env="PRECIS_CLASSIFY_MODEL",
        note=(
            "Was the canonical EXCLUDED op: call sites pinned "
            '`model="summarizer"` against a thinking-model empty-response bug '
            "(glm-fleet-flip-safety.md). The worker pass has since moved to "
            "the SMALL tier chain — non-thinking rungs — so the pin is gone "
            "and the hazard with it. Registered so an `llm.op.classify` chain "
            "(e.g. castor's local gpt-oss-120b) steers the drain without a "
            "deploy. The Tier-2 escalate client runs as `classify:escalate` "
            "(unregistered) so its PRECIS_CLASSIFY_ESCALATE_MODEL pin survives "
            "— the re-judge must be a different model than tier 1."
        ),
    ),
}

#: Observed operations deliberately NOT steerable, with why. The override layer
#: never reaches these; the UI shows them read-only with the reason.
EXCLUDED_OPERATIONS: dict[str, ExcludedOp] = {
    "fix_gripe": ExcludedOp(
        "bypasses the router — agentic `call_claude_agent` chokepoint, not route()"
    ),
    "classify:escalate": ExcludedOp(
        "model pinned for correctness — the Tier-2 re-judge must run a "
        "*different* model than the tier-1 `classify` chain"
    ),
    "classify_topics": ExcludedOp(
        "model pinned in code for correctness (local-serving `summarizer` alias)"
    ),
}

# LLM call sites observed but still **unregistered** — an `llm.op.<source>`
# row is inert for these until a vetted `LLM_OPERATIONS` entry lands: the
# quest_tick family (`quest_review`, `quest_weave`, `quest_roadmap`),
# `doctor_tick`, `diagnose_gripe`, `draft_refresh` and `hub_tagline`. Unlike
# `EXCLUDED_OPERATIONS` (deliberately never steerable) these are simply
# un-vetted — registering one means adding an entry whose default mirrors the
# call site, not just writing the row.


def is_steerable(source: str | None) -> bool:
    """``True`` iff ``source`` is a registered, override-able operation."""
    return bool(source) and source in LLM_OPERATIONS


def op_default(source: str | None) -> OpDefault | None:
    """The registry default for ``source``, or ``None`` if not steerable."""
    if not source:
        return None
    return LLM_OPERATIONS.get(source)


def excluded_reason(source: str | None) -> str | None:
    """Why ``source`` is non-steerable (read-only in the UI), or ``None``."""
    if not source:
        return None
    ex = EXCLUDED_OPERATIONS.get(source)
    return ex.reason if ex else None


def resolve_op(source: str | None) -> tuple[Tier, str | None] | None:
    """Resolve the effective ``(tier, model)`` for a **registered** operation.

    Returns ``None`` for any non-registered ``source`` — the caller then keeps
    today's ``req.model``-or-``resolve_model`` path untouched (so functional
    pins like ``classify`` → ``summarizer`` and router-bypassers are never
    steered). For a registered source, precedence is (highest first):

        runtime DB override (``llm.op.<source>``) > legacy ``env`` hatch >
        registry literal

    for the model, plus a tier remap from the DB override. The returned
    ``model`` is a *fallback* for ``route()`` (``model or resolve_model(tier)``),
    so a per-tier ``llm.chain.<tier>`` rung that pins its own model still wins
    over this — matching the proposal's precedence ladder.
    """
    default = LLM_OPERATIONS.get(source or "")
    if default is None:
        return None

    tier = default.tier
    model = default.model
    # Deploy-time env hatch (legacy per-op PRECIS_*_MODEL), still honoured under
    # the runtime override so migrating off a call-site model= arg loses nothing.
    if default.env:
        env_model = os.environ.get(default.env)
        if env_model:
            model = env_model
    # Runtime DB override — the operator's live control, top priority.
    from precis.utils.llm import live_config

    override = live_config.op_override(source or "")
    if override:
        ov_tier = override.get("tier")
        if ov_tier:
            try:
                tier = Tier(ov_tier)
            except ValueError:
                log.warning(
                    "operations: ignoring bad tier %r for op %s", ov_tier, source
                )
        ov_model = override.get("model")
        if ov_model:
            model = ov_model
    return tier, model


#: ``(source, reason)`` pairs already warned about this process — resolution runs
#: per call, so an unbounded warning per call would flood the log.
_warned: set[tuple[str, str]] = set()


def _warn_once(source: str, reason: str, msg: str, *args: object) -> None:
    if (source, reason) in _warned:
        return
    _warned.add((source, reason))
    log.warning(msg, *args)


def resolve_op_chain(
    source: str | None, *, tools_needed: bool, backend: Backend
) -> list[Rung] | None:
    """The per-operation rung list from ``llm.op.<source>``'s ``chain`` key, or
    ``None`` (→ the caller uses the tier chain).

    Only a registered source honours it. The rungs use the ``llm.chain.<tier>``
    grammar and the same parser/validation
    (:func:`~precis.utils.llm.router.parse_chain_rungs`); a malformed chain is
    logged and ignored (tier chain), and a tool-using call drops tool-less
    rungs, an emptied filter also yielding ``None``.
    """
    if not source or source not in LLM_OPERATIONS:
        return None
    from precis.utils.llm import live_config

    override = live_config.op_override(source)
    if not override or "chain" not in override:
        return None
    raw = override["chain"]
    key = live_config.op_key(source)
    if not isinstance(raw, list) or not raw:
        _warn_once(
            source,
            "shape",
            "operations: %s chain is not a non-empty list — tier chain",
            key,
        )
        return None
    rungs, bad = parse_chain_rungs(raw)
    if bad is not None:
        reason, i, detail = bad
        _warn_once(
            source,
            f"rung {i} {reason}",
            "operations: %s chain rung %d %s (%r) — using the tier chain",
            key,
            i,
            reason,
            detail,
        )
        return None
    if tools_needed:
        kept = filter_tool_rungs(rungs, key, warn=(source, "tools") not in _warned)
        if len(kept) != len(rungs):
            _warned.add((source, "tools"))
        rungs = kept
    return rungs or None


__all__ = [
    "EXCLUDED_OPERATIONS",
    "LLM_OPERATIONS",
    "OP_KEY_PREFIX",
    "ExcludedOp",
    "OpDefault",
    "excluded_reason",
    "is_steerable",
    "op_default",
    "resolve_op",
    "resolve_op_chain",
]
