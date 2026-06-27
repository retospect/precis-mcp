"""Cross-scheme identifier alias lookup. Mixin on :class:`precis.store.Store`.

A single paper can carry many identifiers — DOI (arXiv DOI form *and*
journal DOI), arXiv id, Semantic Scholar paperId, PubMed id, MAG id,
OpenAlex id, DBLP, plus the local ``pdf_hash`` content fingerprint.
Migration ``0009_ref_identifiers.sql`` introduces the
``ref_identifiers`` table that holds one row per ``(scheme, value)``
mapping into ``refs.id``. This mixin provides:

* :meth:`find_ref_by_identifier`        — generic alias lookup (by scheme+value).
* :meth:`find_paper_ref_by_identifier`  — paper-specific, scheme auto-detected from value shape.
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
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from psycopg import Connection
    from psycopg_pool import ConnectionPool


# Identifier shape detection -------------------------------------------------
#
# Each pattern recognises a value form unambiguously enough that we can
# infer the scheme when the caller passes a bare identifier string
# (e.g. an agent doing `get(kind='paper', id='1705.02630')`). Order of
# checking matters: more-specific patterns first.

_DOI_RE = re.compile(r"^10\.\d{4,9}/.+", re.IGNORECASE)
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
    if v.startswith(
        ("DOI:", "doi:", "https://doi.org/", "http://doi.org/", "https://dx.doi.org/")
    ):
        return "doi"
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
        # Strip explicit DOI: prefix
        v = re.sub(r"^doi:\s*", "", v, flags=re.IGNORECASE)
    elif scheme == "arxiv":
        # arXiv DOI form (10.48550/arXiv.X) -> bare arxiv id
        m = re.match(
            r"^(?:https?://(?:dx\.)?doi\.org/)?10\.48550/arxiv\.(.+)$", v, re.IGNORECASE
        )
        if m:
            v = m.group(1)
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
            "AND r.deleted_at IS NULL"
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

        This is the generalised replacement for the legacy
        :meth:`RefsMixin.find_paper_slug_by_doi` — that method now
        delegates here.
        """
        scheme = detect_identifier_scheme(value)
        if scheme is None:
            return None
        return self.find_ref_by_identifier(scheme, value, kind="paper")

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
        if conn is not None:
            cur = conn.cursor()
            cur.executemany(sql, rows)
            return cur.rowcount or 0
        with self.pool.connection() as c:
            cur = c.cursor()
            cur.executemany(sql, rows)
            return cur.rowcount or 0

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
        """
        from precis.errors import BadInput

        s = (scheme or "").strip().lower()
        v = _normalise_identifier(s, value)
        if not s or not v:
            return False

        def _do(c: Connection) -> bool:
            owner = c.execute(
                "SELECT ri.ref_id, r.deleted_at IS NOT NULL "
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
                        next="resolve the duplicate before reassigning the identifier",
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
                (s, v, ref_id, (source or "manual").strip().lower()),
            )
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
            sql += " AND r.deleted_at IS NULL"
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
