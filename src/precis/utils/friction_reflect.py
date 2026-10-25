"""End-of-run tool-friction reflection (Part A of
the friction-reflection design; git holds the full plan).

Every *eligible* agentic ``claude -p`` run can be asked, at the very
end, whether the MCP surface fought it — and to file one grounded
``kind='gripe'`` if so. This captures *soft* friction (the tool
succeeded but was clumsy: no verb for what you wanted, N calls where
one should have worked, a result shape that forced a re-query) — the
signal that never throws an ``[error:*]`` and so is invisible to
transcript mining.

The mechanism is a system-prompt footer appended at the
:func:`precis.utils.claude_agent.call_claude_agent` chokepoint. The
gripe itself is filed *by the running agent* via MCP ``put`` — there is
no programmatic raise path for gripes (unlike alerts), so the agent
that felt the friction is the surrogate raiser.

**Default-OFF for write-capable runs.** Once on, the footer rides
*every* agentic run (reviewers, planner ticks, dream, the web follow-up
path), so it is gated behind ``PRECIS_FRICTION_REFLECT`` and enabled
deliberately — mirroring the classifier pass's default-OFF discipline.
Eligibility is further narrowed to runs that (a) have MCP tools (so the
agent *can* ``put`` a gripe) and (b) carry enough turn headroom that the
reflection won't crowd out the task. The one-shot JSON judges never
reach here — they use :mod:`precis.utils.claude_p`, a schema-locked
shape — so they are structurally excluded.

**Always-ON for read-only runs.** A ``write:none`` envelope (the
``agent_ro`` DB role) gets the nudge on every run from the first release
— Reto 2026-10-25, gr516603; a ro run's friction is otherwise lost
entirely, and the DB caps it (3 per run tag/hour, 20 per hour, 4000
chars) so no junk-rate gauge is needed. Its footer is
:data:`FRICTION_REFLECTION_RO`: same binary ask, but the filing is
create-only (the definer function stamps ``source:agent_ro`` +
``friction`` + ``agent:<run>`` itself, so no ``tags=``) and it carries
the FRICTION-block output fallback for when ``put`` is refused — the
envelope's tier-1 tool deny still blocks ``mcp__precis__put`` under
``claude -p``, so the block is the ro run's real channel there.
"""

from __future__ import annotations

from precis.utils.env import env_flag

# Below this ``--max-turns`` we skip the footer: a tight run should
# spend its budget on the task, not on meta-reflection. The prompt also
# says "never at the expense of your task", but this is the cheap
# call-time guard the spec asked for ("skip on tight-budget runs").
FRICTION_MIN_TURNS = 8

# The reflection footer. Binary-first, terminal, "none" is the honored
# default (kills the confabulation pressure of always being asked).
# Grounded on the actual call sequence + a check-the-skill-first gate,
# so a genuine capability gap files a gripe while a discovery gap
# (verb exists, agent didn't find it) is caught before it becomes noise.
FRICTION_REFLECTION = """

────────────────────────────────────────────────────────
Before you finish — one reflection, then stop:

Did any precis tool actually get in your way this run — a call you
couldn't make, several calls where one should have worked, a result
shape you had to re-query, an argument that surprised you? If nothing
got in your way, just say "friction: none" and finish. That is the
expected answer for most runs — do not invent friction to be helpful.

If something genuinely did: first check the relevant `precis-*-help`
skill — does the verb you wanted already exist? If it does, you found
it; no gripe. If it genuinely does not, file exactly ONE gripe:

    put(kind='gripe',
        text='<the ideal call you wanted, and the calls you actually '
             'had to make instead>',
        tags=['friction', 'friction-model:<your model>'])

Describe the *tool* interaction, not your task content. Do not run more
tools to investigate — reflect only from what you already did. Never do
this at the expense of finishing your actual task.
────────────────────────────────────────────────────────
"""

# The read-only variant (gr516603): same binary ask and honored
# "friction: none" default, but the filing is create-only — the definer
# stamps the tags, so teaching ``tags=`` would just earn a refused call
# — and it teaches the FRICTION-block output fallback (mechanism (b) of
# the spec): under ``claude -p`` the envelope's tier-1 deny still blocks
# ``mcp__precis__put``, and the DB caps a ro run at 3 filings, so a ro
# agent that can't ``put`` still reports in a shape its rw
# parent/coordinator can file.
FRICTION_REFLECTION_RO = """

────────────────────────────────────────────────────────
Before you finish — one reflection, then stop:

Did any precis tool actually get in your way this run — a call you
couldn't make, several calls where one should have worked, a result
shape you had to re-query, an argument that surprised you? If nothing
got in your way, just say "friction: none" and finish. That is the
expected answer for most runs — do not invent friction to be helpful.

If something genuinely did: first check the relevant `precis-*-help`
skill — does the verb you wanted already exist? If it does, you found
it; no gripe. If it genuinely does not, file at most THREE gripes (one
per pain point):

    put(kind='gripe',
        text='<the ideal call you wanted, and the calls you actually '
             'had to make instead>')

Your run is read-only: the server stamps the gripe source:agent_ro +
friction + agent:<run> itself — do not pass tags=, link= or an id.
Bodies cap at 4000 chars and the run at 3 filings.

If `put` is refused anyway (tool-level deny, or a cap), end your output
with one FRICTION block per pain point instead (at most 3):

    FRICTION
    title: <one line>
    body: <what blocked you, what you expected, up to 1200 chars>
    END

Describe the *tool* interaction, not your task content. Do not run more
tools to investigate — reflect only from what you already did. Never do
this at the expense of finishing your actual task.
────────────────────────────────────────────────────────
"""


def friction_enabled() -> bool:
    """Whether the end-of-run friction reflection is switched on for
    write-capable runs (``PRECIS_FRICTION_REFLECT``). Default-OFF —
    read-only runs don't need it (see :func:`friction_eligible`)."""
    return env_flag("PRECIS_FRICTION_REFLECT")


def friction_eligible(
    *, has_mcp: bool, max_turns: int, read_only: bool = False
) -> bool:
    """Is this run eligible for the friction footer?

    Requires MCP tools present (so the agent can actually ``put`` a
    gripe) and enough turn headroom that the reflection won't crowd out
    the task. The global switch gates *write-capable* runs; a read-only
    run (``write:none`` envelope → the ``agent_ro`` DB role) is always
    eligible — its friction is otherwise lost entirely (gr516603).
    """
    if not has_mcp or max_turns < FRICTION_MIN_TURNS:
        return False
    return read_only or friction_enabled()


def append_friction_footer(
    system_prompt_text: str | None,
    *,
    has_mcp: bool,
    max_turns: int,
    read_only: bool = False,
) -> str | None:
    """Return the system-prompt text with the friction footer appended
    when this run is eligible; otherwise return it unchanged.

    ``None`` in stays ``None`` out when ineligible; when eligible with no
    prior system prompt, the footer becomes the whole system prompt. A
    read-only run gets :data:`FRICTION_REFLECTION_RO` — create-only
    ``put`` plus the FRICTION-block fallback — instead of the
    tag-teaching :data:`FRICTION_REFLECTION`.
    """
    if not friction_eligible(has_mcp=has_mcp, max_turns=max_turns, read_only=read_only):
        return system_prompt_text
    footer = FRICTION_REFLECTION_RO if read_only else FRICTION_REFLECTION
    if system_prompt_text:
        return system_prompt_text + footer
    return footer


__all__ = [
    "FRICTION_MIN_TURNS",
    "FRICTION_REFLECTION",
    "FRICTION_REFLECTION_RO",
    "append_friction_footer",
    "friction_eligible",
    "friction_enabled",
]
