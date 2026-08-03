"""Stub-eligibility SQL fragment — DRY across every stub-backlog query.

A *stub* (docs/design/stubs-mcp-and-skill.md) is a ``paper`` ref with an
external identifier (DOI / arXiv / S2 by default) registered but no PDF
yet — the backlog the ``fetch_oa`` worker auto-chases. Three call sites
need the identical predicate: :func:`precis.store._refs_ops.RefsMixin.
stub_backlog` and ``.stub_backlog_count`` (``store/_refs_ops.py``), and
the ``fetch_oa`` worker's claim query
(``workers/fetch_oa.py::claim_stubs_to_fetch``). Before this module the
predicate was hand-copied at all three — a known DRY defect that let the
"what counts as fetchable" rule drift out of sync across the copies.

:data:`STUB_ID_KINDS` is the fixed whitelist an ``id_kinds=`` argument is
filtered against before it reaches SQL. ``id_kinds`` feeds a literal
``IN (...)`` list, so :func:`stub_predicate_sql` builds that list by
walking the whitelist itself (checking each fixed token for membership
in the caller's request), never by splicing a caller-supplied string
straight into the query text — that's the difference between a filter
and a SQL-injection seam.
"""

from __future__ import annotations

from collections.abc import Iterable

#: The only identifier kinds that make a paper stub "fetchable" — the
#: ``fetch_oa`` worker only knows how to chase these three. Canonical
#: order matches the original hand-copied literal (``'doi', 'arxiv',
#: 's2'``) so the default call shape reproduces byte-identical SQL.
_STUB_ID_KIND_ORDER: tuple[str, ...] = ("doi", "arxiv", "s2")

STUB_ID_KINDS: frozenset[str] = frozenset(_STUB_ID_KIND_ORDER)


def stub_predicate_sql(
    alias: str = "r", id_kinds: Iterable[str] = _STUB_ID_KIND_ORDER
) -> str:
    """The shared "is this a fetchable stub" WHERE-fragment.

    References ``<alias>.kind`` / ``<alias>.pdf_sha256`` /
    ``<alias>.deleted_at`` / ``<alias>.ref_id`` — the caller's query
    must alias the ``refs`` row accordingly (``r`` everywhere today).

    ``id_kinds`` narrows which external-identifier kinds count as
    fetchable (e.g. the DOI-only chase queue passes ``('doi',)``). The
    accepted list is built by walking the fixed :data:`STUB_ID_KINDS`
    order and keeping only the tokens the caller asked for — so the
    text spliced into the ``IN (...)`` list is always one of the three
    hard-coded literals, never a caller-supplied string, regardless of
    what garbage ``id_kinds`` might contain.
    """
    requested = set(id_kinds)
    accepted = [k for k in _STUB_ID_KIND_ORDER if k in requested]
    if not accepted:
        raise ValueError(
            f"stub_predicate_sql: no valid id_kinds in {tuple(id_kinds)!r} "
            f"(accepted: {sorted(STUB_ID_KINDS)})"
        )
    kinds_sql = ", ".join(f"'{k}'" for k in accepted)
    return (
        f"{alias}.kind = 'paper' AND {alias}.pdf_sha256 IS NULL "
        f"AND {alias}.deleted_at IS NULL "
        f"AND EXISTS (SELECT 1 FROM ref_identifiers ri "
        f"WHERE ri.ref_id = {alias}.ref_id AND ri.id_kind IN ({kinds_sql}))"
    )
