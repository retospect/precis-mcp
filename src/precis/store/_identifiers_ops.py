"""Cross-scheme identifier alias lookup. Mixin on :class:`precis.store.Store`.

A single paper can carry many identifiers — DOI (arXiv DOI form *and*
journal DOI), arXiv id, Semantic Scholar paperId, PubMed id, MAG id,
OpenAlex id, DBLP, plus the local ``pdf_hash`` content fingerprint.
Migration ``0009_ref_identifiers.sql`` introduces the
``ref_identifiers`` table that holds one row per ``(scheme, value)``
mapping into ``refs.id``. This mixin provides:

* :meth:`find_ref_by_identifier`        — generic alias lookup (by scheme+value).
* :meth:`find_paper_ref_by_identifier`  — paper-specific, scheme auto-detected from value shape.
* :meth:`find_paper_refs_by_identifiers_bulk` — the same lookup for MANY values
  at once, bounded queries regardless of N.
* :meth:`insert_ref_identifiers`        — bulk INSERT with ``ON CONFLICT DO NOTHING``.
* :meth:`list_ref_identifiers`          — read-back of all aliases for a ref.

The shape of ``scheme`` strings is documented in the migration header;
recognised values are ``'doi'``, ``'arxiv'``, ``'s2'``, ``'pubmed'``,
``'mag'``, ``'openalex'``, ``'dblp'``, ``'corpusid'``, ``'pdfsha256'``.
The mixin doesn't enforce a closed vocabulary — the table ``CHECK``
constraint only requires lowercase non-empty strings.

Per-scheme reliability (see migration header for full doc):
* doi / arxiv / pdfsha256 are strict — one value -> one paper.
* s2 is pragmatic — S2 sometimes merges distinct papers under one
  paperId. We adopt S2's clustering wholesale; conflicts surface in
  ``ref_identifier_conflicts`` for operator review.

Canonical case rule (gr514198): ``id_value`` is stored LOWERCASE for
every scheme — an OpenAlex id is ``w…`` here, not the ``W…`` casing
``meta.openalex.id`` / ``meta.openalex.referenced_works`` / the OpenAlex
API itself use. Every write and lookup through this mixin runs
:func:`_normalise_identifier`, so the rule is invisible to mixin
callers — but raw SQL outside the mixin must remember it on BOTH sides:
probe ``WHERE id_kind='openalex' AND id_value = lower(:w_id)`` (or match
``lower(id_value)`` so a legacy mixed-case row still resolves). A
``w…`` ↔ ``W…`` miss is silent — the row exists, the join just doesn't.
The DOI scheme is additionally enforced by the ``0049`` trigger; other
schemes have no DB-level guard, so the Python helpers are the chokepoint.

Cleared-identifier marker (gr515246): a human ``clear`` (``edit``'s
``doi=''`` / the Meta tab's blanked field) stamps
``refs.meta.cleared_identifiers.<scheme> = {"at", "by"}`` on top of
deleting the ``ref_identifiers`` row. Without it the next automated
sweep (``doi_backfill``, ``apply_resolution``, remediate) saw a
DOI-less paper with a title and re-derived the identifier the operator
just removed — the deleted DOI "came back". Both writers consult the
marker: a *machine*-sourced write of a marked scheme is skipped, a
human-sourced one lands and lifts the marker (the operator changed
their mind). Machine clears (e.g. ``park_mismatch``'s
registry-title-zero-overlap DOI drop, ``source='resolve-metadata'``)
never stamp it — a parked ref is still allowed to auto-recover the
*correct* identifier under review.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from psycopg.types.json import Jsonb

if TYPE_CHECKING:
    from psycopg import Connection
    from psycopg_pool import ConnectionPool

log = logging.getLogger(__name__)

#: ``refs.meta`` key recording identifiers a human deliberately cleared:
#: ``{scheme: {"at": <ts>, "by": <source>}}``. See the module docstring.
CLEARED_IDENTIFIERS_META_KEY = "cleared_identifiers"

#: ``source`` values that mean a person made the change — the same set
#: :func:`precis.store._refs_ops._default_author_source` maps to the
#: ``human`` tier. Only these stamp (on clear) / lift (on set) the
#: :data:`CLEARED_IDENTIFIERS_META_KEY` marker.
_HUMAN_EDIT_SOURCES = frozenset({"web-edit", "mcp-edit", "edit"})


# Identifier shape detection -------------------------------------------------
#
# Each pattern recognises a value form unambiguously enough that we can
# infer the scheme when the caller passes a bare identifier string
# (e.g. an agent doing `get(kind='paper', id='1705.02630')`). Order of
# checking matters: more-specific patterns first.

# Registrant length is unbounded in the DOI grammar — real (if rare)
# prefixes run shorter than four digits, so the bound stays loose; the
# ``10.<digits>/`` shape is already unambiguous against every other
# scheme here.
_DOI_RE = re.compile(r"^10\.\d{1,9}/.+", re.IGNORECASE)
# arXiv post-2007 format: YYMM.NNNN[N][vN]
_ARXIV_NEW_RE = re.compile(r"^\d{4}\.\d{4,5}(v\d+)?$")
# arXiv pre-2007 format: archive/YYMMNNN, e.g. cond-mat/0211262, math-ph/9910009
_ARXIV_OLD_RE = re.compile(r"^[a-z][a-z\-\.]+/\d{7}(v\d+)?$", re.IGNORECASE)
# Semantic Scholar paperId: 40-char hex (SHA1)
_S2_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
# pdf_hash: 64-char hex (SHA-256)
_PDF_HASH_RE = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)
# OpenAlex Work id: W followed by digits, optionally with full URL prefix
_OPENALEX_RE = re.compile(r"^W\d{6,}$")
# PubMed id: pure digits (4-9 chars typically; bound to avoid mistaking too-short ints)
_PUBMED_RE = re.compile(r"^\d{4,9}$")


def detect_identifier_scheme(value: str) -> str | None:
    """Infer a scheme (``'doi'``, ``'arxiv'``, ``'s2'``, ``'pdfsha256'``,
    ``'openalex'``, ``'pubmed'``) from the shape of an identifier string.

    Returns ``None`` when the value matches no recognised pattern —
    callers should fall back to slug lookup at that point.

    Detection is conservative: ``S2:abc...`` and ``PMID:1234`` style
    explicit prefixes are stripped before matching, so callers can
    pass either ``'10.1103/PhysRevB.96.075447'`` (auto-detect DOI) or
    ``'PMID:12345678'`` (explicit prefix forces PubMed lookup).
    """
    if not value:
        return None
    v = value.strip()
    # arXiv DOI form: short-circuit to scheme='arxiv' so a query for
    # `10.48550/arXiv.1705.02630` resolves the same as `1705.02630`.
    # We also accept the URL-form `https://doi.org/10.48550/arXiv.X`.
    arxiv_doi = re.match(
        r"^(?:https?://(?:dx\.)?doi\.org/)?10\.48550/arxiv\.(.+)$", v, re.IGNORECASE
    )
    if arxiv_doi:
        return "arxiv"
    # Explicit prefixes win.
    if v.startswith(("S2:", "s2:")):
        return "s2"
    if v.startswith(("PMID:", "pmid:", "PubMed:", "pubmed:")):
        return "pubmed"
    if v.startswith(
        ("OpenAlex:", "openalex:", "W") if v[:1] in "Ww" else ("OpenAlex:", "openalex:")
    ):
        return "openalex"
    if v.startswith(("MAG:", "mag:")):
        return "mag"
    if v.startswith(("DBLP:", "dblp:")):
        return "dblp"
    low = v.lower()
    if low.startswith(
        (
            "doi:",
            "https://doi.org/",
            "http://doi.org/",
            "https://dx.doi.org/",
            "http://dx.doi.org/",
            "doi.org/",
        )
    ):
        return "doi"
    if low.startswith(
        (
            "arxiv:",
            "arxiv.org/abs/",
            "arxiv.org/pdf/",
            "https://arxiv.org/",
            "http://arxiv.org/",
        )
    ):
        return "arxiv"
    # Shape-based detection.
    if _DOI_RE.match(v):
        return "doi"
    if _ARXIV_NEW_RE.match(v) or _ARXIV_OLD_RE.match(v):
        return "arxiv"
    if _PDF_HASH_RE.match(v):
        return "pdfsha256"
    if _S2_RE.match(v):
        return "s2"
    if _OPENALEX_RE.match(v):
        return "openalex"
    if _PUBMED_RE.match(v):
        return "pubmed"
    return None


def _normalise_identifier(scheme: str, value: str) -> str:
    """Strip prefixes / URL wrappers and lowercase to canonical form.

    The ``ref_identifiers`` table stores values in canonical lowercase
    form; this helper produces the same form before INSERT or SELECT.
    Idempotent — calling on an already-canonical string is a no-op.
    """
    v = value.strip()
    if scheme == "doi":
        # Strip URL form
        v = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/)", "", v, flags=re.IGNORECASE)
        # Strip bare-host form and explicit DOI: prefix
        v = re.sub(r"^doi\.org/", "", v, flags=re.IGNORECASE)
        v = re.sub(r"^doi:\s*", "", v, flags=re.IGNORECASE)
    elif scheme == "arxiv":
        # arXiv DOI form (10.48550/arXiv.X) -> bare arxiv id
        m = re.match(
            r"^(?:https?://(?:dx\.)?doi\.org/)?10\.48550/arxiv\.(.+)$", v, re.IGNORECASE
        )
        if m:
            v = m.group(1)
        # Strip arxiv: prefix and abs/pdf URL forms
        v = re.sub(r"^arxiv:\s*", "", v, flags=re.IGNORECASE)
        v = re.sub(
            r"^(?:https?://)?arxiv\.org/(?:abs|pdf)/", "", v, flags=re.IGNORECASE
        )
        v = re.sub(r"\.pdf$", "", v, flags=re.IGNORECASE)
        # Strip versions (v1, v2, ...) — bare id is canonical
        v = re.sub(r"v\d+$", "", v)
    elif scheme == "s2":
        v = re.sub(r"^s2:\s*", "", v, flags=re.IGNORECASE)
    elif scheme == "pubmed":
        v = re.sub(r"^(?:pmid|pubmed):\s*", "", v, flags=re.IGNORECASE)
    elif scheme == "openalex":
        v = re.sub(r"^openalex:\s*", "", v, flags=re.IGNORECASE)
        v = re.sub(r"^https?://openalex\.org/", "", v, flags=re.IGNORECASE)
    elif scheme == "mag":
        v = re.sub(r"^mag:\s*", "", v, flags=re.IGNORECASE)
    elif scheme == "dblp":
        v = re.sub(r"^dblp:\s*", "", v, flags=re.IGNORECASE)
    return v.lower()


def _cleared_schemes(c: Connection, ref_id: int) -> set[str]:
    """Schemes marked cleared-by-a-human on the ref (empty set if none).

    ``FOR NO KEY UPDATE`` locks the refs row on the caller's conn so a
    concurrent ``clear_ref_identifier`` (DELETE + stamp) can't race the
    check-then-write callers — on a pool conn this is one implicit
    transaction, so the lock is only as wide as the read. NO KEY UPDATE,
    not UPDATE: a FOR UPDATE lock conflicts with the KEY SHARE that FK
    inserts (chunks, ref_events, ref_identifiers) take on the refs row,
    which is a deadlock shape — db_writer's upgrade-path lock comment
    documents the same constraint.
    """
    row = c.execute(
        "SELECT meta->%s FROM refs WHERE ref_id = %s FOR NO KEY UPDATE",
        (CLEARED_IDENTIFIERS_META_KEY, ref_id),
    ).fetchone()
    marked = row[0] if row else None
    if not isinstance(marked, dict):
        return set()
    return set(marked)


def _stamp_cleared(
    c: Connection,
    ref_id: int,
    scheme: str,
    source: str,
    values: Iterable[str] = (),
) -> None:
    """Record ``scheme`` as human-cleared on ``refs.meta`` (gr515246).

    The cleared ``id_value``s go in the marker too (``"values"``) — a
    merge can then tell "the survivor holds the very identifier the
    human removed" apart from "the survivor holds its own value of the
    same scheme" and keep the latter. Scheme-only entries from before
    this field existed stay valid (treated as value-unknown).

    Also drops the paper's ``meta.<scheme>`` copy — a paper's DOI/arXiv
    surfaces both in ``ref_identifiers`` and as a top-level meta key
    (``_render_overview`` reads it), so deleting only the alias row
    leaves a ghost. Scoped to ``kind='paper'``: on other kinds
    ``meta.doi`` names *another* object entirely (a provenance ref's
    ``meta.doi`` is the retracted paper's).
    """
    extra = Jsonb({"values": list(values)} if values else {})
    c.execute(
        "UPDATE refs SET meta = jsonb_set(coalesce(meta, '{}'::jsonb), "
        "  '{cleared_identifiers}', "
        "  coalesce(meta->'cleared_identifiers', '{}'::jsonb) "
        "  || jsonb_build_object(%s::text, "
        "      jsonb_build_object('at', now(), 'by', %s::text) || %s::jsonb)) "
        "WHERE ref_id = %s "
        "  AND (meta->'cleared_identifiers' IS NULL "
        "       OR jsonb_typeof(meta->'cleared_identifiers') = 'object')",
        (scheme, source, extra, ref_id),
    )
    if scheme in ("doi", "arxiv"):
        c.execute(
            "UPDATE refs SET meta = meta - %s::text "
            "WHERE ref_id = %s AND kind = 'paper' AND meta ? %s::text",
            (scheme, ref_id, scheme),
        )


def _unstamp_cleared(c: Connection, ref_id: int, scheme: str) -> None:
    """Lift the human-cleared mark for ``scheme`` — an explicit re-add."""
    c.execute(
        "UPDATE refs SET meta = CASE "
        "  WHEN coalesce(meta->'cleared_identifiers', '{}'::jsonb) - %s::text "
        "       = '{}'::jsonb "
        "       THEN meta - 'cleared_identifiers' "
        "       ELSE jsonb_set(meta, '{cleared_identifiers}', "
        "                      (meta->'cleared_identifiers') - %s::text) END "
        "WHERE ref_id = %s "
        "  AND jsonb_typeof(meta->'cleared_identifiers') = 'object' "
        "  AND meta->'cleared_identifiers' ? %s::text",
        (scheme, scheme, ref_id, scheme),
    )


class IdentifiersMixin:
    """Cross-scheme alias lookup over the ``ref_identifiers`` table."""

    pool: ConnectionPool

    def find_ref_by_identifier(
        self,
        scheme: str,
        value: str,
        *,
        kind: str | None = None,
    ) -> int | None:
        """Look up a ref id by ``(scheme, value)``.

        ``scheme`` and ``value`` are normalised (lowercased, prefixes
        stripped) before the query so the caller can pass user-form
        identifiers like ``'https://doi.org/10.1234/...'`` directly.

        ``kind=`` filters via JOIN to ``refs.kind``. Pass ``'paper'``
        for paper-specific lookup; ``None`` matches any ref kind.

        Returns ``None`` if no live ref carries this identifier.
        """
        s = scheme.strip().lower()
        v = _normalise_identifier(s, value)
        if not v:
            return None
        sql = (
            "SELECT pi.ref_id "
            "FROM ref_identifiers pi "
            "JOIN refs r ON r.ref_id = pi.ref_id "
            "WHERE pi.id_kind = %s AND pi.id_value = %s "
            "AND r.retired_at IS NULL"
        )
        params: list[object] = [s, v]
        if kind is not None:
            sql += " AND r.kind = %s"
            params.append(kind)
        sql += " LIMIT 1"
        with self.pool.connection() as conn:
            row = conn.execute(sql, params).fetchone()
        return int(row[0]) if row is not None else None

    def find_paper_ref_by_identifier(self, value: str) -> int | None:
        """Look up a paper ref by ANY known identifier — DOI, arXiv,
        S2 paperId, PubMed, OpenAlex, MAG, DBLP, pdf_hash.

        Scheme is auto-detected from the value shape via
        :func:`detect_identifier_scheme`. URL forms and explicit
        prefixes (``DOI:``, ``S2:``, ``PMID:``, etc.) are stripped.

        Returns the ``refs.id`` of the matching paper, or ``None``
        when no scheme matches or no ref carries this identifier.

        This is the generalised form behind the narrower, still-live
        DOI-only convenience wrapper
        :meth:`RefsMixin.find_paper_slug_by_doi`, which delegates here.
        """
        scheme = detect_identifier_scheme(value)
        if scheme is None:
            return None
        return self.find_ref_by_identifier(scheme, value, kind="paper")

    def find_paper_refs_by_identifiers_bulk(
        self, values: Iterable[str]
    ) -> dict[str, int]:
        """Bulk twin of :meth:`find_paper_ref_by_identifier` — resolve many
        raw identifier strings (a mixed bag of DOIs / arXiv ids / …) in a
        BOUNDED number of queries (one per distinct detected scheme, so
        ~1-2 for a typical DOI+arXiv batch) instead of one round trip per
        value. Used by the Semantic Scholar corpus-diff render
        (docs/backlog/discovery-exclude-by-container.md), which otherwise
        paid N ``find_paper_ref_by_identifier`` calls for N fetched hits.

        Returns ``{original_value: ref_id}`` for every input that both
        scheme-detects (:func:`detect_identifier_scheme`) and resolves to a
        live paper; values that don't detect or don't match are simply
        absent from the result — same "quiet miss" contract as the
        singular lookup, just batched. When two distinct raw values
        normalise to the same ``(scheme, value)`` pair, only the last one
        (input order) keeps its own key in the group — degenerate in
        practice (callers pass distinct hit identifiers).
        """
        by_scheme: dict[str, dict[str, str]] = {}
        for raw in values:
            if not raw:
                continue
            scheme = detect_identifier_scheme(raw)
            if scheme is None:
                continue
            norm = _normalise_identifier(scheme, raw)
            if not norm:
                continue
            by_scheme.setdefault(scheme, {})[norm] = raw
        if not by_scheme:
            return {}
        out: dict[str, int] = {}
        with self.pool.connection() as conn:
            for scheme, norm_to_raw in by_scheme.items():
                rows = conn.execute(
                    "SELECT pi.id_value, pi.ref_id "
                    "FROM ref_identifiers pi "
                    "JOIN refs r ON r.ref_id = pi.ref_id "
                    "WHERE pi.id_kind = %s AND pi.id_value = ANY(%s) "
                    "AND r.kind = 'paper' AND r.retired_at IS NULL",
                    (scheme, list(norm_to_raw)),
                ).fetchall()
                for id_value, ref_id in rows:
                    raw_value = norm_to_raw.get(str(id_value))
                    if raw_value is not None:
                        out[raw_value] = int(ref_id)
        return out

    def insert_ref_identifiers(
        self,
        ref_id: int,
        identifiers: Iterable[tuple[str, str, str]],
        *,
        conn: Connection | None = None,
    ) -> int:
        """Bulk-insert alias rows for ``ref_id``.

        ``identifiers`` is an iterable of ``(scheme, value, source)``
        triples. Empty values are silently dropped. ``ON CONFLICT DO
        NOTHING`` — pre-existing rows for a ``(scheme, value)`` win,
        which is the conflict policy documented in the migration:
        first-write-wins, second-write surfaces in
        ``ref_identifier_conflicts``.

        Returns the number of rows actually written. ``conn=`` lets
        the caller share a transaction with the parent ``ingest`` flow.

        A scheme the operator deliberately cleared
        (:data:`CLEARED_IDENTIFIERS_META_KEY`, gr515246) is skipped for
        machine-sourced rows — bulk sweeps may not resurrect it; a
        human-sourced row both lands and lifts the mark.
        """
        rows: list[tuple[str, str, int, str]] = []
        for scheme, value, source in identifiers:
            s = scheme.strip().lower()
            v = _normalise_identifier(s, value)
            if not s or not v:
                continue
            rows.append((s, v, ref_id, source.strip().lower() or "manual"))
        if not rows:
            return 0
        sql = (
            "INSERT INTO ref_identifiers (id_kind, id_value, ref_id, source) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING"
        )

        def _do(c: Connection) -> int:
            marked = _cleared_schemes(c, ref_id)
            human: set[str] = set()
            kept: list[tuple[str, str, int, str]] = []
            for row in rows:
                if row[0] in marked and row[3] not in _HUMAN_EDIT_SOURCES:
                    log.info(
                        "ref_identifiers: skipping %s=%s on #%s — "
                        "cleared by a human edit (gr515246)",
                        row[0],
                        row[1],
                        ref_id,
                    )
                    continue
                if row[0] in marked:
                    human.add(row[0])
                kept.append(row)
            if not kept:
                return 0
            with c.cursor() as cur:
                cur.executemany(sql, kept)
                written = cur.rowcount or 0
            for s in human:
                # Lift the mark only when the human row actually landed —
                # a value already owned by another ref loses the
                # ON CONFLICT, and unmarking then would let the next
                # machine sweep resurrect the cleared scheme.
                landed = c.execute(
                    "SELECT 1 FROM ref_identifiers WHERE ref_id = %s AND id_kind = %s",
                    (ref_id, s),
                ).fetchone()
                if landed is not None:
                    _unstamp_cleared(c, ref_id, s)
            return written

        if conn is not None:
            return _do(conn)
        with self.pool.connection() as c:
            return _do(c)

    def set_ref_identifier(
        self,
        ref_id: int,
        scheme: str,
        value: str,
        *,
        source: str = "web-edit",
        conn: Connection | None = None,
    ) -> bool:
        """Set (replace) this ref's identifier for ``scheme`` to ``value``.

        Operator correction path for the web metadata editor. Unlike
        :meth:`insert_ref_identifiers` (first-write-wins, ``ON CONFLICT
        DO NOTHING``), this *replaces* the ref's own ``(ref_id,
        scheme)`` rows so fixing a wrong DOI / arXiv id actually takes.
        Scoped to this ref's rows — never touches another ref's
        aliases.

        Blank ``value`` is a no-op (blank field = keep existing).
        Returns True when a row was written. Raises :class:`BadInput`
        if ``value`` is already claimed by a *different* **live** ref (the
        cross-ref uniqueness conflict) rather than silently dropping
        this ref's old alias. A conflicting owner that is **soft-deleted**
        is not a real conflict: it has no live claim on the value (and
        nothing can load it to merge against — see ``merge_refs``), so we
        reclaim its orphaned row for this ref. This covers the bare-delete
        path (the ``🗑 Delete paper`` button soft-deletes the ref but, unlike
        ``merge_refs``, leaves its ``ref_identifiers`` rows), which would
        otherwise wedge the survivor's DOI / arXiv id permanently.

        A scheme marked human-cleared
        (:data:`CLEARED_IDENTIFIERS_META_KEY`, gr515246) refuses
        *machine*-sourced writes — ``False`` with a log line, so a
        backfill/enrich pass leaves the clear alone instead of
        re-deriving the identifier the operator removed. A human-sourced
        set lands and lifts the mark.
        """
        from precis.errors import BadInput

        s = (scheme or "").strip().lower()
        v = _normalise_identifier(s, value)
        if not s or not v:
            return False
        src = (source or "manual").strip().lower()

        def _do(c: Connection) -> bool:
            marked = _cleared_schemes(c, ref_id)
            if s in marked and src not in _HUMAN_EDIT_SOURCES:
                log.info(
                    "set_ref_identifier: refusing %s=%s on #%s — "
                    "cleared by a human edit (gr515246)",
                    s,
                    v,
                    ref_id,
                )
                return False
            owner = c.execute(
                "SELECT ri.ref_id, r.retired_at IS NOT NULL "
                "FROM ref_identifiers ri "
                "JOIN refs r ON r.ref_id = ri.ref_id "
                "WHERE ri.id_kind = %s AND ri.id_value = %s",
                (s, v),
            ).fetchone()
            if owner is not None:
                owner_id, owner_deleted = int(owner[0]), bool(owner[1])
                if owner_id == ref_id:
                    return False  # already ours — nothing to do
                if not owner_deleted:
                    raise BadInput(
                        f"{s}={v!r} already belongs to ref id={owner_id}",
                        next=(
                            "a merge_duplicate cleanup is needed — see "
                            "get(kind='skill', id='precis-paper-help') §resolving "
                            "a duplicate ref; prefer the ref with chunks/body as "
                            "the survivor"
                        ),
                    )
                # Orphan from a soft-deleted ref — reclaim it. The PK is
                # (id_kind, id_value), so the stale row must go before the
                # INSERT below can land.
                c.execute(
                    "DELETE FROM ref_identifiers WHERE id_kind = %s AND id_value = %s",
                    (s, v),
                )
            c.execute(
                "DELETE FROM ref_identifiers WHERE ref_id = %s AND id_kind = %s",
                (ref_id, s),
            )
            c.execute(
                "INSERT INTO ref_identifiers (id_kind, id_value, ref_id, source) "
                "VALUES (%s, %s, %s, %s)",
                (s, v, ref_id, src),
            )
            if s in marked:
                # Reached only for a human-sourced write (the machine
                # branch returned above) — an explicit re-add lifts the
                # clear mark so automated sweeps manage it again.
                _unstamp_cleared(c, ref_id, s)
            return True

        if conn is not None:
            return _do(conn)
        with self.pool.connection() as c:
            return _do(c)

    def clear_ref_identifier(
        self,
        ref_id: int,
        scheme: str,
        *,
        source: str = "web-edit",
        conn: Connection | None = None,
    ) -> bool:
        """Delete this ref's own ``(scheme)`` identifier row, if any.

        gr353804: the only door to fix a wrong identifier was
        :meth:`set_ref_identifier`, which can only *replace* a value —
        there was no way to remove one outright (e.g. an SI record that
        must not carry any DOI once its parent owns the correct one).
        Scoped to rows this ref itself owns via the ``ref_id`` filter in
        the ``DELETE`` — it can never touch another ref's row for the
        same value, so there is no collision case to refuse here (unlike
        :meth:`set_ref_identifier`, which writes a new value that might
        already belong to someone else).

        Returns True when a row was actually removed, False when this
        ref had no identifier for ``scheme`` (a clean no-op, not an
        error). The one False→True exception: a *human* clear of a
        ``doi``/``arxiv`` ghost — no identifier row, but the paper's
        ``meta.<scheme>`` copy still shows the value on the Meta tab.
        Blanking that field must stamp the marker and drop the ghost,
        exactly like clearing a real row. An empty or null
        ``meta.<scheme>`` is no ghost — nothing is displayed, so the
        clear stays a no-op (no marker either, or a future sweep would
        be blocked from filling a field the human never saw).

        A human-sourced clear additionally stamps the
        :data:`CLEARED_IDENTIFIERS_META_KEY` marker (and drops the
        paper's ``meta.<scheme>`` copy) so automated identifier writers
        don't re-derive what the operator just removed (gr515246).
        """
        s = (scheme or "").strip().lower()
        if not s:
            return False
        src = (source or "manual").strip().lower()

        def _do(c: Connection) -> bool:
            # Lock the refs row first — the same order every writer
            # takes (refs, then ref_identifiers), so a clear can't
            # deadlock against a set that locked refs via
            # _cleared_schemes before touching ref_identifiers.
            _cleared_schemes(c, ref_id)
            deleted = [
                str(r[0])
                for r in c.execute(
                    "DELETE FROM ref_identifiers "
                    "WHERE ref_id = %s AND id_kind = %s RETURNING id_value",
                    (ref_id, s),
                ).fetchall()
            ]
            if not deleted:
                if src in _HUMAN_EDIT_SOURCES and s in ("doi", "arxiv"):
                    ghost = c.execute(
                        "SELECT meta->>%s::text FROM refs WHERE ref_id = %s "
                        "AND kind = 'paper' "
                        "AND nullif(meta->>%s::text, '') IS NOT NULL",
                        (s, ref_id, s),
                    ).fetchone()
                    if ghost is not None:
                        v = _normalise_identifier(s, str(ghost[0]))
                        _stamp_cleared(c, ref_id, s, src, values=[v] if v else [])
                        return True
                return False
            if src in _HUMAN_EDIT_SOURCES:
                _stamp_cleared(c, ref_id, s, src, values=deleted)
            return True

        if conn is not None:
            return _do(conn)
        with self.pool.connection() as c:
            return _do(c)

    def suggest_cite_key(
        self,
        authors: Any,
        year: int | None,
        *,
        exclude_ref_id: int | None = None,
        conn: Connection | None = None,
    ) -> str:
        """Suggest a free ``cite_key`` for ``authors`` + ``year``.

        Mirrors the ingest-time minting (:func:`precis.identity.make_cite_key`
        + the prefix-collision probe in ``db_writer.resolve_cite_key``): the
        bare ``surname<yy>`` form when free, else the next ``a``..``z`` suffix.
        ``exclude_ref_id`` drops that ref's own current cite_key from the
        taken set so re-suggesting for the paper being edited is stable
        (it never collides with itself). Returns ``""`` only when authors
        are unknown enough that the base would be the ``anon`` placeholder.
        """
        from precis.identity import make_cite_key

        base = make_cite_key(authors, year)  # bare surname+yy prefix
        if base.startswith("anon"):
            return ""

        def _do(c: Connection) -> str:
            rows = c.execute(
                "SELECT id_value FROM ref_identifiers "
                "WHERE id_kind = 'cite_key' AND id_value LIKE %s",
                (base + "%",),
            ).fetchall()
            taken = {str(r[0]) for r in rows}
            if exclude_ref_id is not None:
                own = c.execute(
                    "SELECT id_value FROM ref_identifiers "
                    "WHERE id_kind = 'cite_key' AND ref_id = %s",
                    (exclude_ref_id,),
                ).fetchone()
                if own is not None:
                    taken.discard(str(own[0]))
            return make_cite_key(authors, year, taken=taken)

        if conn is not None:
            return _do(conn)
        with self.pool.connection() as c:
            return _do(c)

    def ref_cite_keys(
        self,
        ref_id: int,
        *,
        conn: Connection | None = None,
    ) -> list[str]:
        """All ``cite_key`` aliases for ``ref_id`` (oldest first).

        A paper commonly carries more than one ``cite_key`` — e.g. its
        author-year key plus a book's own bib key registered additively
        by ``tex-import`` (:mod:`precis.draftimport.resolve`). The on-disk
        PDF is filed under *whichever* key the fetcher chose as its
        filename stem, which need not be the one the display slug resolves
        to. Callers that address the file by cite_key (the web PDF
        resolver) must therefore try every alias, not just ``ref.slug``.
        """

        def _do(c: Connection) -> list[str]:
            rows = c.execute(
                "SELECT id_value FROM ref_identifiers "
                "WHERE ref_id = %s AND id_kind = 'cite_key' "
                "ORDER BY created_at, id_value",
                (ref_id,),
            ).fetchall()
            return [str(r[0]) for r in rows]

        if conn is not None:
            return _do(conn)
        with self.pool.connection() as c:
            return _do(c)

    def ref_cite_keys_bulk(self, ref_ids: Iterable[int]) -> dict[int, list[str]]:
        """Batch twin of :meth:`ref_cite_keys` — every ``cite_key`` alias
        (oldest first) for many refs in one query instead of one per ref.

        A claim hub's N supporters used to cost N ``ref_cite_keys`` round
        trips — twice over, once for the render and once more when
        ``claim_trust`` re-derived the same hub (OPEN-ITEMS.md "/smartdraft
        reader" batch B). A missing ref_id is simply absent from the
        returned dict (its aliases are ``[]``, same as an empty
        :meth:`ref_cite_keys` call)."""
        ids = list({int(r) for r in ref_ids})
        if not ids:
            return {}
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT ref_id, id_value FROM ref_identifiers "
                "WHERE ref_id = ANY(%s) AND id_kind = 'cite_key' "
                "ORDER BY ref_id, created_at, id_value",
                (ids,),
            ).fetchall()
        out: dict[int, list[str]] = {}
        for ref_id, value in rows:
            out.setdefault(int(ref_id), []).append(str(value))
        return out

    def identifier_owner(
        self,
        scheme: str,
        value: str,
        *,
        include_deleted: bool = False,
        conn: Connection | None = None,
    ) -> int | None:
        """Return the ref_id that owns ``(scheme, value)``, or ``None``.

        Normalises ``value`` exactly as :meth:`set_ref_identifier` does, so
        a caller can detect the cross-ref conflict that ``set_ref_identifier``
        would raise on — without raising. Skips soft-deleted refs unless
        ``include_deleted``. Used by the dedup paths to find the canonical
        ref a re-derived DOI already belongs to.
        """
        s = (scheme or "").strip().lower()
        v = _normalise_identifier(s, value)
        if not s or not v:
            return None
        sql = (
            "SELECT ri.ref_id FROM ref_identifiers ri "
            "JOIN refs r ON r.ref_id = ri.ref_id "
            "WHERE ri.id_kind = %s AND ri.id_value = %s"
        )
        if not include_deleted:
            sql += " AND r.retired_at IS NULL"
        sql += " LIMIT 1"

        def _do(c: Connection) -> int | None:
            row = c.execute(sql, (s, v)).fetchone()
            return int(row[0]) if row else None

        if conn is not None:
            return _do(conn)
        with self.pool.connection() as c:
            return _do(c)

    def identifiers_for_refs(
        self,
        ref_ids: list[int],
    ) -> dict[int, dict[str, str]]:
        """Batch alias lookup: ``{ref_id: {scheme: value, ...}}``.

        One query over ``ref_identifiers`` for many refs — the display
        path (web papers list / hover card) needs DOI / arXiv links
        for a page of results without N round-trips. When a ref carries
        more than one value for a scheme, the first by ``id_value``
        order wins (deterministic; DOIs are effectively unique per ref
        anyway). Schemes are returned verbatim (``'doi'``, ``'arxiv'``,
        ``'s2'``, …) so callers pick what they want.
        """
        if not ref_ids:
            return {}
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT ref_id, id_kind, id_value FROM ref_identifiers "
                "WHERE ref_id = ANY(%s) ORDER BY ref_id, id_kind, id_value",
                (list(ref_ids),),
            ).fetchall()
        out: dict[int, dict[str, str]] = {}
        for ref_id, scheme, value in rows:
            bucket = out.setdefault(int(ref_id), {})
            # First value per scheme wins (rows are ordered by id_value).
            bucket.setdefault(str(scheme), str(value))
        return out

    def current_pub_ids(self, ref_ids: Iterable[int]) -> dict[int, str]:
        """Batch lookup of each ref's **current** ``pub_id`` alias.

        A hub can carry more than one ``id_kind='pub_id'`` row —
        :func:`~precis.taproot.hub.refine_claim_sentence` INSERTs a new
        one on every reworded sentence and deliberately keeps the old
        one so a draft already citing it keeps resolving (see that
        function's docstring). ``created_at`` is the only column that
        orders them — there's no serial id on ``ref_identifiers`` — and
        it's reliable here because the INSERT always happens at write
        time with ``DEFAULT now()``, never backdated or bulk-loaded out
        of order for this id_kind. ``DISTINCT ON (ref_id) ... ORDER BY
        ref_id, created_at DESC`` therefore picks the most-recently
        minted pub_id, i.e. the hub's live version.

        Used both to stamp a fresh ``cites`` edge
        (``handlers/draft.py::sync_draft_links``) and to detect drift on
        an existing one (``draft/lint.py::find_drifted_cites``)
        — both need "the version right now", not "a version". Missing
        ids (no ``pub_id`` yet — not citable) are simply absent from the
        result.
        """
        ids = list({int(r) for r in ref_ids})
        if not ids:
            return {}
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT DISTINCT ON (ref_id) ref_id, id_value "
                "FROM ref_identifiers "
                "WHERE ref_id = ANY(%s) AND id_kind = 'pub_id' "
                "ORDER BY ref_id, created_at DESC",
                (ids,),
            ).fetchall()
        return {int(r[0]): str(r[1]) for r in rows}

    def list_ref_identifiers(
        self,
        ref_id: int,
    ) -> list[tuple[str, str, str]]:
        """Return ``[(scheme, value, source), ...]`` for a ref.

        Used by display surfaces that want to show every known alias
        for a paper, and by the ingest path to verify what was
        written. Sorted by scheme then value for stable output.
        """
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT id_kind, id_value, source FROM ref_identifiers "
                "WHERE ref_id = %s ORDER BY id_kind, id_value",
                (ref_id,),
            ).fetchall()
        return [(str(r[0]), str(r[1]), str(r[2] or "")) for r in rows]


__all__ = [
    "IdentifiersMixin",
    "detect_identifier_scheme",
]
