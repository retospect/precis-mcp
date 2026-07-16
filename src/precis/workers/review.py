"""Reviewer driver — Slice 3 of ``docs/design/todo-tree-plan.md``.

One `claude_agent`-based pass that turns into multiple reviewers
through configuration. Where structural.py + deep_review.py each
re-implemented gate / dedup / prompt-shell / digest-write / mcp
plumbing, this module factors all of that into a single
:func:`run_review_pass` driver. Adding a new reviewer is a
:class:`Reviewer` instance + a context-builder function — no new
SQL, no new claim flow.

Anatomy of a reviewer:

* **identity** — ``name`` (used in ``BatchResult`` and logging),
  ``tier_tag`` (the open-tag literal that marks digest memories),
  ``meta_prefix`` (for the digest's ``meta`` keys).
* **gating** — ``gate_env`` (truthy env var that turns the pass on),
  ``min_interval_hours`` (dedup window against the most recent
  digest of this tier).
* **dispatch** — ``model`` / ``max_turns`` / ``timeout_s``.
* **content** — ``context_builder(store) -> dict[str, str]`` returns
  the live tree/context strings (the SQL reads), and ``modules`` is the
  ordered :class:`~precis.utils.prompt.Module` list that renders the
  prompt (ADR 0038 step 3). The driver assembles those modules against
  an :class:`~precis.utils.prompt.AssemblyContext` whose ``extras`` carry
  ``today`` + ``tier_tag`` + everything the context-builder returned, then
  packages the blocks with :class:`~precis.utils.prompt.ClaudeAgentAdapter`.
  The shared "define your abbreviations" + "only-put-is-a-gripe" footer
  blocks live once here (:data:`_ABBREVIATIONS_MODULE` /
  :data:`_FOOTER_MODULE`) and are reused by every reviewer, so that
  boilerplate is authored a single time.

Both shipped reviewers (structural, deep_review) live as
:class:`Reviewer` instances at module scope; their handlers
(:mod:`precis.workers.structural`, :mod:`precis.workers.deep_review`)
became thin shims so existing imports keep working.

Future reviewers (a hypothetical "patent-watch review", a "skill
catalogue review") get the same dispatch surface without copying
~250 lines of plumbing.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from precis.store import Store
from precis.store.types import Tag
from precis.utils.env import env_flag
from precis.utils.llm.router import LlmRequest, Tier, dispatch
from precis.utils.load_gate import skip_if_high_load
from precis.utils.prompt import (
    AssemblyContext,
    ClaudeAgentAdapter,
    Layer,
    Module,
    Profile,
    assemble,
)
from precis.workers.runner import BatchResult

log = logging.getLogger(__name__)


# ── shared boilerplate modules (the ADR 0038 step-3 dedup win) ────
#
# The two reviewer prompts used to carry VERBATIM-DUPLICATED copies of
# these two paragraphs. They are authored ONCE here and reused by every
# reviewer's module list (see :mod:`precis.workers.structural` /
# :mod:`precis.workers.deep_review`).
#
# Layer note: reviewers emit a single flat directive — the review path
# passes one ``prompt`` to :func:`call_claude_agent` (there is no cached
# system/user split as in the planner). So every reviewer module rides the
# ``VARIABLE`` layer and :class:`ClaudeAgentAdapter` renders them, in
# authored order, into that one user string. The ``Layer`` tag is inert on
# this path; it stays ``VARIABLE`` so the adapter never reorders blocks.


#: The "spell out your abbreviations" admonition. Byte-identical between
#: the two reviewers, so it lives here once.
_ABBREVIATIONS_BLOCK = (
    "**Define your abbreviations.** A memory has no glossary, so spell out\n"
    "each abbreviation on first use — write `AGNR (armchair graphene\n"
    "nanoribbon)`, not a bare `AGNR`. This covers all-caps acronyms and\n"
    "hyphenated compounds (`GNR-FET`)."
)


def _footer_block(ctx: AssemblyContext) -> str:
    """The "do not address anyone / the only put you may make is a gripe" footer.

    Identical between reviewers except for the tier tag the worker will
    stamp on the digest, which is interpolated from ``ctx.extras['tier_tag']``.
    The gripe carve-out (added earlier on this branch) is part of the shared
    text, so it is guaranteed to stay in lock-step across reviewers.
    """
    tier_tag = ctx.extras["tier_tag"]
    return (
        "Do not address anyone. Do not use the precis MCP `put` tool to\n"
        "write a memory directly — the worker will write your output as a\n"
        f"memory tagged `{tier_tag}` after you finish. Your final stdout\n"
        "IS the digest body.\n"
        "\n"
        "Exception: if a precis tool itself errored or returned wrong results\n"
        "while you were reviewing (tooling friction, not a tree finding), you\n"
        "may `put(kind='gripe', text=…)` — search existing gripes first. That\n"
        "is the only `put` you may make; your digest still goes to stdout, not\n"
        "to a memory."
    )


#: Shared trailing modules every reviewer appends after its body. Authored
#: once; imported by each reviewer's module list.
_ABBREVIATIONS_MODULE = Module(
    id="reviewer.abbreviations",
    layer=Layer.VARIABLE,
    build=lambda _ctx: _ABBREVIATIONS_BLOCK,
)
_FOOTER_MODULE = Module(
    id="reviewer.footer",
    layer=Layer.VARIABLE,
    build=_footer_block,
)


# ── Reviewer config ──────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Reviewer:
    """Configuration for one reviewer instance.

    ``context_builder`` is a callable taking the store and returning a
    dict of named strings (the live tree/context reads). The driver
    always injects ``today`` (ISO date) and ``tier_tag`` on top of
    whatever the builder returns, then exposes the merged dict as the
    :class:`~precis.utils.prompt.AssemblyContext` ``extras``.

    ``modules`` is the ordered :class:`~precis.utils.prompt.Module` list
    that renders the prompt (ADR 0038 step 3). Each module's ``build``
    reads what it needs from ``ctx.extras`` — so the reviewer-specific
    body reads ``today`` + the builder's keys, and the shared
    :data:`_FOOTER_MODULE` reads ``tier_tag``. The context strings come
    from a SQL read on the internal corpus, so no escaping is needed.
    """

    name: str
    tier_tag: str
    gate_env: str
    meta_prefix: str
    #: The capability tier the call routes through (ADR 0046 unit 4b); the
    #: model resolves from it at dispatch time so ``PRECIS_LLM_BACKEND`` /
    #: ``PRECIS_MODEL_*`` can switch it. ``model`` is the pre-resolved id
    #: used only for prompt assembly (token budgeting).
    tier: Tier
    model: str
    max_turns: int
    timeout_s: float
    min_interval_hours: float
    context_builder: Callable[[Store], dict[str, str]]
    modules: list[Module]


# ── driver ────────────────────────────────────────────────────────


def run_review_pass(reviewer: Reviewer, store: Store) -> BatchResult:
    """Run one reviewer pass. Counters:

    * ``claimed`` = 1 if we ran the LLM, 0 if dedup'd / disabled
    * ``ok`` = 1 if we wrote a digest memory, 0 otherwise
    * ``failed`` = 1 if the LLM dispatch errored, 0 otherwise
    """
    if not _gate_enabled(reviewer.gate_env):
        log.info(
            "review[%s]: %s not set; skipping",
            reviewer.name,
            reviewer.gate_env,
        )
        return BatchResult(handler=reviewer.name, claimed=0, ok=0, failed=0)
    if skip_if_high_load(f"review[{reviewer.name}]"):
        return BatchResult(handler=reviewer.name, claimed=0, ok=0, failed=0)
    if _recent_digest_exists(store, reviewer.tier_tag, reviewer.min_interval_hours):
        log.info(
            "review[%s]: digest written < %sh ago; skipping",
            reviewer.name,
            reviewer.min_interval_hours,
        )
        return BatchResult(handler=reviewer.name, claimed=0, ok=0, failed=0)
    prompt = _build_prompt(reviewer, store)
    # Routed through the LLM seam (ADR 0046 unit 4b): the reviewer's tier
    # resolves the model at dispatch time, so PRECIS_LLM_BACKEND / PRECIS_MODEL_*
    # can switch it. A per-reviewer PRECIS_<NAME>_MODEL still pins one (None ⇒
    # tier default, which equals the old reviewer.model). Errors fold into
    # res.error rather than raising.
    res = dispatch(
        LlmRequest(
            tier=reviewer.tier,
            source=f"review:{reviewer.name}",
            prompt=prompt,
            tools_needed=True,
            model=os.environ.get(f"PRECIS_{reviewer.name.upper()}_MODEL"),
            mcp_config=_mcp_config_path(),
            max_turns=reviewer.max_turns,
            timeout_s=reviewer.timeout_s,
            # Stream-json gets us cost/turns from the result event; the
            # digest writer sees the unwrapped plain text.
            output_format="stream-json",
            extra_args=("--verbose",),
        )
    )
    if res.error:
        if res.paused:
            # Window-scoped breaker trip (dollar cap / claude-OAuth quota), not a
            # failure. Skip silently — the breaker already raised the one-shot
            # budget/quota alert on the trip transition, and the digest is not
            # deduped, so we re-attempt for free once the window rolls off. This
            # is the fix for the 106k structural "failures" the capped budget
            # spun onto the FAILED-PASSES panel.
            log.debug(
                "review[%s]: paused by breaker; skipping (%s)", reviewer.name, res.error
            )
            return BatchResult(handler=reviewer.name, claimed=0, ok=0, failed=0)
        log.error("review[%s]: claude agent failed: %s", reviewer.name, res.error)
        return BatchResult(handler=reviewer.name, claimed=1, ok=0, failed=1)
    digest_id = _write_digest(store, reviewer, res.text, res.cost_usd)
    log.info(
        "review[%s]: wrote digest memory id=%d cost=$%.4f duration=%.1fs",
        reviewer.name,
        digest_id,
        res.cost_usd or 0.0,
        res.duration_s or 0.0,
    )
    return BatchResult(handler=reviewer.name, claimed=1, ok=1, failed=0)


# ── gate / dedup / prompt / write ─────────────────────────────────


def _gate_enabled(env_var: str) -> bool:
    return env_flag(env_var)


def _recent_digest_exists(store: Store, tier_tag: str, hours: float) -> bool:
    """True when a digest of the given tier was written within ``hours``."""
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            SELECT 1 FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE r.kind = 'memory'
               AND r.deleted_at IS NULL
               AND t.namespace = 'OPEN'
               AND t.value = %s
               AND r.created_at > now() - %s::interval
             LIMIT 1
            """,
            (tier_tag, f"{hours} hours"),
        ).fetchone()
    return row is not None


def _build_prompt(reviewer: Reviewer, store: Store) -> str:
    """Assemble ``reviewer.modules`` into the single directive prompt.

    The context-builder's live strings (plus ``today`` and ``tier_tag``)
    ride the :class:`AssemblyContext` ``extras``; every module reads what
    it needs from there. :class:`ClaudeAgentAdapter` packages the blocks —
    all ``VARIABLE`` on this path — into one user string in authored order
    (the ``CACHED`` half is always empty for reviewers).
    """
    today = datetime.now(UTC).date().isoformat()
    ctx = AssemblyContext(
        store=store,
        ref_id=0,
        model=reviewer.model,
        profile=Profile.AGENT,
        extras={
            "today": today,
            "tier_tag": reviewer.tier_tag,
            **reviewer.context_builder(store),
        },
    )
    _system, user = ClaudeAgentAdapter.render(assemble(reviewer.modules, ctx))
    return user


def _write_digest(
    store: Store,
    reviewer: Reviewer,
    body: str,
    cost_usd: float | None,
) -> int:
    """Insert the digest as a ``kind='memory'`` ref and return its id.

    Tags applied: ``tree-review:YYYY-MM-DD`` + ``{tier_tag}`` +
    ``user:asa`` + ``internal-thought``. ``meta`` keys are namespaced
    by ``{meta_prefix}date`` and ``{meta_prefix}cost_usd`` so a single
    `kind='memory'` row can answer "when did this reviewer last run"
    without inspecting tags.
    """
    today = datetime.now(UTC).date().isoformat()
    meta: dict[str, Any] = {
        f"{reviewer.meta_prefix}date": today,
        f"{reviewer.meta_prefix}cost_usd": cost_usd,
    }
    title = (
        body.strip() or f"{reviewer.name.replace('_', ' ').title()} {today}: (empty)"
    )
    with store.tx() as conn:
        ref = store.insert_ref(
            kind="memory",
            slug=None,
            title=title,
            meta=meta,
            conn=conn,
        )
        for tag in (
            Tag.open(f"tree-review:{today}"),
            Tag.open(reviewer.tier_tag),
            Tag.open("user:asa"),
            Tag.open("internal-thought"),
        ):
            store.add_tag(ref.id, tag, set_by="system", conn=conn)
    return int(ref.id)


def _mcp_config_path() -> Path | None:
    """Resolve ``PRECIS_MCP_CONFIG`` env var to a Path, if set."""
    raw = os.environ.get("PRECIS_MCP_CONFIG")
    if not raw:
        return None
    p = Path(raw)
    return p if p.exists() else None


__all__ = [
    "Reviewer",
    "run_review_pass",
]

#: Re-exported for the per-reviewer module lists (structural / deep_review),
#: which append these two shared trailing blocks after their own body.
_SHARED_TRAILING_MODULES: tuple[Module, Module] = (
    _ABBREVIATIONS_MODULE,
    _FOOTER_MODULE,
)
