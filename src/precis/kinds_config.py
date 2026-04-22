"""Per-agent masking via ``PRECIS_KINDS`` — parser + validation.

Implements §13 of ``docs/plugin-architecture.md``: a single env var with
bracket syntax narrows what each server instance exposes, per-kind verbs
included.

Grammar::

    PRECIS_KINDS = KIND_SPEC [, KIND_SPEC …]
    KIND_SPEC    = KIND [ '[' VERB [, VERB …] ']' ]
    VERB         = search | get | put | move

Rules:

- Bare kind → all four verbs allowed.
- Bracketed kind → whitelist only those verbs.
- Kinds not listed → absent from the tool enum.
- Env unset / empty → **no mask**; all registered kinds exposed with all
  verbs (caller treats ``None`` from ``load_from_env`` as "no filter").

Validation is strict — ambiguity is never silently fixed. Fatal cases
(raise :class:`ConfigError` → server main exits with code 2):

- An **alias** appears in config (e.g. ``wolfram`` instead of ``math``).
- An **unknown verb** appears in brackets.
- **Empty brackets** (e.g. ``paper[]``).
- A **duplicate kind** appears more than once in the list.
- **Malformed** tokens (unbalanced brackets, nested brackets, stray commas).

Non-fatal cases (just skip and warn):

- A **kind name not in the registry** is dropped with a startup warning.
  Aliases do NOT fall into this bucket — they're fatal above.

See §10.1 for the full fatal/non-fatal matrix.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping

from precis.protocol import VERBS

__all__ = [
    "ConfigError",
    "load_from_env",
    "parse_precis_kinds",
]


class ConfigError(ValueError):
    """Fatal configuration error — caller should exit(2).

    Carries a single-line human message suitable for stderr.  Matches the
    §10.1 fatal path: ``print(err, file=sys.stderr); sys.exit(2)``.
    """


# ---------------------------------------------------------------------------
# Grammar — compiled once at module import
# ---------------------------------------------------------------------------

# Match a KIND_SPEC: ``kind`` or ``kind[verb, verb, …]``.
# Kind names are ASCII letters/digits/underscore/dash; colon and URI
# punctuation are rejected so URI-style mistakes fail early with a clear
# message instead of parsing as a kind called ``paper:wang2020state``.
_KIND_SPEC = re.compile(
    r"""
    ^\s*
    (?P<kind>[A-Za-z0-9_\-]+)       # kind name
    (?:
        \s*\[\s*
        (?P<verbs>[^\[\]]*)         # verbs inside brackets — no nesting
        \s*\]\s*
    )?
    \s*$
    """,
    re.VERBOSE,
)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def parse_precis_kinds(
    value: str,
    *,
    aliases: Mapping[str, str] | None = None,
    known_kinds: Mapping[str, object] | None = None,
    warnings_out: list[str] | None = None,
) -> dict[str, frozenset[str]] | None:
    """Parse a ``PRECIS_KINDS`` string into a ``{kind: frozenset(verbs)}`` mask.

    Returns ``None`` when ``value`` is empty / whitespace-only (meaning "no
    filtering — expose every registered kind with every verb").

    Parameters:
        value: The raw env-var string.  ``""`` / ``None`` → no filtering.
        aliases: Optional ``{alias_name: canonical_name}`` map used for
            fatal detection when someone puts an alias in config.  If not
            provided, the check is skipped (useful in unit tests that want
            to exercise the grammar in isolation).
        known_kinds: Optional collection whose ``__contains__`` answers
            "is this a valid canonical kind name?".  Unknown names become
            **non-fatal warnings** appended to ``warnings_out``; they are
            dropped from the returned mask so downstream consumers never
            see them.  If not provided, every name is accepted as-is (the
            caller can filter later).
        warnings_out: Optional list to append human-readable non-fatal
            warnings to.  Ignored when ``None``.

    Raises:
        ConfigError: On any fatal case (§10.1 fatal column).

    Returns:
        ``{kind_name: frozenset(verbs)}`` or ``None`` for no-filter.
    """
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None

    mask: dict[str, frozenset[str]] = {}
    seen: set[str] = set()  # for duplicate detection

    for raw_spec in _split_top_level(stripped):
        token = raw_spec.strip()
        if not token:
            raise ConfigError(
                "PRECIS_KINDS: stray comma produces empty kind spec — "
                "check for leading/trailing/doubled commas"
            )
        match = _KIND_SPEC.match(token)
        if not match:
            raise ConfigError(
                f"PRECIS_KINDS: malformed kind spec {token!r} — "
                f"expected 'kind' or 'kind[verb,verb,…]'"
            )
        kind = match.group("kind")
        verbs_raw = match.group("verbs")

        # Alias-in-config → fatal.
        if aliases is not None and kind in aliases:
            canonical = aliases[kind]
            raise ConfigError(
                f"PRECIS_KINDS contains alias {kind!r}. "
                f"Use canonical name {canonical!r}. "
                f"Aliases are for runtime URI compatibility only, not for config."
            )

        # Duplicate kind → fatal.
        if kind in seen:
            raise ConfigError(f"PRECIS_KINDS: kind {kind!r} listed more than once")
        seen.add(kind)

        # Verbs: bare kind → all four; bracketed → whitelist, must be non-empty.
        if verbs_raw is None:
            verbs: frozenset[str] = VERBS  # all allowed
        else:
            verb_tokens = [v.strip() for v in verbs_raw.split(",")]
            # Empty brackets or stray commas inside brackets → fatal.
            if not verb_tokens or any(not v for v in verb_tokens):
                raise ConfigError(
                    f"PRECIS_KINDS: kind {kind!r} has empty brackets or a "
                    f"stray comma inside its verb list"
                )
            for v in verb_tokens:
                if v not in VERBS:
                    allowed = ", ".join(sorted(VERBS))
                    raise ConfigError(
                        f"PRECIS_KINDS: unknown verb {v!r} for kind {kind!r}. "
                        f"Allowed verbs: {allowed}"
                    )
            verbs = frozenset(verb_tokens)

        # Unknown kind → non-fatal warning, drop from mask.
        if known_kinds is not None and kind not in known_kinds:
            msg = (
                f"PRECIS_KINDS: kind {kind!r} is not registered — "
                f"skipped (check for typos or missing plugin)"
            )
            if warnings_out is not None:
                warnings_out.append(msg)
            continue

        mask[kind] = verbs

    return mask


# ---------------------------------------------------------------------------
# Env loader
# ---------------------------------------------------------------------------


def load_from_env(
    *,
    env: Mapping[str, str] | None = None,
    aliases: Mapping[str, str] | None = None,
    known_kinds: Mapping[str, object] | None = None,
    warnings_out: list[str] | None = None,
) -> dict[str, frozenset[str]] | None:
    """Read ``PRECIS_KINDS`` from the environment and parse it.

    Thin wrapper over :func:`parse_precis_kinds` that pulls the env var
    value.  Accepts an explicit ``env`` mapping for tests (defaults to
    ``os.environ``).  Same return contract: ``None`` → no filter applied.
    """
    env_map = env if env is not None else os.environ
    raw = env_map.get("PRECIS_KINDS", "")
    return parse_precis_kinds(
        raw,
        aliases=aliases,
        known_kinds=known_kinds,
        warnings_out=warnings_out,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _split_top_level(value: str) -> list[str]:
    """Split at top-level commas, respecting bracket grouping.

    ``"paper,doc[get,search],memory"`` → ``["paper", "doc[get,search]", "memory"]``.

    Does not validate bracket balance fully — that's the caller's job via
    ``_KIND_SPEC``.  But it does detect obviously nested or unclosed
    brackets and raises :class:`ConfigError` up-front for a cleaner
    message than the regex would give.
    """
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    for ch in value:
        if ch == "[":
            if depth:
                raise ConfigError(
                    "PRECIS_KINDS: nested '[' is not allowed — "
                    "verb lists may not contain another bracket group"
                )
            depth += 1
            buf.append(ch)
        elif ch == "]":
            if depth == 0:
                raise ConfigError(
                    "PRECIS_KINDS: unbalanced ']' — closing bracket with "
                    "no matching opener"
                )
            depth -= 1
            buf.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(buf))
            buf.clear()
        else:
            buf.append(ch)
    if depth != 0:
        raise ConfigError(
            "PRECIS_KINDS: unbalanced '[' — opening bracket with no matching closer"
        )
    # Always flush once after the loop when we saw at least one split or
    # have content in buf.  Trailing commas thus produce an empty-string
    # entry that the caller's stray-comma check fires on, instead of being
    # silently dropped.  Pure-empty inputs never reach here — they're
    # filtered by the earlier ``stripped`` guard.
    if buf or parts:
        parts.append("".join(buf))
    return parts
