"""CQL assembly for the ``patent`` kind.

EPO OPS uses CQL (Common Query Language, OPS dialect). The handler
exposes the cross-kind ``search(q=, tags=, scope=, top_k=)`` surface
and translates it into CQL on the remote leg.

Two inputs feed the lift:

* ``q=`` — auto-promoted to ``(ti="..." OR ab="...")`` if it's a
  bare keyword phrase, or passed through wrapped in parens if it
  already looks like CQL.
* ``tags=`` — closed-set translation table maps the patent-specific
  open lowercase prefixes (``cpc:``, ``ipc:``, ``applicant:``,
  ``country:``, ``kind:``, ``family:``) to the corresponding OPS CQL
  fields. Open prefixes that don't have a CQL equivalent
  (``topic:``, ``project:``, …) are silently skipped — they only
  narrow the local SQL leg.

Tag values are stored lowercased (precis convention via
``Tag.open()``). The lift re-uppercases ISO country codes, kind
codes, and CPC/IPC classes; for **applicants** it consults
``meta.applicants[]`` on a local patent so canonical spelling
survives the slug round-trip. Naive title-case is the cold-start
fallback.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from precis.errors import BadInput

if TYPE_CHECKING:
    pass


class _StoreProto(Protocol):
    """Subset of ``Store`` used by the lift — narrowed for testability."""

    def find_first_meta_for_open_tag(self, *, kind: str, tag: str) -> dict | None: ...


# ---------------------------------------------------------------------------
# Lookup tables
# ---------------------------------------------------------------------------

_TAG_TO_CQL: dict[str, str] = {
    "cpc": "cpc",  # cpc:b01j27/24 → cpc=B01J27/24
    "ipc": "ipc",  # ipc:h01m       → ipc=H01M
    "applicant": "pa",  # applicant:siemens-ag → pa="Siemens AG"
    "country": "pact",  # country:ep      → pact=EP
    "kind": "kind",  # kind:b1         → kind=B1
    "family": "famn",  # family:12345678 → famn=12345678
}

# Detect CQL-shaped ``q=`` so we don't double-wrap the title/abstract
# disjunction around something that's already a CQL expression. The
# tokens are surrounded by spaces so they don't false-match inside
# words ('ipa=' would match 'a' otherwise).
_CQL_OPERATORS: tuple[str, ...] = (
    " and ",
    " or ",
    " not ",
    " within ",
)
_CQL_FIELD_MARK = "="  # field=value is always CQL-shaped


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_cql(
    *,
    q: str | None,
    tags: list[str] | None,
    store: _StoreProto | None = None,
) -> str:
    """Assemble one CQL string for the OPS remote leg.

    Args:
        q:     Free-text or raw CQL. ``None`` / empty is allowed iff
               at least one tag lifts to a CQL clause.
        tags:  Lowercased ``"prefix:value"`` strings as stored in
               ``ref_open_tags``. Open prefixes without a CQL
               equivalent are silently skipped.
        store: Used by ``applicant:`` lift to look up canonical
               spelling from ``meta.applicants[]``. Optional —
               cold-start fallback is naive title-case unslug.

    Raises:
        BadInput: neither ``q=`` nor any liftable tag was provided.
    """
    parts: list[str] = []

    if q is not None and q.strip():
        parts.append(_promote_or_passthrough(q.strip()))

    for tag in tags or []:
        clause = _lift_tag(tag, store=store)
        if clause is not None:
            parts.append(clause)

    if not parts:
        raise BadInput(
            "search requires q= or a CQL-liftable tag",
            next=(
                "search(kind='patent', q='photocatalysis') or "
                "search(kind='patent', tags=['cpc:b01j27/24'])"
            ),
        )

    return " and ".join(parts)


def _promote_or_passthrough(q: str) -> str:
    """Bare keyword → ``(ti="..." OR ab="...")``; CQL → wrap in parens.

    Bare-keyword detection: if the string contains ``=`` (a CQL
    field=value) or any boolean operator (case-insensitive), treat
    it as raw CQL. Otherwise wrap it as a phrase match against the
    title and abstract fields. This is the same heuristic the spec
    references in ``docs/patent-kind-spec.md``.
    """
    lower = q.lower()
    is_cql = _CQL_FIELD_MARK in q or any(op in f" {lower} " for op in _CQL_OPERATORS)
    if is_cql:
        return f"({q})"

    safe = q.replace("\\", "\\\\").replace('"', '\\"')
    return f'(ti="{safe}" OR ab="{safe}")'


def _lift_tag(tag: str, *, store: _StoreProto | None) -> str | None:
    """One stored tag → one CQL clause, or None if the prefix is open.

    Returns None for tags whose prefix isn't in ``_TAG_TO_CQL`` —
    those are open lowercase prefixes (``topic:``, ``project:``)
    that only narrow the local SQL leg.
    """
    if ":" not in tag:
        return None
    prefix, _, value = tag.partition(":")
    field = _TAG_TO_CQL.get(prefix)
    if field is None:
        return None
    if not value:
        return None

    if prefix == "applicant":
        phrase = _resolve_applicant(value, store=store)
    elif prefix in ("country", "kind"):
        phrase = value.upper()
    elif prefix in ("cpc", "ipc"):
        phrase = _classification_canonical(value)
    else:
        phrase = value

    return f'{field}="{_escape(phrase)}"'


def _classification_canonical(slug: str) -> str:
    """Lowercased CPC/IPC slug → canonical OPS form.

    Storage rule: tag values are lowercased on insert. CPC/IPC use
    uppercase letters in their canonical form (e.g. ``B01J27/24``).
    OPS itself is forgiving on case for these fields, but the
    canonical render makes search hits match human-eyed citations.
    """
    return slug.upper()


def _resolve_applicant(slug: str, *, store: _StoreProto | None) -> str:
    """Slugged applicant tag → canonical OPS phrase.

    Strategy:
        1. Look up any local patent ref tagged ``applicant:<slug>``,
           read its ``meta.applicants[]``, return the first
           canonical name whose own slugification matches.
        2. Fall back to naive ``hyphen→space`` + Title Case.

    Why: tag-storage is lossy on case and on space-vs-hyphen. The
    canonical name lives in ``meta.applicants`` from biblio parsing,
    so use it whenever available.
    """
    if store is not None:
        meta = store.find_first_meta_for_open_tag(
            kind="patent", tag=f"applicant:{slug}"
        )
        if meta is not None:
            for app in meta.get("applicants", []) or []:
                name = app.get("name") if isinstance(app, dict) else None
                if isinstance(name, str) and slugify_applicant(name) == slug:
                    return name

    # Cold-start fallback. Title-case ASCII is fine for most western
    # applicants ('siemens-ag' → 'Siemens Ag'); OPS phrase matching
    # is forgiving enough that 'pa="Siemens Ag"' still finds the
    # right records. Once one local patent for the applicant has
    # been ingested, the meta lookup above wins.
    return slug.replace("-", " ").title()


def slugify_applicant(name: str) -> str:
    """Canonical applicant slug: lowercased, spaces → hyphens, intrinsic
    hyphens preserved. Round-trippable on simple western names.

    Examples:
        ``"Siemens AG"`` → ``"siemens-ag"``
        ``"Hewlett-Packard"`` → ``"hewlett-packard"``
        ``"BASF SE"`` → ``"basf-se"``

    Note: this is lossy on names that mix spaces *and* hyphens
    ambiguously; the ``_resolve_applicant`` cache hits the local
    meta first to recover the canonical spelling.
    """
    out = name.strip().lower()
    # Collapse runs of whitespace to single hyphen.
    parts = out.split()
    return "-".join(parts)


def _escape(value: str) -> str:
    """Escape a value for a CQL string literal.

    OPS CQL uses double-quoted strings with backslash escaping for
    embedded quotes. We do the minimum: escape backslashes first,
    then quotes. Leave other characters alone — OPS handles
    accents/diacritics natively.
    """
    return value.replace("\\", "\\\\").replace('"', '\\"')


def validate_strict_cql(cql: str) -> str:
    """Validate that ``cql`` is explicit CQL — no bare-keyword auto-promote.

    Phase-2 ``watch-patents`` rule (spec § Phase 2 testing notes,
    confirmed in this build): saved watches must use explicit CQL so
    their meaning doesn't drift if the ad-hoc ``q=`` auto-promote
    heuristic ever changes. Bare keywords like ``"photocatalysis"``
    are rejected at create time with a recovery hint pointing at the
    explicit fields.

    Returns the CQL trimmed of leading/trailing whitespace on
    success. Raises ``BadInput`` with a recovery hint on failure —
    same shape as ``parse_docdb_id``'s rejections so the agent has a
    consistent fix-it surface.

    Validation is intentionally lightweight:

    * non-empty;
    * contains either an explicit ``field=value`` mark, or one of
      the recognised boolean operators (``and`` / ``or`` / ``not`` /
      ``within``).

    We do **not** try to parse OPS's full CQL grammar — OPS itself
    will reject malformed expressions on first run with a clear 4xx,
    and over-strict client-side validation would lock out perfectly
    legal queries that use rare fields we haven't enumerated.
    """
    if not isinstance(cql, str):
        raise BadInput(
            f"watch CQL must be a string, got {type(cql).__name__!r}",
            next="watch-patents 'cpc=B01J27/24 and pa=\"Siemens AG\"'",
        )

    trimmed = cql.strip()
    if not trimmed:
        raise BadInput(
            "watch CQL is empty",
            next="watch-patents 'cpc=B01J27/24'",
        )

    lower = trimmed.lower()
    has_field = _CQL_FIELD_MARK in trimmed
    has_operator = any(op in f" {lower} " for op in _CQL_OPERATORS)

    if not (has_field or has_operator):
        raise BadInput(
            f"watch CQL must be explicit, not a bare keyword: {cql!r}",
            next=(
                "watches run unattended for years - meaning shouldn't "
                "drift if auto-promote rules change. Use explicit CQL "
                "fields, e.g. "
                '\'ti="photocatalysis" or ab="photocatalysis"\', '
                "'cpc=B01J27/24', 'pa=\"Siemens AG\"'"
            ),
        )

    return trimmed


__all__ = ["build_cql", "slugify_applicant", "validate_strict_cql"]
