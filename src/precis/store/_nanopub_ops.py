"""Nanopub publish-state + proof-store CRUD (migration 0128).

Mixin on :class:`precis.store.Store`. Mechanical row access only — state-
machine *legality*, mint gates, and artifact assembly live in
:mod:`precis.nanopub`; this layer enforces just the invariants the DB
schema states (one non-terminal publish row per hub via the partial
unique index; append-only proof-store tables via triggers — an UPDATE or
DELETE against them raises from Postgres, deliberately uncatchable here).

Tables: ``nanopub_publish`` (the one mutable working row per hub),
``nanopub_artifacts`` (append-only exact signed bytes + indexed
extracts), ``nanopub_ots_batches`` / ``nanopub_ots_leaves`` /
``nanopub_ots_proofs`` (append-only Merkle batch family; an upgrade
INSERTs a new proof row, never rewrites the pending one).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from psycopg.types.json import Jsonb

#: Publish states with no live claim on the hub — the partial unique
#: index `nanopub_publish_one_live_per_hub` excludes exactly these.
TERMINAL_STATES = ("superseded", "retracted", "rejected")


@dataclass(frozen=True, slots=True)
class PublishRow:
    """One ``nanopub_publish`` row (see migration 0128 for column docs)."""

    id: int
    claim_ref_id: int
    artifact_type: str
    approved_title: str | None
    claim_sha: str | None
    aida_uri: str | None
    #: Frozen payload envelope: ``{"passages": [...], "fields": {...},
    #: "motivation": str, "testable_by": str}`` — see
    #: :class:`precis.nanopub.assemble.MintInput`.
    grounding: dict[str, Any]
    dependency_codes: dict[str, Any]
    trusty_uri: str | None
    artifact_id: int | None
    batch_id: int | None
    state: str
    created_at: datetime
    updated_at: datetime
    #: Set once by the registry POST (slice 5) — the point of no return.
    published_at: datetime | None = None
    registry_url: str | None = None


@dataclass(frozen=True, slots=True)
class ArtifactRow:
    """One append-only ``nanopub_artifacts`` row. ``trig_bytes`` is the
    authority; every other column is a rebuildable extract."""

    id: int
    publish_id: int
    claim_ref_id: int
    artifact_type: str
    trig_bytes: bytes
    byte_sha256: str
    trusty_uri: str
    aida_uri: str
    claim_sha: str
    signer: str
    key_fingerprint: str
    dois: list[str]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class AllowlistEntry:
    """One ``nanopub_trust_allowlist`` row (migration 0129): an explicit
    (identity, key-fingerprint) pair trusted at publication time. Keys
    pinned, never bare identities; ``attesting`` marks the human key."""

    id: int
    identity_uri: str
    key_fingerprint: str
    attesting: bool
    valid_from: datetime
    valid_until: datetime | None
    note: str


@dataclass(frozen=True, slots=True)
class OtsBatchRow:
    """One append-only ``nanopub_ots_batches`` row plus its latest proof
    state (joined at read; ``proof_state`` is ``None`` for a batch with no
    proof row yet — a defect, surfaced by the audit)."""

    id: int
    merkle_root: str
    construction: str
    leaf_count: int
    calendar_url: str
    created_at: datetime
    proof_state: str | None


@dataclass(frozen=True, slots=True)
class OtsLeafRow:
    """One append-only ``nanopub_ots_leaves`` row: an artifact's inclusion
    path in one batch. Part of the proof, not metadata about it."""

    id: int
    batch_id: int
    artifact_id: int
    leaf_index: int
    leaf_hash: str
    path_proof: bytes


_PUBLISH_COLS = (
    "id, claim_ref_id, artifact_type, approved_title, claim_sha, aida_uri, "
    "grounding, dependency_codes, trusty_uri, artifact_id, batch_id, state, "
    "created_at, updated_at, published_at, registry_url"
)

_ARTIFACT_COLS = (
    "id, publish_id, claim_ref_id, artifact_type, trig_bytes, byte_sha256, "
    "trusty_uri, aida_uri, claim_sha, signer, key_fingerprint, dois, "
    "created_at"
)


def _row_to_publish(row: tuple[Any, ...]) -> PublishRow:
    return PublishRow(
        id=int(row[0]),
        claim_ref_id=int(row[1]),
        artifact_type=str(row[2]),
        approved_title=row[3],
        claim_sha=row[4],
        aida_uri=row[5],
        grounding=dict(row[6] or {}),
        dependency_codes=dict(row[7] or {}),
        trusty_uri=row[8],
        artifact_id=int(row[9]) if row[9] is not None else None,
        batch_id=int(row[10]) if row[10] is not None else None,
        state=str(row[11]),
        created_at=row[12],
        updated_at=row[13],
        published_at=row[14],
        registry_url=row[15],
    )


def _row_to_artifact(row: tuple[Any, ...]) -> ArtifactRow:
    return ArtifactRow(
        id=int(row[0]),
        publish_id=int(row[1]),
        claim_ref_id=int(row[2]),
        artifact_type=str(row[3]),
        trig_bytes=bytes(row[4]),
        byte_sha256=str(row[5]),
        trusty_uri=str(row[6]),
        aida_uri=str(row[7]),
        claim_sha=str(row[8]),
        signer=str(row[9]),
        key_fingerprint=str(row[10]),
        dois=list(row[11] or []),
        created_at=row[12],
    )


class NanopubMixin:
    """Mixin: assumes the concrete Store provides ``self.pool``."""

    pool: Any

    # ── publish rows ─────────────────────────────────────────────────

    def nanopub_publish_row(self, claim_ref_id: int) -> PublishRow | None:
        """The hub's live (non-terminal) publish row, or ``None``."""
        with self.pool.connection() as conn:
            row = conn.execute(
                f"SELECT {_PUBLISH_COLS} FROM nanopub_publish "
                "WHERE claim_ref_id = %s AND state != ALL(%s)",
                (claim_ref_id, list(TERMINAL_STATES)),
            ).fetchone()
        return _row_to_publish(row) if row else None

    def nanopub_publish_row_by_id(self, row_id: int) -> PublishRow | None:
        with self.pool.connection() as conn:
            row = conn.execute(
                f"SELECT {_PUBLISH_COLS} FROM nanopub_publish WHERE id = %s",
                (row_id,),
            ).fetchone()
        return _row_to_publish(row) if row else None

    def nanopub_publish_states_bulk(
        self, claim_ref_ids: Iterable[int]
    ) -> dict[int, tuple[str, datetime]]:
        """``{claim_ref_id: (state, updated_at)}`` for MANY hubs in one
        query — the smartdraft Claims-rail batch discipline (its bulk
        renderer resolves a render-window's worth of hubs per request;
        a per-hub :meth:`nanopub_publish_row` would re-add N round trips).

        Per hub: the live (non-terminal) row when one exists — the partial
        unique index guarantees at most one — else the most recently
        updated terminal row (so a retracted/superseded/rejected hub still
        reports what happened to it rather than vanishing). Hubs with no
        publish row at all are simply absent from the result."""
        ids = list(set(claim_ref_ids))
        if not ids:
            return {}
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT DISTINCT ON (claim_ref_id) claim_ref_id, state, updated_at "
                "FROM nanopub_publish WHERE claim_ref_id = ANY(%s) "
                "ORDER BY claim_ref_id, (state = ANY(%s)) ASC, updated_at DESC",
                (ids, list(TERMINAL_STATES)),
            ).fetchall()
        return {int(r[0]): (str(r[1]), r[2]) for r in rows}

    def nanopub_create_publish_row(
        self, claim_ref_id: int, *, artifact_type: str = "claim"
    ) -> PublishRow:
        """Insert a fresh ``candidate`` row. Raises (unique-index) if the
        hub already has a live one — callers check first via
        :meth:`nanopub_publish_row`."""
        with self.pool.connection() as conn:
            row = conn.execute(
                "INSERT INTO nanopub_publish (claim_ref_id, artifact_type) "
                f"VALUES (%s, %s) RETURNING {_PUBLISH_COLS}",
                (claim_ref_id, artifact_type),
            ).fetchone()
        assert row is not None
        return _row_to_publish(row)

    def nanopub_approve(
        self,
        row_id: int,
        *,
        approved_title: str,
        claim_sha: str,
        aida_uri: str,
        grounding: dict[str, Any],
    ) -> bool:
        """Freeze the reviewed claim string + grounding and flip
        ``candidate`` → ``reviewed``. CAS on state; ``False`` = the row
        was not in ``candidate`` (re-approval goes through
        :meth:`nanopub_reopen` first)."""
        with self.pool.connection() as conn:
            cur = conn.execute(
                "UPDATE nanopub_publish SET approved_title = %s, "
                "claim_sha = %s, aida_uri = %s, grounding = %s, "
                "state = 'reviewed', updated_at = now() "
                "WHERE id = %s AND state = 'candidate'",
                (approved_title, claim_sha, aida_uri, Jsonb(grounding), row_id),
            )
            return cur.rowcount == 1

    def nanopub_record_signed(
        self,
        row_id: int,
        *,
        trusty_uri: str,
        artifact_id: int,
        dependency_codes: dict[str, str],
    ) -> bool:
        """Flip ``reviewed`` → ``signed``, binding the artifact. CAS."""
        with self.pool.connection() as conn:
            cur = conn.execute(
                "UPDATE nanopub_publish SET trusty_uri = %s, artifact_id = %s, "
                "dependency_codes = %s, state = 'signed', updated_at = now() "
                "WHERE id = %s AND state = 'reviewed'",
                (trusty_uri, artifact_id, Jsonb(dependency_codes), row_id),
            )
            return cur.rowcount == 1

    def nanopub_transition(
        self, row_id: int, *, to_state: str, expect: tuple[str, ...]
    ) -> bool:
        """CAS state flip. Legality (which flips are allowed at all) is
        :func:`precis.nanopub.state.check_transition` — call that first."""
        with self.pool.connection() as conn:
            cur = conn.execute(
                "UPDATE nanopub_publish SET state = %s, updated_at = now() "
                "WHERE id = %s AND state = ANY(%s)",
                (to_state, row_id, list(expect)),
            )
            return cur.rowcount == 1

    def nanopub_reopen(self, row_id: int) -> bool:
        """Drift/edit reopen: flip a pre-anchor row (``reviewed`` or
        ``signed``) back to ``candidate``, discarding the frozen fields.
        The signed artifact row (if any) stays — append-only — but the
        publish row no longer points at it."""
        with self.pool.connection() as conn:
            cur = conn.execute(
                "UPDATE nanopub_publish SET state = 'candidate', "
                "approved_title = NULL, claim_sha = NULL, aida_uri = NULL, "
                "grounding = NULL, dependency_codes = NULL, trusty_uri = NULL, "
                "artifact_id = NULL, updated_at = now() "
                "WHERE id = %s AND state IN ('reviewed', 'signed')",
                (row_id,),
            )
            return cur.rowcount == 1

    def nanopub_reopen_stuck_batch(self, batch_id: int) -> int:
        """Stuck-pending remedy: flip every ``anchored`` row bound to
        ``batch_id`` back to ``signed`` and clear its ``batch_id``, so the
        next :func:`precis.nanopub.ots.stamp_batch` sweep picks the rows
        into a fresh batch (a later date, the old anchor left in place as
        history — ``nanopub_ots_batches`` is append-only, so the stuck
        batch itself is never marked; it just stops being any row's
        current batch). Deliberately narrower than :meth:`nanopub_reopen`
        (which never touches ``anchored`` rows at all): this is scoped to
        one named batch, not any anchored row, so it cannot be used to
        undo an anchor wholesale. Returns the row count reopened."""
        with self.pool.connection() as conn:
            cur = conn.execute(
                "UPDATE nanopub_publish SET state = 'signed', batch_id = NULL, "
                "updated_at = now() WHERE batch_id = %s AND state = 'anchored'",
                (batch_id,),
            )
            return cur.rowcount

    def nanopub_rows_in_state(
        self, state: str, *, limit: int = 200
    ) -> list[PublishRow]:
        with self.pool.connection() as conn:
            rows = conn.execute(
                f"SELECT {_PUBLISH_COLS} FROM nanopub_publish "
                "WHERE state = %s ORDER BY updated_at ASC LIMIT %s",
                (state, limit),
            ).fetchall()
        return [_row_to_publish(r) for r in rows]

    def nanopub_set_batch(self, row_id: int, batch_id: int) -> bool:
        """Bind an anchored publish row to its batch and flip
        ``signed`` → ``anchored``. CAS."""
        with self.pool.connection() as conn:
            cur = conn.execute(
                "UPDATE nanopub_publish SET batch_id = %s, state = 'anchored', "
                "updated_at = now() WHERE id = %s AND state = 'signed'",
                (batch_id, row_id),
            )
            return cur.rowcount == 1

    def nanopub_record_published(self, row_id: int, *, registry_url: str) -> bool:
        """Flip ``anchored`` → ``published``, stamping when and where. CAS.
        The registry POST itself (the one true point of no return) happens
        in :mod:`precis.nanopub.registry` BEFORE this bookkeeping — a
        ``False`` here after a successful POST means the row moved
        mid-publish and must be reconciled by hand, loudly."""
        with self.pool.connection() as conn:
            cur = conn.execute(
                "UPDATE nanopub_publish SET state = 'published', "
                "published_at = now(), registry_url = %s, updated_at = now() "
                "WHERE id = %s AND state = 'anchored'",
                (registry_url, row_id),
            )
            return cur.rowcount == 1

    # ── trust allowlist (publication-time gate) ──────────────────────

    def nanopub_allowlist(self) -> list[AllowlistEntry]:
        """Every allowlist row, open and closed windows alike — window
        checks are the caller's (deferred until OTS supplies signature
        time; the preflight requires an entry with an open window NOW)."""
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT id, identity_uri, key_fingerprint, attesting, "
                "valid_from, valid_until, note FROM nanopub_trust_allowlist "
                "ORDER BY id ASC",
            ).fetchall()
        return [
            AllowlistEntry(
                id=int(r[0]),
                identity_uri=str(r[1]),
                key_fingerprint=str(r[2]),
                attesting=bool(r[3]),
                valid_from=r[4],
                valid_until=r[5],
                note=str(r[6] or ""),
            )
            for r in rows
        ]

    def nanopub_allowlist_add(
        self,
        *,
        identity_uri: str,
        key_fingerprint: str,
        attesting: bool = False,
        note: str = "",
    ) -> int:
        """Add (or re-open/amend) one pinned (identity, fingerprint) pair.
        Upsert on the pair: adding an existing pair updates ``attesting``/
        ``note`` and clears a closed window — entries are hand-curated
        (``approvesOf`` may inform this, never drive it)."""
        with self.pool.connection() as conn:
            row = conn.execute(
                "INSERT INTO nanopub_trust_allowlist (identity_uri, "
                "key_fingerprint, attesting, note) VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (identity_uri, key_fingerprint) DO UPDATE SET "
                "attesting = EXCLUDED.attesting, note = EXCLUDED.note, "
                "valid_until = NULL RETURNING id",
                (identity_uri, key_fingerprint, attesting, note),
            ).fetchone()
        assert row is not None
        return int(row[0])

    def nanopub_allowlist_end(self, entry_id: int) -> bool:
        """Close an entry's validity window (never DELETE — rotation does
        not invalidate old signatures, and the history is the audit)."""
        with self.pool.connection() as conn:
            cur = conn.execute(
                "UPDATE nanopub_trust_allowlist SET valid_until = now() "
                "WHERE id = %s AND valid_until IS NULL",
                (entry_id,),
            )
            return cur.rowcount == 1

    # ── artifacts (append-only) ──────────────────────────────────────

    def nanopub_artifact_by_trusty(self, code_or_uri: str) -> ArtifactRow | None:
        """Artifact lookup by full trusty URI or bare ``RA…`` artifact
        code — the review surface serves by artifact code locally while
        the w3id name resolves nowhere (embargo). Exact matches only —
        the code reaches here from an unauthenticated URL path, so no
        LIKE (`%`/`_` in the input must never widen the match; the w3id
        base is fixed at mint, spec: base-URI-fixed-at-mint)."""
        with self.pool.connection() as conn:
            row = conn.execute(
                f"SELECT {_ARTIFACT_COLS} FROM nanopub_artifacts "
                "WHERE trusty_uri = %s "
                "   OR trusty_uri = 'https://w3id.org/np/' || %s "
                "LIMIT 1",
                (code_or_uri, code_or_uri),
            ).fetchone()
        return _row_to_artifact(row) if row else None

    def nanopub_insert_artifact(
        self,
        *,
        publish_id: int,
        claim_ref_id: int,
        artifact_type: str,
        trig_bytes: bytes,
        trusty_uri: str,
        aida_uri: str,
        claim_sha: str,
        signer: str,
        key_fingerprint: str,
        dois: list[str],
    ) -> int:
        with self.pool.connection() as conn:
            row = conn.execute(
                "INSERT INTO nanopub_artifacts (publish_id, claim_ref_id, "
                "artifact_type, trig_bytes, trusty_uri, aida_uri, claim_sha, "
                "signer, key_fingerprint, dois) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (
                    publish_id,
                    claim_ref_id,
                    artifact_type,
                    trig_bytes,
                    trusty_uri,
                    aida_uri,
                    claim_sha,
                    signer,
                    key_fingerprint,
                    Jsonb(dois),
                ),
            ).fetchone()
        assert row is not None
        return int(row[0])

    def nanopub_artifact(self, artifact_id: int) -> ArtifactRow | None:
        with self.pool.connection() as conn:
            row = conn.execute(
                f"SELECT {_ARTIFACT_COLS} FROM nanopub_artifacts WHERE id = %s",
                (artifact_id,),
            ).fetchone()
        return _row_to_artifact(row) if row else None

    def nanopub_artifacts_since(
        self, *, after_id: int = 0, limit: int = 500
    ) -> list[ArtifactRow]:
        """Audit iterator: artifacts in id order, paged by ``after_id``."""
        with self.pool.connection() as conn:
            rows = conn.execute(
                f"SELECT {_ARTIFACT_COLS} FROM nanopub_artifacts "
                "WHERE id > %s ORDER BY id ASC LIMIT %s",
                (after_id, limit),
            ).fetchall()
        return [_row_to_artifact(r) for r in rows]

    # ── OTS batches / leaves / proofs (append-only) ──────────────────

    def nanopub_create_batch(
        self,
        *,
        merkle_root: str,
        construction: str,
        calendar_url: str,
        leaves: list[tuple[int, int, str, bytes]],
        pending_proof: bytes,
    ) -> int:
        """One transaction: batch row + every leaf + the initial
        ``pending`` proof row. ``leaves`` items are
        ``(artifact_id, leaf_index, leaf_hash, path_proof)``."""
        with self.pool.connection() as conn:
            row = conn.execute(
                "INSERT INTO nanopub_ots_batches (merkle_root, construction, "
                "leaf_count, calendar_url) VALUES (%s, %s, %s, %s) "
                "RETURNING id",
                (merkle_root, construction, len(leaves), calendar_url),
            ).fetchone()
            assert row is not None
            batch_id = int(row[0])
            for artifact_id, leaf_index, leaf_hash, path_proof in leaves:
                conn.execute(
                    "INSERT INTO nanopub_ots_leaves (batch_id, artifact_id, "
                    "leaf_index, leaf_hash, path_proof) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (batch_id, artifact_id, leaf_index, leaf_hash, path_proof),
                )
            conn.execute(
                "INSERT INTO nanopub_ots_proofs (batch_id, state, ots_proof) "
                "VALUES (%s, 'pending', %s)",
                (batch_id, pending_proof),
            )
        return batch_id

    def nanopub_pending_batches(self) -> list[OtsBatchRow]:
        """Batches whose *latest* proof row is still ``pending``."""
        return self._batches_where(
            "latest.state = 'pending'",
        )

    def nanopub_batches(self, *, limit: int = 500) -> list[OtsBatchRow]:
        return self._batches_where("TRUE", limit=limit)

    def _batches_where(self, predicate: str, *, limit: int = 500) -> list[OtsBatchRow]:
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT b.id, b.merkle_root, b.construction, b.leaf_count, "
                "b.calendar_url, b.created_at, latest.state "
                "FROM nanopub_ots_batches b "
                "LEFT JOIN LATERAL (SELECT state FROM nanopub_ots_proofs p "
                "  WHERE p.batch_id = b.id ORDER BY p.id DESC LIMIT 1"
                ") latest ON TRUE "
                f"WHERE {predicate} ORDER BY b.id ASC LIMIT %s",
                (limit,),
            ).fetchall()
        return [
            OtsBatchRow(
                id=int(r[0]),
                merkle_root=str(r[1]),
                construction=str(r[2]),
                leaf_count=int(r[3]),
                calendar_url=str(r[4]),
                created_at=r[5],
                proof_state=r[6],
            )
            for r in rows
        ]

    def nanopub_latest_proof(self, batch_id: int) -> tuple[str, bytes] | None:
        """The batch's newest proof row as ``(state, ots_bytes)``."""
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT state, ots_proof FROM nanopub_ots_proofs "
                "WHERE batch_id = %s ORDER BY id DESC LIMIT 1",
                (batch_id,),
            ).fetchone()
        return (str(row[0]), bytes(row[1])) if row else None

    def nanopub_add_proof(self, batch_id: int, *, state: str, ots_proof: bytes) -> int:
        with self.pool.connection() as conn:
            row = conn.execute(
                "INSERT INTO nanopub_ots_proofs (batch_id, state, ots_proof) "
                "VALUES (%s, %s, %s) RETURNING id",
                (batch_id, state, ots_proof),
            ).fetchone()
        assert row is not None
        return int(row[0])

    def nanopub_batch_leaves(self, batch_id: int) -> list[OtsLeafRow]:
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT id, batch_id, artifact_id, leaf_index, leaf_hash, "
                "path_proof FROM nanopub_ots_leaves WHERE batch_id = %s "
                "ORDER BY leaf_index ASC",
                (batch_id,),
            ).fetchall()
        return [
            OtsLeafRow(
                id=int(r[0]),
                batch_id=int(r[1]),
                artifact_id=int(r[2]),
                leaf_index=int(r[3]),
                leaf_hash=str(r[4]),
                path_proof=bytes(r[5]),
            )
            for r in rows
        ]
