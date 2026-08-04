"""Stub-eligibility SQL fragment — DRY across every stub-backlog query.

A *stub* (docs/design/stubs-mcp-and-skill.md) is a ``paper`` ref with an
external identifier (DOI / arXiv / S2 by default) registered but no PDF
yet — the backlog the ``fetch_oa`` worker auto-chases. Call sites needing
the identical predicate: :func:`precis.store._refs_ops.RefsMixin.
stub_backlog`, ``.stub_backlog_count``, and ``.requeue_stubs_for_fetch``
(``store/_refs_ops.py``), and the ``fetch_oa`` worker's claim query
(``workers/fetch_oa.py::claim_stubs_to_fetch``). Before this module the
predicate was hand-copied at all call sites — a known DRY defect that let
the "what counts as fetchable" rule drift out of sync across the copies.

:data:`STUB_ID_KINDS` is the fixed whitelist an ``id_kinds=`` argument is
filtered against before it reaches SQL. ``id_kinds`` feeds a literal
``IN (...)`` list, so :func:`stub_predicate_sql` builds that list by
walking the whitelist itself (checking each fixed token for membership
in the caller's request), never by splicing a caller-supplied string
straight into the query text — that's the difference between a filter
and a SQL-injection seam.

``exclude_retracted`` (default ``True``) drops rows whose
``refs.retraction_status = 'retracted'`` — a retracted paper is not
something we still need to acquire a PDF for, so it falls out of the
chase-candidate query and every stub/chase-queue browse alike. The
comparison uses ``IS DISTINCT FROM`` so ``NULL`` (never checked — the
common case, and the state every stub starts in) stays eligible, and so
does any other non-``'retracted'`` value (``'corrected'`` /
``'expression_of_concern'`` — informational, not a fetch-blocker). Only
the exact ``'retracted'`` literal drops a row.
"""

from __future__ import annotations

from collections.abc import Iterable

#: The only identifier kinds that make a paper stub "fetchable" — the
#: ``fetch_oa`` worker only knows how to chase these three. Canonical
#: order matches the original hand-copied literal (``'doi', 'arxiv',
#: 's2'``) so the default call shape reproduces byte-identical SQL.
_STUB_ID_KIND_ORDER: tuple[str, ...] = ("doi", "arxiv", "s2")

STUB_ID_KINDS: frozenset[str] = frozenset(_STUB_ID_KIND_ORDER)

#: The subset a *human* can hand-download from — ``item_view`` renders a
#: LibKey (DOI) or arXiv PDF link for exactly these. Narrower than
#: :data:`STUB_ID_KINDS`: an S2-only stub is fetchable by the ``fetch_oa``
#: worker but carries no clickable link, so the ``/drive`` "Stubs (to get)"
#: queue floats DOI/arXiv rows ahead of it (``downloadable_first``).
MANUAL_DOWNLOAD_ID_KINDS: frozenset[str] = frozenset(("doi", "arxiv"))


def _accepted_id_kinds(id_kinds: Iterable[str], *, caller: str) -> list[str]:
    """Intersect ``id_kinds`` with the fixed :data:`STUB_ID_KINDS` order.

    Walking the hard-coded order and keeping only requested tokens means
    the strings that ever reach an ``IN (...)`` list are the fixed
    literals, never a caller-supplied string — the difference between a
    filter and a SQL-injection seam. Raises if nothing survives.
    """
    requested = set(id_kinds)
    accepted = [k for k in _STUB_ID_KIND_ORDER if k in requested]
    if not accepted:
        raise ValueError(
            f"{caller}: no valid id_kinds in {tuple(id_kinds)!r} "
            f"(accepted: {sorted(STUB_ID_KINDS)})"
        )
    return accepted


def fetchable_id_exists_sql(
    alias: str = "r",
    *,
    sub_alias: str = "ri",
    id_kinds: Iterable[str] = _STUB_ID_KIND_ORDER,
) -> str:
    """``EXISTS (...)`` fragment: does ``<alias>`` carry an external
    identifier of one of ``id_kinds``?

    The single source of truth for the "has a fetchable id" test —
    reused by :func:`stub_predicate_sql` and by the ``/drive`` browse's
    ``has_external_id`` filter / ``downloadable_first`` ranking
    (``store/_refs_ops.py``) so none of them hand-copy the id-kind
    whitelist. ``sub_alias`` names the inner ``ref_identifiers`` row so
    two fragments (e.g. a WHERE filter *and* an ORDER-BY rank) can
    coexist in one query without colliding. The ``IN (...)`` list is
    built injection-safe via :func:`_accepted_id_kinds`.
    """
    accepted = _accepted_id_kinds(id_kinds, caller="fetchable_id_exists_sql")
    kinds_sql = ", ".join(f"'{k}'" for k in accepted)
    return (
        f"EXISTS (SELECT 1 FROM ref_identifiers {sub_alias} "
        f"WHERE {sub_alias}.ref_id = {alias}.ref_id "
        f"AND {sub_alias}.id_kind IN ({kinds_sql}))"
    )


def stub_predicate_sql(
    alias: str = "r",
    id_kinds: Iterable[str] = _STUB_ID_KIND_ORDER,
    *,
    exclude_retracted: bool = True,
) -> str:
    """The shared "is this a fetchable stub" WHERE-fragment.

    References ``<alias>.kind`` / ``<alias>.pdf_sha256`` /
    ``<alias>.deleted_at`` / ``<alias>.ref_id`` / ``<alias>.
    retraction_status`` — the caller's query must alias the ``refs``
    row accordingly (``r`` everywhere today).

    ``id_kinds`` narrows which external-identifier kinds count as
    fetchable (e.g. the DOI-only chase queue passes ``('doi',)``); the
    id-presence test is delegated to :func:`fetchable_id_exists_sql`,
    which builds the ``IN (...)`` list injection-safe by walking the
    fixed :data:`STUB_ID_KINDS` whitelist.

    ``exclude_retracted`` (default ``True``) additionally drops rows
    already stamped ``retraction_status = 'retracted'`` — see the
    module docstring for the ``IS DISTINCT FROM`` / ``NULL`` rationale.
    Pass ``False`` only for a caller that deliberately wants retracted
    stubs back (none exist today; this is the opt-out valve, not a
    conditional default).
    """
    predicate = (
        f"{alias}.kind = 'paper' AND {alias}.pdf_sha256 IS NULL "
        f"AND {alias}.deleted_at IS NULL "
        f"AND {fetchable_id_exists_sql(alias, id_kinds=id_kinds)}"
    )
    if exclude_retracted:
        # IS DISTINCT FROM (not <>): NULL — unchecked, the default state
        # every stub starts in — must stay eligible so it gets checked at
        # least once; a plain <> comparison drops NULLs (three-valued
        # logic), which would silently exclude every never-checked stub.
        predicate += f" AND {alias}.retraction_status IS DISTINCT FROM 'retracted'"
    return predicate
