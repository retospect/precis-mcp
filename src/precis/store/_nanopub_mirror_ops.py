"""Registry-mirror ops (``nanopub_mirror`` + ``nanopub_mirror_edges``,
migration 0130) — the store half of :mod:`precis.nanopub.mirror`.

A cache of *other people's* frozen artifacts, not our proof store: no
append-only guarantee. The one write invariant lives in
:meth:`NanopubMirrorMixin.mirror_upsert`: a row that already passed
trusty verification is never overwritten (its bytes are content-addressed
truth); an unverified row may be replaced by a re-fetch.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from psycopg.types.json import Jsonb

_MIRROR_COLS = (
    "artifact_code, trig_bytes, byte_sha256, source_url, fetched_at, "
    "verified, aida_uri, signer, key_fingerprint, dois, "
    "assertion_predicates, retracted_by, superseded_by"
)


@dataclass(frozen=True, slots=True)
class MirrorRow:
    artifact_code: str
    trig_bytes: bytes
    byte_sha256: str
    source_url: str
    fetched_at: datetime
    verified: bool
    aida_uri: str | None
    signer: str | None
    key_fingerprint: str | None
    dois: list[str]
    assertion_predicates: list[str]
    retracted_by: str | None
    superseded_by: str | None


def _row_to_mirror(row: tuple[Any, ...]) -> MirrorRow:
    return MirrorRow(
        artifact_code=str(row[0]),
        trig_bytes=bytes(row[1]),
        byte_sha256=str(row[2]),
        source_url=str(row[3]),
        fetched_at=row[4],
        verified=bool(row[5]),
        aida_uri=row[6],
        signer=row[7],
        key_fingerprint=row[8],
        dois=list(row[9] or []),
        assertion_predicates=list(row[10] or []),
        retracted_by=row[11],
        superseded_by=row[12],
    )


class NanopubMirrorMixin:
    """Mixin: assumes the concrete Store provides ``self.pool``."""

    pool: Any

    def mirror_codes(self) -> set[str]:
        """Every artifact code already mirrored — the PK diff against the
        registry list IS the sync cursor (resumable by construction)."""
        with self.pool.connection() as conn:
            rows = conn.execute("SELECT artifact_code FROM nanopub_mirror").fetchall()
        return {str(r[0]) for r in rows}

    def mirror_row(self, artifact_code: str) -> MirrorRow | None:
        with self.pool.connection() as conn:
            row = conn.execute(
                f"SELECT {_MIRROR_COLS} FROM nanopub_mirror WHERE artifact_code = %s",
                (artifact_code,),
            ).fetchone()
        return _row_to_mirror(row) if row else None

    def mirror_upsert(
        self,
        artifact_code: str,
        *,
        trig_bytes: bytes,
        source_url: str,
        verified: bool,
        aida_uri: str | None = None,
        signer: str | None = None,
        key_fingerprint: str | None = None,
        dois: list[str] | None = None,
        assertion_predicates: list[str] | None = None,
    ) -> bool:
        """Insert or replace one mirrored artifact. A row that already
        passed verification is content-addressed truth and is never
        overwritten (returns ``False``); an unverified row may be
        replaced by a re-fetch."""
        with self.pool.connection() as conn:
            cur = conn.execute(
                """
                INSERT INTO nanopub_mirror
                    (artifact_code, trig_bytes, source_url, verified,
                     aida_uri, signer, key_fingerprint, dois,
                     assertion_predicates)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (artifact_code) DO UPDATE SET
                    trig_bytes = EXCLUDED.trig_bytes,
                    source_url = EXCLUDED.source_url,
                    fetched_at = now(),
                    verified = EXCLUDED.verified,
                    aida_uri = EXCLUDED.aida_uri,
                    signer = EXCLUDED.signer,
                    key_fingerprint = EXCLUDED.key_fingerprint,
                    dois = EXCLUDED.dois,
                    assertion_predicates = EXCLUDED.assertion_predicates
                WHERE nanopub_mirror.verified = FALSE
                """,
                (
                    artifact_code,
                    trig_bytes,
                    source_url,
                    verified,
                    aida_uri,
                    signer,
                    key_fingerprint,
                    Jsonb(dois or []),
                    Jsonb(assertion_predicates or []),
                ),
            )
            return cur.rowcount == 1

    def mirror_replace_edges(
        self, from_code: str, edges: list[tuple[str, str]]
    ) -> None:
        """Idempotent re-index of one artifact's outbound np→np edges:
        DELETE + INSERT (``(to_code, relation)`` pairs) — extracts are
        rebuildable, so replacement is the update model."""
        with self.pool.connection() as conn:
            conn.execute(
                "DELETE FROM nanopub_mirror_edges WHERE from_code = %s",
                (from_code,),
            )
            for to_code, relation in edges:
                conn.execute(
                    "INSERT INTO nanopub_mirror_edges (from_code, to_code, "
                    "relation) VALUES (%s, %s, %s) "
                    "ON CONFLICT (from_code, to_code, relation) DO NOTHING",
                    (from_code, to_code, relation),
                )

    def mirror_edges_to(self, to_code: str) -> list[tuple[str, str]]:
        """Inbound edges on one code: ``(from_code, relation)`` pairs."""
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT from_code, relation FROM nanopub_mirror_edges "
                "WHERE to_code = %s ORDER BY from_code",
                (to_code,),
            ).fetchall()
        return [(str(r[0]), str(r[1])) for r in rows]

    def mirror_apply_flags(self) -> int:
        """Derive ``retracted_by``/``superseded_by`` from the edge table
        under the authoritative-retraction rule: BOTH sides must be
        *verified* (an unverified target's extracted signer came from
        untrusted bytes — a spurious signer match must not flag it) and
        the flagging artifact signed by the same signer as its target.
        Idempotent; returns rows newly flagged."""
        flagged = 0
        for relation, column in (
            ("retracts", "retracted_by"),
            ("supersedes", "superseded_by"),
        ):
            with self.pool.connection() as conn:
                cur = conn.execute(
                    f"""
                    UPDATE nanopub_mirror t
                       SET {column} = e.from_code
                      FROM nanopub_mirror_edges e
                      JOIN nanopub_mirror f ON f.artifact_code = e.from_code
                     WHERE e.relation = %s
                       AND e.to_code = t.artifact_code
                       AND f.verified
                       AND t.verified
                       AND f.signer IS NOT NULL
                       AND f.signer = t.signer
                       AND t.{column} IS NULL
                    """,
                    (relation,),
                )
                flagged += cur.rowcount
        return flagged

    def mirror_aida_matches(self, aida_uris: list[str]) -> list[MirrorRow]:
        """Verified mirror rows whose AIDA URI is one of the given
        variants (caller canonicalises encodings — both ``%20`` and ``+``
        live in the wild)."""
        if not aida_uris:
            return []
        with self.pool.connection() as conn:
            rows = conn.execute(
                f"SELECT {_MIRROR_COLS} FROM nanopub_mirror "
                "WHERE verified AND aida_uri = ANY(%s)",
                (aida_uris,),
            ).fetchall()
        return [_row_to_mirror(r) for r in rows]

    def mirror_stats(self) -> dict[str, int]:
        with self.pool.connection() as conn:
            row = conn.execute(
                """
                SELECT count(*),
                       count(*) FILTER (WHERE verified),
                       count(*) FILTER (WHERE retracted_by IS NOT NULL),
                       count(*) FILTER (WHERE superseded_by IS NOT NULL),
                       count(*) FILTER (WHERE aida_uri IS NOT NULL)
                  FROM nanopub_mirror
                """
            ).fetchone()
        assert row is not None
        return {
            "total": int(row[0]),
            "verified": int(row[1]),
            "retracted": int(row[2]),
            "superseded": int(row[3]),
            "with_aida": int(row[4]),
        }
