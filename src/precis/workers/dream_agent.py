"""Dream-pass worker — `claude_agent` shape.

Replaces the bash `dream-pass.sh` script that lives in
`cluster/roles/precis_dream/files/`. Same dispatch payload (claude -p
with a persona as system prompt + MCP precis config + bypass
permissions + WebFetch/WebSearch disabled), but lifted into the
unified :func:`precis.utils.claude_agent.call_claude_agent` so:

* cost / timeout / turn caps are uniform with the structural and
  deep reviewers,
* the helper's `log_event` hook attributes the run on
  ``ref_events`` (per-host telemetry),
* the cluster-side bash script collapses to a one-liner that just
  shells out to `precis worker --only dream_agent --once`.

Inputs (env):

* ``PRECIS_DREAM_PROMPT_PATH`` — optional override file containing the
  directive prompt. When unset (or unreadable), the worker falls back to
  the **packaged** dreaming workflow at
  ``precis/data/prompts/dream-prompt.md`` — the persona-neutral SSOT, so
  the prompt no longer has to be shipped by the operator's deploy. Set
  this only to override the default with a site-specific prompt.
* ``PRECIS_DREAM_SOUL_PATH`` — optional override for the agent's system
  prompt (`--append-system-prompt`). Unset (the normal case) falls back to
  the **packaged** dreamer persona at
  ``precis/data/prompts/dream-persona.md``. This used to point at the
  operator's own chat persona (asa's ``SOUL.md``), which is written for a
  Discord/Slack turn loop and says nothing about synthesis over a corpus;
  it is now a site override, not a prerequisite.
* ``PRECIS_MCP_CONFIG`` — MCP config JSON the agent uses to call
  precis tools.
* ``PRECIS_DREAM_LENS`` — the oracle lens (comma-list) biasing the
  per-cycle persona stance. Default ``sci`` (50% scientists / 50%
  evenly across the other traditions; see ``utils/oracle_lens.py``).
* ``PRECIS_DREAM_PROCESS_PROB`` — fraction of cycles that hold a
  multi-phase PROCESS lens (Disney) instead of a single-stance persona.
  Default 0.15.
* ``PRECIS_DREAM_QUEST_ANCHOR`` — default-ON nudge that names a randomly
  chosen active quest and asks the dream to seed one of its two anchors
  off it, keeping the other free-roaming. ``=0`` disables it.
* ``PRECIS_DREAM_QUEST_ANGLE`` — the ``angle=`` value suggested for that
  quest-seeded search. Default 0.5.

Gating: ``PRECIS_DREAM_AGENT=1`` (env). The pass is explicit-only
on the CLI (``--only dream_agent``) AND env-gated, mirroring the
existing dream worker's discipline.

Cadence (§A): folded onto the decentralized ``scheduler`` worker pass
(:mod:`precis.workers.scheduler`) as the ``dream_agent`` cadence —
host-pinned to melchior, its ``resolve_interval`` reading
:func:`precis.workers.dream_throttle.resolve_min_interval_minutes` (DB >
env > compiled 15) directly, so the cadence knob IS the live interval
rather than a fixed launchd timer plus a throttle underneath it. The old
standalone hermes-pinned 15-min LaunchDaemon (``dream-pass.sh``) is
retired; :func:`eligible` is the cadence's local gate. §G's in-pass
:func:`precis.workers.dream_throttle.skip_if_too_soon` guard stays as a
belt-and-suspenders check (and still guards a manual ``--only
dream_agent`` run).

Output disposition: **the dream agent writes its own memories**
via the precis MCP `put` tool during the session. The worker
itself does not write a digest — that would duplicate the agentic
side effects. Successful dispatch is logged; the audit text is
not stored as a separate memory.
"""

from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path
from typing import Any

from precis.handlers._patent_ingest import FAMILY_STUB_META_KEY
from precis.store import Store
from precis.utils import handle_registry
from precis.utils.dream_seed import load_lenses, render_lens_block
from precis.utils.env import env_flag
from precis.utils.llm.router import LlmRequest, LlmResult, Tier, dispatch, resolve_model
from precis.utils.load_gate import skip_if_high_load
from precis.utils.oracle_lens import draw_lens_entry, render_lens_block_from_draw
from precis.utils.working_set_render import render_working_set
from precis.workers import dream_throttle
from precis.workers.runner import BatchResult
from precis.workers.working_set import Provenance, WorkingSet

# The dream's default lens: bias the persona draw toward the scientist
# traditions (50% science / 50% evenly across the rest — see
# utils/oracle_lens.py). Comma-list to widen (e.g. "sci,art").
_DEFAULT_DREAM_LENS = "sci"

# Fraction of cycles that hold a multi-phase PROCESS lens (Disney) instead
# of a single-stance persona. The rest draw a persona from the oracle.
_DEFAULT_PROCESS_LENS_PROB = 0.15

# The angle= suggested for the quest-anchor nudge's seeded search: adjacent-
# to the quest, not on-the-nose (angle=0 would just be its nearest matches).
_DEFAULT_QUEST_ANGLE = 0.5

log = logging.getLogger(__name__)


# Default model: the router's BIG tier — local-first (qwen3-235b on the
# spark pair) with an OSS cloud fallback, per ``llm.chain.big``.
#
# This was FRONTIER (opus-4.8) on the "if it's worth thinking about, think
# well" argument. The bill retired that argument: 30 days of hourly dreaming
# cost $1,313.77 — over 4x the plan_tick runaway we treated as an incident —
# because FRONTIER has no ``llm.chain.frontier`` row and falls through to the
# compiled opus default. A speculative-association pass is exactly the work
# that can wait for local hardware: nothing downstream blocks on a dream, so
# latency is free and the tokens are not. Override with
# PRECIS_DREAM_AGENT_MODEL for a per-pass pin.
def _default_model() -> str:
    return resolve_model(Tier.BIG)


# Same turn cap as the bash script's --max-turns 20.
_DEFAULT_MAX_TURNS = 20

# Same wall-clock window as structural/deep — agents that need
# longer can bump per call. The bash had no timeout; the helper's
# 10-min default is the conservative upgrade.
_DEFAULT_TIMEOUT_S = 600

# Tier-1 tool deny for the dream pass (gr179501). Dream writes NEW
# memories (``mcp__precis__put`` stays) and promotes its own verified
# memory to ``tier:synthetic-insight`` (dream-prompt.md Step 7 —
# ``mcp__precis__tag`` stays, a needed cooperative-tier residual the
# tool-level deny can't scope by kind). It never edits/deletes/links refs,
# so those are denied: its fisheye draws recent paper/patent summaries into
# the prompt unvetted, and a crafted summary must not be able to steer it
# into ``delete``/``edit``/``link`` of arbitrary refs. Web stays off too
# (dreams run on corpus state; the precis ``web``/``websearch`` kinds go via
# ``get``/``search``, not the built-in WebFetch/WebSearch). Explicit here
# rather than via the opt-in envelope these standing passes never set.
_DREAM_DISALLOWED_TOOLS: tuple[str, ...] = (
    "WebFetch",
    "WebSearch",
    "mcp__precis__edit",
    "mcp__precis__delete",
    "mcp__precis__link",
)


def run_dream_pass(store: Store) -> BatchResult:
    """One dream cycle. Counters:

    * ``claimed`` = 1 if we ran the LLM, 0 if gated / mis-configured
    * ``ok`` = 1 on a clean dispatch (the agent's memory writes
      happen via MCP and aren't double-counted here)
    * ``failed`` = 1 if the helper raised :class:`ClaudeAgentError`
    """
    if not _gate_enabled():
        log.info("dream_agent: PRECIS_DREAM_AGENT not set; skipping")
        return BatchResult(handler="dream_agent", claimed=0, ok=0, failed=0)
    if dream_throttle.skip_if_too_soon(store):
        return BatchResult(handler="dream_agent", claimed=0, ok=0, failed=0)
    if skip_if_high_load("dream_agent"):
        return BatchResult(handler="dream_agent", claimed=0, ok=0, failed=0)
    persona = _load_persona()
    mcp_path = _env_path("PRECIS_MCP_CONFIG")
    prompt = _load_prompt()
    if prompt is None:
        log.error(
            "dream_agent: no dream prompt available (override + packaged both failed); skipping"
        )
        return BatchResult(handler="dream_agent", claimed=0, ok=0, failed=0)
    prompt, lens_id = _apply_lens(prompt, store)
    prompt = _apply_fisheye(prompt, store)
    prompt, quest_anchor = _apply_quest_anchor(prompt, store)

    # Mint a durable per-tick provenance node (kind='agentlog') carrying the
    # full assembled prompt + lens/quest metadata, mirroring plan_tick
    # (job_types/plan_tick.py). Its id threads onto the subprocess env below
    # so every chunk the dream writes (a websearch fetch, a memory create)
    # attributes back to this run via `touch_from_env` (a `touched` link) —
    # this is Slice B's "whodunnit" fix. Best-effort: a provenance failure
    # must never sink the dream pass.
    from precis import agentlog

    model_pin = os.environ.get("PRECIS_DREAM_AGENT_MODEL")
    log_id: int | None = None
    try:
        log_id = agentlog.open_log(
            store,
            source="dream",
            title="dream tick",
            model=model_pin,
            prompt=prompt,
            meta_extra={"lens": lens_id, "quest_anchor": quest_anchor},
        )
    except Exception:
        log.warning("dream_agent: failed to open agentlog", exc_info=True)
    env_overlay = {agentlog.ENV_VAR: str(log_id)} if log_id is not None else None

    # Reliable provenance edge — rides the write-time auto-mention linkifier
    # (confirmed live), NOT the env-scoped `touched` path above (which depends
    # on PRECIS_CURRENT_AGENTLOG reaching the MCP server and is currently
    # dormant cluster-wide). Tell the dream to cite this tick's handle in each
    # memory, so a `memory --related-to--> agentlog` edge forms and the node
    # links to everything the dream connected (its websearches are reachable
    # one hop further via the memory's own citations). `agentlog` is on
    # LINKIFY_KINDS so the token resolves. No-op when the node failed to open.
    # (meta.prompt above captures the pre-footer prompt — the substance; this
    # provenance footer is deliberately not folded back in.)
    if log_id is not None:
        prompt += (
            f"\n\n## This tick's provenance handle\n\n"
            f"This dream cycle is recorded as `agentlog:{log_id}`. In EACH memory "
            f"you write this cycle, include the token `agentlog:{log_id}` inline "
            f"(alongside your other cited handles) so the memory — and through it "
            f"the websearches/papers it draws on — links back to the run that "
            f"produced it."
        )

    # Committing to a real dispatch — stamp the throttle's last-real-run
    # timestamp now (not on any of the no-op returns above), so the next
    # tick's ``skip_if_too_soon`` measures from an actual reconsolidation
    # attempt. Best-effort: a settings-write hiccup must never sink the pass.
    try:
        dream_throttle.mark_real_run(store)
    except Exception:
        log.warning("dream_agent: failed to mark last-real-run", exc_info=True)

    # Routed through the LLM seam: BIG + tools, so the
    # operator-authored ``llm.chain.big`` (local qwen3-235b → OSS cloud)
    # carries it. ``model=`` keeps the per-pass ``PRECIS_DREAM_AGENT_MODEL``
    # pin (None ⇒ the tier default). Errors fold into ``res.error``.
    res = dispatch(
        LlmRequest(
            tier=Tier.BIG,
            source="dream",
            prompt=prompt,
            tools_needed=True,
            model=model_pin,
            system_prompt=persona,
            mcp_config=mcp_path,
            max_turns=_DEFAULT_MAX_TURNS,
            timeout_s=_DEFAULT_TIMEOUT_S,
            # Dreams don't fan out to the open web and never mutate
            # existing refs — put-only for the corpus (gr179501).
            disallowed_tools=_DREAM_DISALLOWED_TOOLS,
            # Stream-json gets us cost/turns from the result event.
            output_format="stream-json",
            extra_args=("--verbose",),
            env_overlay=env_overlay,
        )
    )

    def _finalize(status: str) -> None:
        if log_id is None:
            return
        try:
            agentlog.finalize_log(
                store,
                log_id=log_id,
                status=status,
                meta_extra=_result_meta(res),
            )
        except Exception:
            log.warning("dream_agent: failed to finalize agentlog", exc_info=True)

    if res.error:
        log.error("dream_agent: claude agent failed: %s", res.error)
        _finalize(res.terminal_reason or "error")
        return BatchResult(handler="dream_agent", claimed=1, ok=0, failed=1)
    log.info(
        "dream_agent: dispatch ok cost=$%.4f duration=%.1fs turns=%s final_text_len=%d",
        res.cost_usd or 0.0,
        res.duration_s or 0.0,
        res.turns_used,
        len(res.text or ""),
    )
    _finalize("ok")
    return BatchResult(handler="dream_agent", claimed=1, ok=1, failed=0)


def _result_meta(res: LlmResult) -> dict[str, Any]:
    """The tick-end telemetry stamped onto the agentlog at finalize —
    ``LlmResult``'s cost / turn / token counters, so a dream tick's
    provenance node carries the same numbers the log line does today."""
    return {
        "cost_usd": res.cost_usd,
        "turns": res.turns_used,
        "duration_s": res.duration_s,
        "input_tokens": res.input_tokens,
        "output_tokens": res.output_tokens,
        "cache_read_tokens": res.cache_read_tokens,
        "cache_creation_tokens": res.cache_creation_tokens,
        "terminal_reason": res.terminal_reason,
    }


# ── helpers ────────────────────────────────────────────────────────


def _gate_enabled() -> bool:
    return env_flag("PRECIS_DREAM_AGENT")


def eligible() -> bool:
    """§A cheap local-process gate for the ``dream_agent`` scheduler cadence
    (``workers/scheduler.py``) — checked BEFORE a claim attempt, same test
    ``run_dream_pass`` itself re-checks (belt-and-suspenders). Mirrors
    ``dream-pass.sh``'s missing-SOUL skip: ``PRECIS_DREAM_AGENT`` truthy AND
    the process actually carries the agent profile. Without this, a worker
    on the *pinned* host that lacks the dream env (e.g. its system-profile
    process, as opposed to the agent-profile one that carries OAuth + the
    MCP config) would win the lease and burn the fire for nothing.

    The capability marker is ``PRECIS_MCP_CONFIG``, not the old soul path:
    a dream's entire deliverable is precis tool calls, so a process that
    can't reach MCP can do nothing whatever it is handed as a persona. The
    persona itself is now packaged (:func:`_load_persona`) and so is never
    a reason to skip."""
    return _gate_enabled() and _env_path("PRECIS_MCP_CONFIG") is not None


#: Packaged dreaming workflow — the SSOT prompt, persona-neutral. The
#: operator's deploy no longer has to ship one; `PRECIS_DREAM_PROMPT_PATH`
#: is now an optional override.
_PACKAGED_PROMPT = "precis.data.prompts"
_PACKAGED_PROMPT_FILE = "dream-prompt.md"

#: Packaged dreamer persona — the ``--append-system-prompt`` layer. Was
#: the operator's own chat persona (asa's ``SOUL.md``, via
#: ``PRECIS_DREAM_SOUL_PATH``), which is written for a Discord/Slack turn
#: loop and says nothing about synthesis over a corpus. The packaged file
#: is the default; the env var stays as an optional site override, exactly
#: like ``PRECIS_DREAM_PROMPT_PATH``.
_PACKAGED_PERSONA_FILE = "dream-persona.md"


def _load_packaged(filename: str, *, what: str) -> str | None:
    """Read a packaged prompt resource; ``None`` (logged) if unreadable."""
    try:
        from importlib import resources

        return (
            resources.files(_PACKAGED_PROMPT)
            .joinpath(filename)
            .read_text(encoding="utf-8")
        )
    except (FileNotFoundError, ModuleNotFoundError, OSError):
        log.exception("dream_agent: packaged %s unreadable", what)
        return None


def _load_prompt() -> str | None:
    """The dream directive prompt: the ``PRECIS_DREAM_PROMPT_PATH``
    override if set+readable, else the packaged default. ``None`` only if
    both are unavailable (the packaged resource should always exist)."""
    override = _env_path("PRECIS_DREAM_PROMPT_PATH")
    if override is not None:
        return override.read_text(encoding="utf-8")
    return _load_packaged(_PACKAGED_PROMPT_FILE, what="dream prompt")


def _load_persona() -> str | None:
    """The dreamer's system prompt: the ``PRECIS_DREAM_SOUL_PATH`` override
    if set+readable, else the packaged persona-neutral default.

    Returns the prompt *text*, not a path — ``LlmRequest.system_prompt``
    takes ``str | Path``, so a packaged resource passes through as a
    literal without needing a file on disk."""
    override = _env_path("PRECIS_DREAM_SOUL_PATH")
    if override is not None:
        return override.read_text(encoding="utf-8")
    return _load_packaged(_PACKAGED_PERSONA_FILE, what="dream persona")


def _apply_lens(prompt: str, store: Store) -> tuple[str, str | None]:
    """Prepend this cycle's lens block to the dream directive.

    Best-effort: any failure leaves the prompt unchanged, so a missing
    oracle corpus or seed file never fails the pass. Returns
    ``(prompt, lens_id)`` — the lens id (``process:<id>`` /
    ``oracle:<slug>~<pos>``) rides along so the tick's agentlog node
    (Slice B) can record which lens ran, without re-drawing it.
    """
    selected = _select_lens_block(store)
    if selected is None:
        return prompt, None
    block, lens_id = selected
    return block + "\n" + prompt, lens_id


#: The dream's fisheye eye-draw: a **kind-diverse** sample of fresh
#: refs given to the dream as its working set — cross-pollination fuel, patents
#: included (Reto). ``(kind, extent, count)``. Memories at ``fisheye+1hop`` so
#: their link neighbourhood (the connections a dream feeds on) rides along.
_DREAM_EYE_KINDS: tuple[tuple[str, str, int], ...] = (
    ("memory", "fisheye+1hop", 3),
    ("paper", "summary", 2),
    ("patent", "summary", 1),
)


def _dream_fisheye_enabled() -> bool:
    """Default-ON; ``PRECIS_DREAM_FISHEYE=0`` disables the eye-draw without a
    redeploy."""
    return os.environ.get("PRECIS_DREAM_FISHEYE", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def _recent_ref_ids(store: Store, kind: str, limit: int) -> list[int]:
    """The most-recently-touched live refs of ``kind`` (the recency draw).

    Over-fetches (3x) and drops family-stub refs (``meta[FAMILY_STUB_META_KEY]``
    truthy) so a stub landing in the window doesn't shrink the draw below
    ``limit`` — a family stub is biblio-only by design (no body chunks), so
    drawing one would waste the dream's one patent slot on a near-empty
    summary. Only patents carry the key; harmless no-op for other kinds.
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id, meta FROM refs WHERE kind = %s AND deleted_at IS NULL "
            "ORDER BY updated_at DESC LIMIT %s",
            (kind, limit * 3),
        ).fetchall()
    ids: list[int] = []
    for ref_id, meta in rows:
        if isinstance(meta, dict) and meta.get(FAMILY_STUB_META_KEY):
            continue
        ids.append(int(ref_id))
        if len(ids) >= limit:
            break
    return ids


def _draft_cite_eye_count() -> int:
    """How many draft-cited papers to draw into the fisheye (0 disables).

    Env ``PRECIS_DREAM_DRAFT_CITE_EYES`` (default 2)."""
    try:
        return max(0, int(os.environ.get("PRECIS_DREAM_DRAFT_CITE_EYES", "2")))
    except ValueError:
        return 2


def _recent_draft_cited_paper_ids(store: Store, limit: int) -> list[int]:
    """Papers cited by the most-recently-touched live drafts.

    The draft handler auto-materialises ``cites`` edges from ``[pc<id>]``
    handles in the prose, so this rides an existing graph — no new plumbing.
    Feeding these into the dream fisheye lets a wandering re-read of the draft
    you are *actively* writing spot a paragraph that drifted from its own cited
    evidence (``docs/backlog/dreaming.md`` names exactly this as the payoff).
    Ordered by the most-recent citing draft; deleted papers excluded."""
    if limit <= 0:
        return []
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT l.dst_ref_id FROM refs d "
            "JOIN links l ON l.src_ref_id = d.ref_id AND l.relation = 'cites' "
            "JOIN refs p ON p.ref_id = l.dst_ref_id AND p.deleted_at IS NULL "
            "WHERE d.kind = 'draft' AND d.deleted_at IS NULL "
            "GROUP BY l.dst_ref_id "
            "ORDER BY max(d.updated_at) DESC LIMIT %s",
            (limit,),
        ).fetchall()
    return [int(r[0]) for r in rows]


def _draw_dream_eyes(store: Store) -> WorkingSet:
    """Place a kind-diverse set of fresh eyes for this cycle."""
    ws = WorkingSet()
    for kind, extent, count in _DREAM_EYE_KINDS:
        for rid in _recent_ref_ids(store, kind, count):
            ws.focus(
                handle_registry.format_handle(kind, rid),
                extent,
                provenance=Provenance.INFERRED,  # an auto-lens the system offered
            )
    # Papers cited by recently-active drafts — the draft's own evidence rides
    # along so the dreamer can catch drift/contradiction against a cited source.
    # (A cited paper already drawn by the recency pass collapses — eyes is a
    # dict keyed by handle.)
    for rid in _recent_draft_cited_paper_ids(store, _draft_cite_eye_count()):
        ws.focus(
            handle_registry.format_handle("paper", rid),
            "summary",
            provenance=Provenance.INFERRED,
        )
    return ws


def _apply_fisheye(prompt: str, store: Store) -> str:
    """Append a fisheye working-set of fresh, kind-diverse material (memories +
    papers + patents) for the dream to connect.

    Best-effort + flag-gated: default-ON, and any failure (or an empty draw)
    leaves the prompt unchanged — the eye-draw can never fail a dream pass."""
    if not _dream_fisheye_enabled():
        return prompt
    try:
        ws = _draw_dream_eyes(store)
        if not ws.eyes:
            return prompt
        block = render_working_set(store, ws)
    except Exception:
        log.exception("dream_agent: fisheye eye-draw failed; dreaming without it")
        return prompt
    if not block.strip() or block == "— empty working set —":
        return prompt
    return (
        f"{prompt}\n\n## Fresh material to dream over (fisheye)\n\n"
        "A kind-diverse draw of recent memories, papers and patents — plus "
        "papers your active drafts cite (watch for drift or contradiction "
        "between a draft and its evidence). Look for connections across them."
        "\n\n" + block
    )


def _dream_quest_anchor_enabled() -> bool:
    """Default-ON; ``PRECIS_DREAM_QUEST_ANCHOR=0`` disables the quest nudge
    without a redeploy."""
    return os.environ.get("PRECIS_DREAM_QUEST_ANCHOR", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def _dream_quest_angle() -> float:
    """The ``angle=`` value suggested for the quest-seeded anchor —
    ``PRECIS_DREAM_QUEST_ANGLE`` (default 0.5). Unset or a bad value falls
    back to the default."""
    raw = os.environ.get("PRECIS_DREAM_QUEST_ANGLE")
    if raw is None:
        return _DEFAULT_QUEST_ANGLE
    try:
        return float(raw)
    except ValueError:
        return _DEFAULT_QUEST_ANGLE


def _apply_quest_anchor(prompt: str, store: Store) -> tuple[str, str | None]:
    """Append a nudge naming a randomly-chosen active quest, asking the
    dream to seed ONE of its two anchors off it while keeping the other
    free-roaming (the "let's try 2a" quest-nudge).

    Best-effort + flag-gated: default-ON, and any failure (or no active
    quests) leaves the prompt unchanged — a dormant quest board never
    starves the dream of its usual free sampling. Returns
    ``(prompt, quest_handle)`` — the chosen quest's ``quest:<id>`` handle
    (or ``None`` when no anchor was applied), threaded out so the tick's
    agentlog node (Slice B) can record which quest it nudged toward
    without a second, re-randomizing draw."""
    if not _dream_quest_anchor_enabled():
        return prompt, None
    try:
        from precis.quest.allocator import active_quest_ids

        ids = active_quest_ids(store)
        if not ids:
            return prompt, None
        qid = ids[secrets.randbelow(len(ids))]
        ref = store.get_ref(kind="quest", id=qid)
        if ref is None:
            return prompt, None
        angle = _dream_quest_angle()
    except Exception:
        log.exception("dream_agent: quest-anchor draw failed; dreaming without it")
        return prompt, None
    log.info("dream_agent: quest_anchor=quest:%s~%s", ref.id, ref.slug)
    quest_handle = f"quest:{ref.id}"
    return (
        f"{prompt}\n\n"
        f"## This cycle's quest anchor: {ref.title}  (quest:{ref.id})\n\n"
        "Make ONE of your two anchors (Step 2 or Step 4) a diverse-cone sample "
        f'seeded off this quest:  search(like="quest:{ref.id}", angle={angle}, n=8)\n'
        'If that errors "has no embedding yet", fall back to:\n'
        f'  search(q="{ref.title}", angle={angle}, n=8)\n'
        "Keep your OTHER anchor FREE-ROAMING — do NOT quest-lock both. The wild,\n"
        "cross-domain leg is required; this is a nudge toward the quest, not a fence.",
        quest_handle,
    )


def _select_lens_block(store: Store) -> tuple[str, str] | None:
    """This cycle's lens: usually a persona drawn from the oracle under
    the ``sci`` lens (50% scientists / 50% evenly across the rest), and
    occasionally a multi-phase PROCESS lens (Disney) instead.

    Returns ``(rendered ## This cycle's lens block, lens_id)``, or ``None``
    to run unlensed. ``lens_id`` (``process:<id>`` / ``oracle:<slug>~<pos>``)
    is the same string logged below, threaded out so the tick's agentlog
    node (Slice B) can record it without a second, re-randomizing draw.
    """
    # Occasionally hold a sequential process instead of a single stance.
    if _coin(_process_lens_prob()):
        processes = load_lenses()
        if processes:
            lens = processes[secrets.randbelow(len(processes))]
            lens_id = f"process:{lens.get('id')}"
            log.info("dream_agent: lens=%s", lens_id)
            return render_lens_block(lens), lens_id

    # Default: draw a persona stance from the oracle under the dream lens.
    try:
        draw = draw_lens_entry(store, _dream_lens_names())
    except Exception:
        log.exception("dream_agent: oracle lens draw failed; running unlensed")
        return None
    if draw is None:
        log.info("dream_agent: no oracle traditions loaded; running unlensed")
        return None
    lens_id = f"oracle:{draw.ref.slug}~{draw.block.pos}"
    log.info("dream_agent: lens=%s", lens_id)
    return render_lens_block_from_draw(draw), lens_id


def _dream_lens_names() -> list[str]:
    """The lens name(s) for the persona draw — ``PRECIS_DREAM_LENS`` (a
    comma-list) or the ``sci`` default."""
    raw = os.environ.get("PRECIS_DREAM_LENS", _DEFAULT_DREAM_LENS)
    names = [s.strip() for s in raw.split(",") if s.strip()]
    return names or [_DEFAULT_DREAM_LENS]


def _process_lens_prob() -> float:
    """Fraction of cycles that run a PROCESS lens — ``PRECIS_DREAM_PROCESS_PROB``
    (default 0.15). Unset or a bad value falls back to the default."""
    raw = os.environ.get("PRECIS_DREAM_PROCESS_PROB")
    if raw is None:
        return _DEFAULT_PROCESS_LENS_PROB
    try:
        return float(raw)
    except ValueError:
        return _DEFAULT_PROCESS_LENS_PROB


def _coin(p: float) -> bool:
    """True with probability ``p`` (CSPRNG)."""
    if p <= 0.0:
        return False
    if p >= 1.0:
        return True
    return secrets.randbelow(10**9) / 10**9 < p


def _env_path(var: str) -> Path | None:
    """Resolve env var → :class:`Path` if the file exists; else ``None``."""
    raw = os.environ.get(var)
    if not raw:
        return None
    p = Path(raw)
    return p if p.exists() else None


__all__ = ["eligible", "run_dream_pass"]
