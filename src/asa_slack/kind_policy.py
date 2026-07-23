"""Kind-allowlist policy for Slack-originated agent turns.

Slack users may ask asa for research info (papers, patents, citations,
some Perplexity) and keep light notes on the people they talk to; they
must not be able to kick off compute (jobs, quests, cron) or touch
internal-ops surfaces — enforced with the existing boot-time kind gate
(``PRECIS_KINDS_DISABLED``, see ``precis.kind_gate`` + the skill
``precis-kinds-disabled-help``), not just prompt language. The env var is
threaded onto the spawned agent subprocess via ``LlmRequest.env_overlay``.
"""

from __future__ import annotations

#: What a Slack turn's agent may touch. Deliberately narrow — this is a
#: research-lookup + light-memory surface, not a general precis client.
ALLOWED_KINDS: frozenset[str] = frozenset(
    {
        "paper",
        "patent",
        "citation",
        "semanticscholar",
        "orcid",
        "edgar",
        "cfp",
        "web",
        "websearch",
        "wikipedia",
        "perplexity-research",
        "perplexity-reasoning",
        "memory",
        "skill",
    }
)

#: Every kind known to this precis-mcp build (see
#: ``get(kind='skill', id='precis-help')`` for the live roster — last
#: cross-checked 2026-07-22, ADR 0060). Kept as an explicit list rather
#: than a live registry read so the allowlist is computed at import time:
#: ``KNOWN_KINDS - ALLOWED_KINDS`` is the disabled set. This means a kind
#: added to the registry but never added here stays *enabled* for Slack —
#: the one gap, caught by ``tests/test_asa_slack_kind_policy.py`` diffing
#: this constant against the live registry.
KNOWN_KINDS: frozenset[str] = frozenset(
    {
        "agentlog",
        "alert",
        "anki",
        "cad",
        "calc",
        "cfp",
        "citation",
        "concept",
        "conv",
        "cron",
        "datasheet",
        "draft",
        "edgar",
        "email",
        "figure",
        "finding",
        "folder",
        "gripe",
        "job",
        "llm",
        "markdown",
        "math",
        "memory",
        "mermaid",
        "message",
        "news",
        "oracle",
        "orcid",
        "paper",
        "part",
        "patent",
        "pcb",
        "perplexity-reasoning",
        "perplexity-research",
        "plaintext",
        "plan",
        "pres",
        "protein",
        "provenance",
        "quest",
        "random",
        "route",
        "semanticscholar",
        "skill",
        "structure",
        "tag",
        "tex",
        "todo",
        "web",
        "websearch",
        "wikipedia",
        "youtube",
    }
)

assert ALLOWED_KINDS <= KNOWN_KINDS, (
    "kind_policy: an allowed kind is missing from KNOWN_KINDS"
)


def slack_kinds_disabled() -> str:
    """``PRECIS_KINDS_DISABLED`` value for a Slack-originated agent turn.

    Comma-separated, sorted for a stable/diffable env value.
    """
    return ",".join(sorted(KNOWN_KINDS - ALLOWED_KINDS))
