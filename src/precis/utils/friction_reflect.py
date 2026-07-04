"""End-of-run tool-friction reflection (Part A of
``docs/design/tool-friction-reflection-and-dreams.md``).

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

**Default-OFF.** Once on, the footer rides *every* agentic run
(reviewers, planner ticks, dream, the web follow-up path), so it is
gated behind ``PRECIS_FRICTION_REFLECT`` and enabled deliberately —
mirroring the classifier pass's default-OFF discipline. Eligibility is
further narrowed to runs that (a) have MCP tools (so the agent *can*
``put`` a gripe) and (b) carry enough turn headroom that the reflection
won't crowd out the task. The one-shot JSON judges never reach here —
they use :mod:`precis.utils.claude_p`, a schema-locked shape — so they
are structurally excluded.
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


def friction_enabled() -> bool:
    """Whether the end-of-run friction reflection is switched on
    (``PRECIS_FRICTION_REFLECT``). Default-OFF."""
    return env_flag("PRECIS_FRICTION_REFLECT")


def friction_eligible(*, has_mcp: bool, max_turns: int) -> bool:
    """Is this run eligible for the friction footer?

    Requires the global switch on, MCP tools present (so the agent can
    actually ``put`` a gripe), and enough turn headroom that the
    reflection won't crowd out the task.
    """
    return friction_enabled() and has_mcp and max_turns >= FRICTION_MIN_TURNS


def append_friction_footer(
    system_prompt_text: str | None,
    *,
    has_mcp: bool,
    max_turns: int,
) -> str | None:
    """Return the system-prompt text with the friction footer appended
    when this run is eligible; otherwise return it unchanged.

    ``None`` in stays ``None`` out when ineligible; when eligible with no
    prior system prompt, the footer becomes the whole system prompt.
    """
    if not friction_eligible(has_mcp=has_mcp, max_turns=max_turns):
        return system_prompt_text
    if system_prompt_text:
        return system_prompt_text + FRICTION_REFLECTION
    return FRICTION_REFLECTION


__all__ = [
    "FRICTION_MIN_TURNS",
    "FRICTION_REFLECTION",
    "append_friction_footer",
    "friction_eligible",
    "friction_enabled",
]
