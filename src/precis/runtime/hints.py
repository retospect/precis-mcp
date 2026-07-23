"""Hint-emitting helpers: the tag-shaped-``q=`` tip and the skill breadcrumb.

``HintsMixin`` carries two independent hint producers: ``_maybe_hint_tag_shaped_q``
emits a ``HintBus`` tip mid-request when a search query looks like a tag
string, and ``_maybe_add_skill_hint`` appends a ``get(kind='skill', ...)``
recovery pointer to an outgoing :class:`~precis.errors.PrecisError`. Distinct
from :mod:`precis.runtime.error`, which renders the final error string —
these only *decorate* the hint/error objects before rendering happens.
"""

from __future__ import annotations

import re
from typing import Any

from precis.errors import PrecisError
from precis.runtime._shared import RuntimeShape

#: Kind → skill alias map for the auto-discovery hint.
#: Used by :meth:`HintsMixin._maybe_add_skill_hint` when
#: ``precis-{kind}-help`` doesn't exist because the kind was renamed
#: but the skill kept its broader name (provider-rooted vs.
#: capability-rooted naming — see ADR 0030 + the rename slice).
_KIND_SKILL_ALIASES: dict[str, str] = {
    "perplexity-research": "precis-perplexity-help",
    "perplexity-reasoning": "precis-perplexity-help",
}


class HintsMixin(RuntimeShape):
    """Tag-shaped-``q=`` search tip + the per-error skill breadcrumb."""

    # ── tag-shaped q hint ──────────────────────────────────────────────

    # Tag tokens are unmistakably tag-shaped: either a closed-prefix
    # axis (UPPERCASE letters + colon + value, e.g. ``STATUS:done``),
    # a lowercase namespace + colon + value (``topic:co2-capture``),
    # or a kebab-case slug with multiple hyphens
    # (``exercise-mcp-throwaway``). Bare single words — `cells`,
    # `photocatalysis`, `pinned`, `topic` — are NOT tag-shaped for
    # this heuristic: the round-2 picky pass found that matching any
    # lowercase token fired the tip on basically every common
    # English search query (F-2). Requiring a colon or ≥1 hyphen
    # tightens the gate to actually-tag-looking strings.
    _TAG_SHAPED_Q_RE = re.compile(
        r"^(?:"
        # closed prefix: UPPERCASE letters/digits/_, colon, value chars
        r"[A-Z][A-Z0-9_]*:[A-Za-z0-9][\w.'-]*"
        # lowercase namespace + colon + value
        r"|[a-z][a-z0-9_]*:[\w.'-]+"
        # kebab-case slug with ≥1 hyphen
        r"|[a-z0-9]+(?:-[a-z0-9]+)+"
        r")$"
    )

    def _maybe_hint_tag_shaped_q(self, args: dict[str, Any]) -> None:
        """Emit a HintBus tip when ``q=`` looks like a tag string.

        Fires only when the call provides ``q=`` but no ``tags=`` —
        semantic search on a single tag-shaped token tends to match
        the substring against unrelated bodies (the broad usability
        pass saw ``q='exercise-mcp-throwaway'`` return paper-block
        hits about "exercise"). The hint is a HintBus tip rather
        than an error so the call still runs; agents that genuinely
        wanted the semantic match are not blocked.
        """
        q = args.get("q")
        if not isinstance(q, str):
            return
        token = q.strip()
        if not token or " " in token:
            return
        if args.get("tags"):
            return
        if not self._TAG_SHAPED_Q_RE.match(token):
            return
        from precis.hints import Hint

        # Round-2 picky N-3, 2026-05-30: dedup on the *query value*
        # rather than a static topic. The static-topic form
        # (``topic="search.tag_shaped_q"``) suppressed the hint on
        # every subsequent tag-shape call after the first — even when
        # the query was different, which is genuinely new information
        # the agent should see. Per-query dedup means the same query
        # repeated keeps suppressing (correct), but a different
        # tag-shape query re-fires (correct).
        self.hub.emit_hint(
            Hint(
                text=(
                    f"q={token!r} looks like a tag — semantic search "
                    "will match the substring against unrelated bodies. "
                    f"If you meant the tag filter, retry with "
                    f"tags=[{token!r}] (and pass q='...' as the topic "
                    "to rank within, or omit q= to list by recency)."
                ),
                topic=f"search.tag_shaped_q:{token}",
                cooldown=6,
            )
        )

    def _maybe_add_skill_hint(
        self, err: PrecisError, verb: str, args: dict[str, Any]
    ) -> None:
        # See _KIND_SKILL_ALIASES above for the module-level map.
        """F6: append a per-kind / per-verb help-skill `next:` hint.

        Mutates ``err.next`` in place to add a discoverability pointer
        without losing whatever the handler already put there. Order:
        (1) caller-supplied hints, then (2) per-kind skill if the call
        named one, else per-verb skill, else the overview. The LLM
        reads top-down and grabs the most-specific recovery action
        first; the new hint is the second-best option.
        """
        kind = args.get("kind") if isinstance(args, dict) else None
        # Drop list/wildcard kinds — the help skill for "paper,patent"
        # or "*" doesn't exist; fall through to the verb/overview hint.
        if isinstance(kind, str) and ("," in kind or kind == "*"):
            kind = None

        live_kinds = set(self.hub.kinds) if self.hub is not None else set()
        if isinstance(kind, str) and kind in live_kinds:
            # ``kind='skill'`` has no ``precis-skill-help`` — skills ARE
            # the help system, so the auto-generated breadcrumb points at
            # an id the caller just failed to fetch (broad-pass R3#3:
            # the NotFound for a bad skill slug ended with a self-
            # referential ``next: get(kind='skill', id='precis-skill-help')``).
            # Route to the live catalogue instead so the recovery hint
            # is always runnable.
            if kind == "skill":
                hint = "get(kind='skill', id='toc')"
            elif kind in _KIND_SKILL_ALIASES:
                # Renamed kinds whose `precis-{kind}-help` doesn't
                # exist (the skill kept the broader provider-rooted
                # name). Mapped here so the auto-hint stays runnable.
                hint = f"get(kind='skill', id='{_KIND_SKILL_ALIASES[kind]}')"
            else:
                hint = f"get(kind='skill', id='precis-{kind}-help')"
        elif verb in {"get", "search", "put", "edit", "delete", "tag", "link"}:
            hint = f"get(kind='skill', id='precis-{verb}-help')"
        else:
            hint = "get(kind='skill', id='precis-overview')"

        existing = err.next
        if existing is None:
            err.next = hint
        elif isinstance(existing, str):
            if hint not in existing:
                err.next = [existing, hint]
        else:
            if hint not in existing:
                err.next = [*existing, hint]
