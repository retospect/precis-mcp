"""Dream-pass worker — `claude_agent` shape.

Replaces the bash `dream-pass.sh` script that lives in
`cluster/roles/precis_dream/files/`. Same dispatch payload (claude -p
with SOUL.md as system prompt + MCP precis config + bypass
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
* ``PRECIS_DREAM_SOUL_PATH`` — file containing the agent's system
  prompt (`--append-system-prompt`). This is the **persona** layer (for
  asa, her SOUL.md) — kept out of the packaged workflow prompt so the
  workflow stays generic.
* ``PRECIS_MCP_CONFIG`` — MCP config JSON the agent uses to call
  precis tools.
* ``PRECIS_DREAM_LENS`` — the oracle lens (comma-list) biasing the
  per-cycle persona stance. Default ``sci`` (50% scientists / 50%
  evenly across the other traditions; see ``utils/oracle_lens.py``).
* ``PRECIS_DREAM_PROCESS_PROB`` — fraction of cycles that hold a
  multi-phase PROCESS lens (Disney) instead of a single-stance persona.
  Default 0.15.

Gating: ``PRECIS_DREAM_AGENT=1`` (env). The pass is explicit-only
on the CLI (``--only dream_agent``) AND env-gated, mirroring the
existing dream worker's discipline.

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

from precis.store import Store
from precis.utils import handle_registry
from precis.utils.dream_seed import load_lenses, render_lens_block
from precis.utils.env import env_flag
from precis.utils.llm.router import LlmRequest, Tier, dispatch, resolve_model
from precis.utils.load_gate import skip_if_high_load
from precis.utils.oracle_lens import draw_lens_entry, render_lens_block_from_draw
from precis.utils.working_set_render import render_working_set
from precis.workers.runner import BatchResult
from precis.workers.working_set import Provenance, WorkingSet

# The dream's default lens: bias the persona draw toward the scientist
# traditions (50% science / 50% evenly across the rest — see
# utils/oracle_lens.py). Comma-list to widen (e.g. "sci,art").
_DEFAULT_DREAM_LENS = "sci"

# Fraction of cycles that hold a multi-phase PROCESS lens (Disney) instead
# of a single-stance persona. The rest draw a persona from the oracle.
_DEFAULT_PROCESS_LENS_PROB = 0.15

log = logging.getLogger(__name__)


# Default model: the router's cloud-super tier (opus-4.8). The dream
# pass moved onto the consolidated cloud reasoning tier (ADR 0046 unit
# 4b) — "if it's worth thinking about, think well": opus-4.8 is where
# the speculative-connection work earns the stronger model, at the same
# price as 4-7. Override with PRECIS_DREAM_AGENT_MODEL for a per-pass pin.
def _default_model() -> str:
    return resolve_model(Tier.CLOUD_SUPER)


# Same turn cap as the bash script's --max-turns 20.
_DEFAULT_MAX_TURNS = 20

# Same wall-clock window as structural/deep — agents that need
# longer can bump per call. The bash had no timeout; the helper's
# 10-min default is the conservative upgrade.
_DEFAULT_TIMEOUT_S = 600


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
    if skip_if_high_load("dream_agent"):
        return BatchResult(handler="dream_agent", claimed=0, ok=0, failed=0)
    soul_path = _env_path("PRECIS_DREAM_SOUL_PATH")
    mcp_path = _env_path("PRECIS_MCP_CONFIG")
    prompt = _load_prompt()
    if prompt is None:
        log.error(
            "dream_agent: no dream prompt available (override + packaged both failed); skipping"
        )
        return BatchResult(handler="dream_agent", claimed=0, ok=0, failed=0)
    prompt = _apply_lens(prompt, store)
    prompt = _apply_fisheye(prompt, store)
    # Routed through the LLM seam (ADR 0046 unit 4b): CLOUD_SUPER + tools,
    # so ``PRECIS_LLM_BACKEND`` can move the whole dream pass onto an OSS
    # model. ``model=`` keeps the per-pass ``PRECIS_DREAM_AGENT_MODEL`` pin
    # (None ⇒ the tier default). Errors fold into ``res.error``.
    res = dispatch(
        LlmRequest(
            tier=Tier.CLOUD_SUPER,
            prompt=prompt,
            tools_needed=True,
            model=os.environ.get("PRECIS_DREAM_AGENT_MODEL"),
            system_prompt=soul_path,
            mcp_config=mcp_path,
            max_turns=_DEFAULT_MAX_TURNS,
            timeout_s=_DEFAULT_TIMEOUT_S,
            # Dreams don't fan out to the open web — keep them on
            # corpus state. Same as the bash script's flag set.
            disallowed_tools=("WebFetch", "WebSearch"),
            # Stream-json gets us cost/turns from the result event.
            output_format="stream-json",
            extra_args=("--verbose",),
        )
    )
    if res.error:
        log.error("dream_agent: claude agent failed: %s", res.error)
        return BatchResult(handler="dream_agent", claimed=1, ok=0, failed=1)
    log.info(
        "dream_agent: dispatch ok cost=$%.4f duration=%.1fs turns=%s final_text_len=%d",
        res.cost_usd or 0.0,
        res.duration_s or 0.0,
        res.turns_used,
        len(res.text or ""),
    )
    _ = store  # reserved for future event-log writes
    return BatchResult(handler="dream_agent", claimed=1, ok=1, failed=0)


# ── helpers ────────────────────────────────────────────────────────


def _gate_enabled() -> bool:
    return env_flag("PRECIS_DREAM_AGENT")


#: Packaged dreaming workflow — the SSOT prompt, persona-neutral. The
#: operator's deploy no longer has to ship one; `PRECIS_DREAM_PROMPT_PATH`
#: is now an optional override, and the persona lives in the system prompt
#: (``PRECIS_DREAM_SOUL_PATH``), not here.
_PACKAGED_PROMPT = "precis.data.prompts"
_PACKAGED_PROMPT_FILE = "dream-prompt.md"


def _load_prompt() -> str | None:
    """The dream directive prompt: the ``PRECIS_DREAM_PROMPT_PATH``
    override if set+readable, else the packaged default. ``None`` only if
    both are unavailable (the packaged resource should always exist)."""
    override = _env_path("PRECIS_DREAM_PROMPT_PATH")
    if override is not None:
        return override.read_text()
    try:
        from importlib import resources

        return (
            resources.files(_PACKAGED_PROMPT)
            .joinpath(_PACKAGED_PROMPT_FILE)
            .read_text(encoding="utf-8")
        )
    except (FileNotFoundError, ModuleNotFoundError, OSError):
        log.exception("dream_agent: packaged dream prompt unreadable")
        return None


def _apply_lens(prompt: str, store: Store) -> str:
    """Prepend this cycle's lens block to the dream directive.

    Best-effort: any failure leaves the prompt unchanged, so a missing
    oracle corpus or seed file never fails the pass.
    """
    block = _select_lens_block(store)
    if block is None:
        return prompt
    return block + "\n" + prompt


#: The dream's fisheye eye-draw (ADR 0051): a **kind-diverse** sample of fresh
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
    """The most-recently-touched live refs of ``kind`` (the recency draw)."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id FROM refs WHERE kind = %s AND deleted_at IS NULL "
            "ORDER BY updated_at DESC LIMIT %s",
            (kind, limit),
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
    return ws


def _apply_fisheye(prompt: str, store: Store) -> str:
    """Append a fisheye working-set of fresh, kind-diverse material (memories +
    papers + patents) for the dream to connect (ADR 0051).

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
        "A kind-diverse draw of recent memories, papers and patents — look for "
        "connections across them.\n\n" + block
    )


def _select_lens_block(store: Store) -> str | None:
    """This cycle's lens: usually a persona drawn from the oracle under
    the ``sci`` lens (50% scientists / 50% evenly across the rest), and
    occasionally a multi-phase PROCESS lens (Disney) instead.

    Returns the rendered ``## This cycle's lens`` block, or ``None`` to
    run unlensed.
    """
    # Occasionally hold a sequential process instead of a single stance.
    if _coin(_process_lens_prob()):
        processes = load_lenses()
        if processes:
            lens = processes[secrets.randbelow(len(processes))]
            log.info("dream_agent: lens=process:%s", lens.get("id"))
            return render_lens_block(lens)

    # Default: draw a persona stance from the oracle under the dream lens.
    try:
        draw = draw_lens_entry(store, _dream_lens_names())
    except Exception:
        log.exception("dream_agent: oracle lens draw failed; running unlensed")
        return None
    if draw is None:
        log.info("dream_agent: no oracle traditions loaded; running unlensed")
        return None
    log.info("dream_agent: lens=oracle:%s~%s", draw.ref.slug, draw.block.pos)
    return render_lens_block_from_draw(draw)


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


__all__ = ["run_dream_pass"]
