"""Reviewer driver — Slice 3 of ``docs/backlog/todo-tree-plan.md``.

One `claude_agent`-based pass that turns into multiple reviewers
through configuration. Where structural.py + deep_review.py each
re-implemented gate / dedup / prompt-shell / digest-write / mcp
plumbing, this module factors all of that into a single
:func:`run_review_pass` driver. Adding a new reviewer is a
:class:`Reviewer` instance + a context-builder function — no new
SQL, no new claim flow.

Anatomy of a reviewer:

* **identity** — ``name`` (used in ``BatchResult`` and logging),
  ``digest_tag`` (the open-tag literal that marks digest memories),
  ``meta_prefix`` (for the digest's ``meta`` keys).
* **gating** — ``gate_env`` (truthy env var that turns the pass on),
  ``min_interval_hours`` (dedup window against the most recent
  digest of this reviewer).
* **dispatch** — ``model`` / ``max_turns`` / ``timeout_s``.
* **content** — ``context_builder(store) -> dict[str, str]`` returns
  the live tree/context strings (the SQL reads), and ``modules`` is the
  ordered :class:`~precis.utils.prompt.Module` list that renders the
  prompt. The driver assembles those modules against
  an :class:`~precis.utils.prompt.AssemblyContext` whose ``extras`` carry
  ``today`` + ``digest_tag`` + everything the context-builder returned, then
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

from precis.alerts import raise_alert, resolve_stale_alerts
from precis.store import Store
from precis.store.types import Tag
from precis.utils.db_log_handler import _resolve_host_name
from precis.utils.env import env_flag
from precis.utils.llm.router import LlmRequest, LlmResult, Tier, route
from precis.utils.load_gate import skip_if_high_load
from precis.utils.prompt import (
    AssemblyContext,
    Block,
    ClaudeAgentAdapter,
    Layer,
    Module,
    Profile,
    assemble,
    persist_assembled_context,
)
from precis.workers.runner import BatchResult

log = logging.getLogger(__name__)

#: Tier-1 tool deny for every standing reviewer pass (gr179501). A reviewer
#: reads the corpus and emits a plain-text digest — the worker writes that
#: stdout as the digest-tagged memory, so the reviewer must NOT ``put`` a
#: memory itself. Its one sanctioned write is the gripe carve-out in
#: :func:`_footer_block`, so ``mcp__precis__put`` stays allowed; everything
#: that mutates existing refs, writes the filesystem, runs a shell, or hits
#: the open web is denied explicitly here rather than trusted to the prompt
#: footer (these standing passes never set an envelope, so it defaults
#: permissive — the enforcement gap the gripe flags).
_REVIEWER_DISALLOWED_TOOLS: tuple[str, ...] = (
    "Bash",
    "Write",
    "Edit",
    "NotebookEdit",
    "WebFetch",
    "WebSearch",
    "mcp__precis__edit",
    "mcp__precis__delete",
    "mcp__precis__tag",
    "mcp__precis__link",
)


# ── shared boilerplate modules (the prompt assembler step-3 dedup win) ────
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

    Identical between reviewers except for the digest tag the worker will
    stamp on the digest, which is interpolated from
    ``ctx.extras['digest_tag']``. The gripe carve-out (added earlier on this
    branch) is part of the shared text, so it is guaranteed to stay in
    lock-step across reviewers.
    """
    digest_tag = ctx.extras["digest_tag"]
    return (
        "Do not address anyone. Do not use the precis MCP `put` tool to\n"
        "write a memory directly — the worker will write your output as a\n"
        f"memory tagged `{digest_tag}` after you finish. Your final stdout\n"
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
    always injects ``today`` (ISO date) and ``digest_tag`` on top of
    whatever the builder returns, then exposes the merged dict as the
    :class:`~precis.utils.prompt.AssemblyContext` ``extras``.

    ``modules`` is the ordered :class:`~precis.utils.prompt.Module` list
    that renders the prompt. Each module's ``build``
    reads what it needs from ``ctx.extras`` — so the reviewer-specific
    body reads ``today`` + the builder's keys, and the shared
    :data:`_FOOTER_MODULE` reads ``digest_tag``. The context strings come
    from a SQL read on the internal corpus, so no escaping is needed.
    """

    name: str
    digest_tag: str
    gate_env: str
    meta_prefix: str
    #: The capability tier the call routes through; the
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
    if _recent_digest_exists(store, reviewer.digest_tag, reviewer.min_interval_hours):
        log.info(
            "review[%s]: digest written < %sh ago; skipping",
            reviewer.name,
            reviewer.min_interval_hours,
        )
        return BatchResult(handler=reviewer.name, claimed=0, ok=0, failed=0)
    if _recent_failure(store, reviewer):
        log.info(
            "review[%s]: dispatch failed < %sh ago; backing off "
            "(not re-attempting every tick)",
            reviewer.name,
            reviewer.min_interval_hours,
        )
        return BatchResult(handler=reviewer.name, claimed=0, ok=0, failed=0)
    blocks = _assemble_reviewer_blocks(reviewer, store)
    _system, prompt = ClaudeAgentAdapter.render(blocks)
    # Resolved once so the tool-starvation check below (_is_tool_starved) can
    # tell "no config offered" (never starved) from "config offered, zero
    # precis calls made" (gr197478) without re-deriving it from the request.
    mcp_config = _mcp_config_path()
    # Routed through the LLM seam: the reviewer's tier
    # resolves the model at dispatch time, so PRECIS_LLM_BACKEND / PRECIS_MODEL_*
    # can switch it. A per-reviewer PRECIS_<NAME>_MODEL still pins one (None ⇒
    # tier default, which equals the old reviewer.model). Errors fold into
    # res.error rather than raising.
    res = route(
        LlmRequest(
            tier=reviewer.tier,
            source=f"review:{reviewer.name}",
            prompt=prompt,
            tools_needed=True,
            model=os.environ.get(f"PRECIS_{reviewer.name.upper()}_MODEL"),
            mcp_config=mcp_config,
            max_turns=reviewer.max_turns,
            timeout_s=reviewer.timeout_s,
            # Explicit tier-1 deny (gr179501): read + emit a digest + the
            # gripe carve-out only; no mutate/fs-write/shell/web.
            disallowed_tools=_REVIEWER_DISALLOWED_TOOLS,
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
        if res.interrupted:
            # The worker was told to stop mid-review (a launchd/deploy bounce or
            # a jetsam cull) and the ``claude -p`` child died with it (exit ≥128
            # = signal). This is NOT a dispatch/config failure — the review
            # simply didn't run — so DON'T write a cooldown marker: a false
            # ``review-fail`` marker would back the pass off for
            # ``min_interval_hours`` (5h) even though nothing is wrong. The
            # digest isn't deduped, so the next tick re-attempts for free.
            log.info(
                "review[%s]: interrupted (signal exit) mid-dispatch; not a "
                "failure, will retry next tick (%s)",
                reviewer.name,
                res.error,
            )
            return BatchResult(handler=reviewer.name, claimed=0, ok=0, failed=0)
        # A non-paused dispatch error (agent-launch / config / transport — e.g.
        # the agent container missing PRECIS_DATABASE_URL on a host) writes a
        # cooldown marker so the NEXT tick backs off (see _recent_failure).
        # Without it the pass re-dispatches every scheduler cycle and one
        # persistent config gap becomes a flood (spark: 124k "failures"/24h) —
        # the same spin the ``paused`` branch above already fixed for budget
        # trips (see LlmResult.paused).
        log.error("review[%s]: claude agent failed: %s", reviewer.name, res.error)
        _write_failure_marker(store, reviewer, res.error)
        return BatchResult(handler=reviewer.name, claimed=1, ok=0, failed=1)
    if _is_silent_empty(res):
        # Empty-result assertion (OPEN-ITEMS §🔇). The dispatch reported
        # success, but the pass did *nothing* — zero tool calls, no text,
        # $0, 0/None turns. On a CAPABLE host (the probe already diverts
        # incapable ones) that is a silent failure, not a clean no-op, and
        # must not become a $0 "success" digest that vanishes. Back the
        # pass off (marker) and raise a visible alert instead.
        log.error(
            "review[%s]: silent-empty pass (cost=$0, turns=%s, tool_calls=0, "
            "no text) — raising alert, not writing digest",
            reviewer.name,
            res.turns_used,
        )
        _write_failure_marker(
            store, reviewer, "silent-empty pass (0 tool calls, no output, $0)"
        )
        _raise_empty_pass_alert(store, reviewer)
        return BatchResult(handler=reviewer.name, claimed=1, ok=0, failed=1)
    if _is_tool_starved(res, mcp_config):
        # Tool-starvation assertion (gr197478). The pass completed cleanly and
        # wrote plausible prose — _is_silent_empty doesn't see this at all —
        # but a config was on offer and it never called a precis tool. That is
        # exactly the shape the 2026-08-02 dropped-credential incident took:
        # `precis serve` couldn't authenticate, so no tool ever registered,
        # and 21 consecutive passes over ~4.6 days emitted confident-looking
        # $-spending digests reasoned from the bare prompt alone. Back the
        # pass off (marker) and raise a visible alert instead of storing the
        # digest as a normal healthy pass.
        log.error(
            "review[%s]: tool-starved pass (mcp_config set, non-empty text, "
            "0 mcp__precis__* tool calls) — raising alert, not writing digest",
            reviewer.name,
        )
        _write_failure_marker(store, reviewer, "tool-starved: precis MCP never used")
        _raise_tool_starved_alert(store, reviewer)
        return BatchResult(handler=reviewer.name, claimed=1, ok=0, failed=1)
    digest_id = _write_digest(store, reviewer, res.text, res.cost_usd)
    # The FULL assembled prompt INPUT, the twin of the
    # ``meta.transcript`` output capture on a plan_tick job ref — reviewers
    # mint no job ref, so the digest memory itself is the closest per-run
    # artifact to attach it to. Never-fatal internally.
    persist_assembled_context(store, digest_id, blocks)
    # A real digest landed — clear any empty-pass / tool-starved alert this
    # reviewer left open.
    _resolve_empty_pass_alert(store, reviewer)
    _resolve_tool_starved_alert(store, reviewer)
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


def _recent_digest_exists(store: Store, digest_tag: str, hours: float) -> bool:
    """True when a digest of the given tag was written within ``hours``."""
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            SELECT 1 FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE r.kind = 'memory'
               AND r.retired_at IS NULL
               AND t.namespace = 'OPEN'
               AND t.value = %s
               AND r.created_at > now() - %s::interval
             LIMIT 1
            """,
            (digest_tag, f"{hours} hours"),
        ).fetchone()
    return row is not None


def _failure_tag(reviewer: Reviewer) -> str:
    """Open-tag literal marking a reviewer's dispatch-failure cooldown."""
    return f"review-fail:{reviewer.name}"


def _recent_failure(store: Store, reviewer: Reviewer) -> bool:
    """True when this reviewer's dispatch failed within ``min_interval_hours``.

    A non-paused dispatch failure writes a cooldown marker
    (:func:`_write_failure_marker`); this gate then backs the pass off to its
    normal cadence instead of re-dispatching every scheduler tick. One
    persistent failure (e.g. the agent container missing ``PRECIS_DATABASE_URL``
    on a host) would otherwise re-run ~1×/s and flood the error surface
    (observed: spark 124k ERROR/24h, all ``review[structural]``).
    """
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            SELECT 1 FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE r.kind = 'memory'
               AND r.retired_at IS NULL
               AND t.namespace = 'OPEN'
               AND t.value = %s
               AND r.created_at > now() - %s::interval
             LIMIT 1
            """,
            (_failure_tag(reviewer), f"{reviewer.min_interval_hours} hours"),
        ).fetchone()
    return row is not None


def _is_silent_empty(res: LlmResult) -> bool:
    """True when a dispatched pass *returned* but did nothing.

    The conjunction — $0 cost ∧ 0/None turns ∧ a *definitive* zero tool
    calls ∧ no output text. ``tool_calls == 0`` (not ``None``) is the hard
    anchor: it only holds on the stream-json path that positively counted
    zero ``tool_use`` blocks, so a genuinely cheap-but-real pass (any tool
    call, or any text) is never flagged, and a transport that cannot report
    tool calls (``None``) can never trip it. Cost/turns are permissive
    (``None`` allowed) — they corroborate; the anchor demands proof.
    """
    return (
        res.tool_calls == 0
        and not (res.text or "").strip()
        and (res.cost_usd is None or res.cost_usd == 0.0)
        and (res.turns_used is None or res.turns_used == 0)
    )


def _mcp_precis_tool_calls(res: LlmResult) -> int | None:
    """Count of ``mcp__precis__*`` tool_use blocks in ``res``, or ``None`` when
    unknown (a transport without a stream-json trace to parse).

    Reuses :func:`~precis.utils.claude_agent.count_tool_use_events` — the same
    parser :func:`~precis.workers.job_types.plan_tick._precis_tools_used` uses
    for the equivalent planner-side guard (891a2d81) — scoped to the precis
    prefix, so a pass that only reached for a built-in tool (``Read``,
    ``Bash``, …) still reads as starved. ``res.tool_calls`` is deliberately
    NOT reused here: it is the *unscoped* total across every tool the pass
    called (see :func:`~precis.utils.llm.router.result_from_agent`), so a
    pass that only used non-precis tools would read as "acted" under it.
    """
    if not res.raw_text:
        return None
    from precis.utils.claude_agent import count_tool_use_events

    return count_tool_use_events(res.raw_text, name_prefix="mcp__precis__")


def _is_tool_starved(res: LlmResult, mcp_config: Path | None) -> bool:
    """True when tools were on offer but the pass never touched precis (gr197478).

    Distinct from :func:`_is_silent_empty`, which only catches a pass that did
    *nothing at all*. This catches the more dangerous shape: the dispatch
    reported success, spent turns/money, and wrote real-looking prose — the
    exact silhouette of the 2026-08-02 incident, where 23ff8cf8 dropped the
    inline DB password from the MCP config template without pinning
    ``PGPASSFILE``, so ``precis serve`` never authenticated and no
    ``mcp__precis__*`` tool ever registered. ``claude -p`` reasoned from the
    bare prompt alone and produced plausible digests for 21 consecutive
    passes over ~4.6 days with nothing to show it. Requires an ``mcp_config``
    was actually offered (no config ⇒ tools were never on the table, not a
    starvation), non-empty output text (an empty run is already
    :func:`_is_silent_empty`'s to catch), and a *definitive* zero
    ``mcp__precis__*`` call count — ``None`` (a transport that can't report a
    stream trace) never trips it, same never-a-false-zero discipline as
    :func:`_is_silent_empty`.
    """
    if mcp_config is None:
        return False
    if not (res.text or "").strip():
        return False
    return _mcp_precis_tool_calls(res) == 0


def _empty_alert_source(reviewer: Reviewer) -> str:
    """Per-reviewer alert source so a resolve touches only this reviewer."""
    return f"review:empty:{reviewer.name}"


def _raise_empty_pass_alert(store: Store, reviewer: Reviewer) -> None:
    """Surface a silent-empty pass as a ``warn`` alert (visible, not paging)."""
    host = _resolve_host_name()
    # Fingerprint is per-reviewer, NOT per-host: the source-scoped
    # `_resolve_empty_pass_alert` clears by (source) alone, so a host in the
    # fingerprint would let one host's recovery resolve another host's still-
    # broken alert. Keep the identity symmetric with the resolve; the host is
    # carried in the title/detail for the operator, not the dedup key.
    fingerprint = f"{reviewer.name}:empty-pass"
    title = (
        f"[review-empty] {reviewer.name} produced nothing on {host} "
        "($0, 0 tool calls, no text)"
    )
    detail = (
        f"The {reviewer.name} reviewer dispatched on a capable host but "
        "returned zero tool calls, no text, and $0 cost — a silent failure, "
        "not a clean no-op. Check the agent transport on this host (OAuth "
        "token, MCP config, model availability). The pass is backed off for "
        "its normal interval and will retry."
    )
    raise_alert(
        store,
        source=_empty_alert_source(reviewer),
        fingerprint=fingerprint,
        title=title,
        detail=detail,
        severity="warn",
    )


def _resolve_empty_pass_alert(store: Store, reviewer: Reviewer) -> None:
    """Clear this reviewer's empty-pass alert once it produces a real digest."""
    resolve_stale_alerts(
        store, source=_empty_alert_source(reviewer), live_fingerprints=()
    )


def _tool_starved_alert_source(reviewer: Reviewer) -> str:
    """Per-reviewer alert source so a resolve touches only this reviewer."""
    return f"review:tool-starved:{reviewer.name}"


def _raise_tool_starved_alert(store: Store, reviewer: Reviewer) -> None:
    """Surface a tool-starved pass as a ``warn`` alert (gr197478)."""
    host = _resolve_host_name()
    # Fingerprint is per-reviewer, NOT per-host — symmetric with the resolve,
    # same reasoning as :func:`_raise_empty_pass_alert`.
    fingerprint = f"{reviewer.name}:tool-starved"
    title = (
        f"[review-tool-starved] {reviewer.name} wrote a digest with zero "
        f"precis tool calls on {host}"
    )
    detail = (
        f"The {reviewer.name} reviewer dispatched with an MCP config, "
        "produced non-empty text, but made zero mcp__precis__* tool calls — "
        "it reviewed nothing but its own prompt. This is the failure mode "
        "behind gr197478 (2026-08-02: a dropped inline DB password left "
        "`precis serve` unable to authenticate, so no tool ever registered "
        "and 21 consecutive passes over ~4.6 days went undetected). Check "
        "that the precis MCP server actually registered its tools for this "
        "host/transport (DB auth, PGPASSFILE, container health). The pass "
        "is backed off for its normal interval and will retry."
    )
    raise_alert(
        store,
        source=_tool_starved_alert_source(reviewer),
        fingerprint=fingerprint,
        title=title,
        detail=detail,
        severity="warn",
    )


def _resolve_tool_starved_alert(store: Store, reviewer: Reviewer) -> None:
    """Clear this reviewer's tool-starved alert once it uses precis again."""
    resolve_stale_alerts(
        store, source=_tool_starved_alert_source(reviewer), live_fingerprints=()
    )


def _write_failure_marker(store: Store, reviewer: Reviewer, error: str | None) -> None:
    """Record a dispatch-failure cooldown so the reviewer backs off.

    One marker per interval (the next tick dedups on it via
    :func:`_recent_failure`), so a broken reviewer attempts at its normal
    cadence (~``min_interval_hours``) rather than every tick. Tagged
    ``internal-thought`` only — NOT ``{digest_tag}`` — so it never
    masquerades as a real digest in the deep-review summary.
    """
    today = datetime.now(UTC).date().isoformat()
    msg = (error or "").strip()
    with store.tx() as conn:
        ref = store.insert_ref(
            kind="memory",
            slug=None,
            title=f"{reviewer.name} review dispatch failed {today}: {msg[:180]}",
            meta={
                f"{reviewer.meta_prefix}fail_date": today,
                f"{reviewer.meta_prefix}fail_error": msg[:500],
            },
            conn=conn,
        )
        for tag in (Tag.open(_failure_tag(reviewer)), Tag.open("internal-thought")):
            store.add_tag(ref.id, tag, set_by="system", conn=conn)


def _assemble_reviewer_blocks(reviewer: Reviewer, store: Store) -> list[Block]:
    """Assemble ``reviewer.modules`` into ordered blocks.

    The context-builder's live strings (plus ``today`` and ``digest_tag``)
    ride the :class:`AssemblyContext` ``extras``; every module reads what
    it needs from there. Factored out of :func:`_build_prompt` so
    :func:`run_review_pass` can also capture the raw block list (for
    :func:`~precis.utils.prompt.persist_assembled_context`) without
    re-running the context-builder SQL a second time.
    """
    today = datetime.now(UTC).date().isoformat()
    ctx = AssemblyContext(
        store=store,
        ref_id=0,
        model=reviewer.model,
        profile=Profile.AGENT,
        extras={
            "today": today,
            "digest_tag": reviewer.digest_tag,
            **reviewer.context_builder(store),
        },
    )
    return assemble(reviewer.modules, ctx)


def _build_prompt(reviewer: Reviewer, store: Store) -> str:
    """Assemble ``reviewer.modules`` into the single directive prompt.

    :class:`ClaudeAgentAdapter` packages the blocks — all ``VARIABLE`` on
    this path — into one user string in authored order (the ``CACHED``
    half is always empty for reviewers).
    """
    _system, user = ClaudeAgentAdapter.render(
        _assemble_reviewer_blocks(reviewer, store)
    )
    return user


def _write_digest(
    store: Store,
    reviewer: Reviewer,
    body: str,
    cost_usd: float | None,
) -> int:
    """Insert the digest as a ``kind='memory'`` ref and return its id.

    Tags applied: ``tree-review:YYYY-MM-DD`` + ``{digest_tag}`` +
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
            Tag.open(reviewer.digest_tag),
            Tag.open("user:asa"),
            Tag.open("internal-thought"),
        ):
            store.add_tag(ref.id, tag, set_by="system", conn=conn)
    return int(ref.id)


def _mcp_config_path() -> Path | None:
    """Resolve the MCP config the reviewer's ``claude -p`` should advertise.

    ``PRECIS_MCP_CONFIG`` (a host path) when set. On an agent-container host
    (``PRECIS_AGENT_CONTAINER=1`` — the spark review node, Phase-2 slice 2)
    that var is deliberately UNSET: setting it would un-gate the in-proc
    ``claude_inproc`` passes (plan_tick / fix_gripe) that self-skip there. So
    when this dispatch will *actually* containerize
    (:func:`~precis.workers.executors.agent_container.container_capability_ok`,
    the same probe :func:`~precis.utils.claude_agent.call_claude_agent` gates
    on — cached per-process so this doesn't double-probe), fall back to the
    image's baked container-internal config
    (:func:`~precis.workers.executors.agent_container.default_agent_mcp_config`),
    which the containerize seam rebases the review's ``--mcp-config`` onto. That
    path exists only *inside* the container, so it skips the host ``.exists()``
    gate. Without this the containerized reviewer runs tool-less (``mcp_config
    is None`` ⇒ no ``--mcp-config`` to rebase) and can't drill into subtrees or
    file gripes — the "MCP tools not available" snapshot digests of gripe
    171107.

    Gating on the *verified capability*, not just the opt-in, is load-bearing:
    if the host opted in but can't launch the container (runtime down / image
    absent / health-latched), ``call_claude_agent`` runs the review in-process
    — where the container-internal path does NOT exist and claude would abort
    "MCP config file not found". Returning ``None`` there keeps that fallback a
    tool-less in-proc run (unchanged from today), not a hard failure.
    """
    raw = os.environ.get("PRECIS_MCP_CONFIG")
    if raw:
        p = Path(raw)
        return p if p.exists() else None
    from precis.workers.executors.agent_container import (
        container_agent_enabled,
        container_capability_ok,
        default_agent_mcp_config,
    )

    if container_agent_enabled() and container_capability_ok():
        return Path(default_agent_mcp_config())
    return None


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
