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
  the variables the ``prompt_template`` will interpolate.
  ``prompt_template`` is an ``str.format``-style template that
  receives ``today`` plus everything the context-builder returned.

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
from precis.utils.claude_agent import (
    ClaudeAgentError,
    call_claude_agent,
)
from precis.utils.env import env_flag
from precis.utils.load_gate import skip_if_high_load
from precis.workers.runner import BatchResult

log = logging.getLogger(__name__)


# ── Reviewer config ──────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Reviewer:
    """Configuration for one reviewer instance.

    ``context_builder`` is a callable taking the store and returning a
    dict of named strings to interpolate into ``prompt_template``.
    The driver always injects ``today`` (ISO date) on top of whatever
    the builder returns.

    ``prompt_template`` uses ``str.format`` — embed the builder's keys
    as ``{name}``. The driver doesn't do any further escaping, so the
    builder is responsible for any sanitisation if the inputs are
    untrusted (today they're not — all context comes from a SQL read
    on internal corpus).
    """

    name: str
    tier_tag: str
    gate_env: str
    meta_prefix: str
    model: str
    max_turns: int
    timeout_s: float
    min_interval_hours: float
    context_builder: Callable[[Store], dict[str, str]]
    prompt_template: str


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
    try:
        result = call_claude_agent(
            prompt,
            model=os.environ.get(
                f"PRECIS_{reviewer.name.upper()}_MODEL", reviewer.model
            ),
            mcp_config=_mcp_config_path(),
            max_turns=reviewer.max_turns,
            timeout_s=reviewer.timeout_s,
            # Stream-json gets us cost/turns from the result event;
            # call_claude_agent unwraps the digest text from the
            # ``result`` field so the digest writer sees plain text.
            output_format="stream-json",
            extra_args=("--verbose",),
        )
    except ClaudeAgentError as exc:
        log.exception("review[%s]: claude agent failed: %s", reviewer.name, exc)
        return BatchResult(handler=reviewer.name, claimed=1, ok=0, failed=1)
    digest_id = _write_digest(store, reviewer, result.final_text, result.cost_usd)
    log.info(
        "review[%s]: wrote digest memory id=%d cost=$%.4f duration=%.1fs",
        reviewer.name,
        digest_id,
        result.cost_usd or 0.0,
        result.duration_s,
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
    """Format ``reviewer.prompt_template`` with the builder's keys + today."""
    today = datetime.now(UTC).date().isoformat()
    ctx = reviewer.context_builder(store)
    return reviewer.prompt_template.format(today=today, **ctx)


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
