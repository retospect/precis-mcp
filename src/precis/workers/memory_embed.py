"""Revision-pinned memory work on the existing embed_batch executor.

The opt-in target constrains work to one memory's body and header, without
changing the ordinary derived queue's order or the configured model route.
Shared chunk leases prevent duplicate work. Ref/chunk locks cover validation
and writes, never model calls: an edit during computation invalidates the
entire target, so an old result cannot be credited to a new revision.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Any

from psycopg import Connection
from psycopg.rows import dict_row

from precis.embedder import Embedder
from precis.store._chunks_ops import header_card_text
from precis.workers.base import ClaimedChunk
from precis.workers.embed import EmbedHandler

MAX_TARGET_CHUNKS = 32

MEMORY_TARGET_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ref_id": {"type": "integer", "minimum": 1},
        "embedder": {"type": "string", "minLength": 1},
        "chunks": {
            "type": "array",
            "minItems": 1,
            "maxItems": MAX_TARGET_CHUNKS,
            "items": {
                "type": "object",
                "properties": {
                    "chunk_id": {"type": "integer", "minimum": 1},
                    "content_sha": {"type": ["string", "null"]},
                    "text_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                },
                "required": ["chunk_id", "content_sha", "text_sha256"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["ref_id", "embedder", "chunks"],
    "additionalProperties": False,
}


def parse_memory_target(raw: object) -> dict[str, Any]:
    """Validate persisted params too: direct dispatch need not pass JSON Schema."""
    if not isinstance(raw, dict) or set(raw) != {"ref_id", "embedder", "chunks"}:
        raise ValueError("memory_target requires ref_id, embedder and chunks")
    if type(raw["ref_id"]) is not int or raw["ref_id"] <= 0:
        raise ValueError("memory_target.ref_id must name one positive memory ID")
    if not isinstance(raw["embedder"], str) or not raw["embedder"].strip():
        raise ValueError("memory_target.embedder must name the expected model")
    chunks = raw["chunks"]
    if not isinstance(chunks, list) or not 1 <= len(chunks) <= MAX_TARGET_CHUNKS:
        raise ValueError(f"memory_target requires 1–{MAX_TARGET_CHUNKS} chunks")
    ids: set[int] = set()
    for chunk in chunks:
        if not isinstance(chunk, dict) or set(chunk) != {
            "chunk_id",
            "content_sha",
            "text_sha256",
        }:
            raise ValueError(
                "memory_target chunk requires ID, content_sha and text_sha256"
            )
        chunk_id = chunk["chunk_id"]
        if type(chunk_id) is not int or chunk_id <= 0 or chunk_id in ids:
            raise ValueError("memory_target chunk IDs must be unique positive integers")
        ids.add(chunk_id)
        if chunk["content_sha"] is not None and not isinstance(
            chunk["content_sha"], str
        ):
            raise ValueError("memory_target.content_sha must be a string or null")
        if not isinstance(chunk["text_sha256"], str) or not re.fullmatch(
            r"[0-9a-f]{64}", chunk["text_sha256"]
        ):
            raise ValueError("memory_target.text_sha256 must be a SHA256 hex digest")
    return {**raw, "chunks": [dict(chunk) for chunk in chunks]}


class TargetedMemoryEmbedHandler(EmbedHandler):
    """Lease only the pinned revision; never fall back to the corpus backlog."""

    def __init__(self, embedder: Embedder, target: dict[str, Any]) -> None:
        super().__init__(embedder)
        self.target = parse_memory_target(target)
        self.problem: str | None = None
        self.stale_revision = False
        self._owned: dict[int, datetime] = {}

    def _current_rows(self, conn: Connection) -> list[dict[str, Any]] | None:
        with conn.cursor(row_factory=dict_row) as cur:
            focus = cur.execute(
                "SELECT title, meta->>'hook' AS hook, "
                "(SELECT name FROM embedders WHERE is_default ORDER BY name LIMIT 1) "
                "AS embedder FROM refs WHERE ref_id = %s AND kind = 'memory' "
                "AND retired_at IS NULL FOR NO KEY UPDATE",
                (self.target["ref_id"],),
            ).fetchone()
            if focus is None or focus["embedder"] != self.target["embedder"]:
                self.problem = "memory retired, missing, or default embedder changed"
                return None
            rows = cur.execute(
                "SELECT c.chunk_id, c.chunk_kind, c.text, c.content_sha, "
                "c.meta->>'no_index' AS no_index, e.status, "
                "e.content_sha AS embedding_sha, e.vector IS NOT NULL AS has_vector "
                "FROM chunks c LEFT JOIN chunk_embeddings e "
                "ON e.chunk_id = c.chunk_id AND e.embedder = %s "
                "WHERE c.ref_id = %s AND c.retired_at IS NULL "
                "AND (c.chunk_kind = 'memory_body' OR "
                "(c.chunk_kind = 'card_combined' AND c.ord = -1)) "
                "ORDER BY c.chunk_id LIMIT %s FOR UPDATE OF c",
                (self.target["embedder"], self.target["ref_id"], MAX_TARGET_CHUNKS + 1),
            ).fetchall()
        pinned = {chunk["chunk_id"]: chunk for chunk in self.target["chunks"]}
        if set(pinned) != {row["chunk_id"] for row in rows}:
            self.stale_revision = True
            self.problem = "memory content revision changed; request processing again"
            return None
        header = header_card_text(focus["title"], focus["hook"])
        if not any(row["chunk_kind"] == "memory_body" for row in rows) or (
            header and not any(row["chunk_kind"] == "card_combined" for row in rows)
        ):
            self.problem = "memory body or current title/hook header card is missing"
            return None
        for row in rows:
            expected = pinned[row["chunk_id"]]
            if row["no_index"] == "true":
                self.problem = "memory chunk has no_index=true; indexing is disabled"
                return None
            if (
                row["content_sha"] != expected["content_sha"]
                or hashlib.sha256(row["text"].encode("utf-8")).hexdigest()
                != expected["text_sha256"]
                or (row["chunk_kind"] == "card_combined" and row["text"] != header)
            ):
                self.stale_revision = True
                self.problem = (
                    "memory content revision changed; request processing again"
                )
                return None
        return rows

    @staticmethod
    def _ready(row: dict[str, Any]) -> bool:
        return bool(
            row["status"] == "ok"
            and row["has_vector"]
            and row["embedding_sha"] == row["content_sha"]
        )

    def claim_batch(self, conn: Connection, *, limit: int) -> list[ClaimedChunk]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        rows = self._current_rows(conn)
        if rows is None:
            return []
        claimed: list[ClaimedChunk] = []
        for row in rows:
            if len(claimed) >= limit:
                break
            if self._ready(row) or row["status"] == "failed":
                continue
            lease = conn.execute(
                "INSERT INTO chunk_claims (chunk_id, artifact) VALUES (%s, %s) "
                "ON CONFLICT (chunk_id, artifact) DO NOTHING RETURNING claimed_at",
                (row["chunk_id"], self.model_name),
            ).fetchone()
            if lease is not None:
                self._owned[row["chunk_id"]] = lease[0]
                claimed.append(ClaimedChunk(chunk_id=row["chunk_id"], text=row["text"]))
        return claimed

    def _may_write(self, conn: Connection, chunk_id: int) -> bool:
        if self._current_rows(conn) is None or chunk_id not in self._owned:
            return False
        owned = conn.execute(
            "SELECT 1 FROM chunk_claims WHERE chunk_id = %s AND artifact = %s "
            "AND claimed_at = %s FOR UPDATE",
            (chunk_id, self.model_name, self._owned[chunk_id]),
        ).fetchone()
        if owned is None:
            self.problem = "chunk lease changed; another worker owns the pending result"
        return owned is not None

    def write_ok(self, conn: Connection, chunk_id: int, payload: object) -> None:
        if self._may_write(conn, chunk_id):
            super().write_ok(conn, chunk_id, payload)

    def write_failed(self, conn: Connection, chunk_id: int, error: str) -> None:
        if self._may_write(conn, chunk_id):
            super().write_failed(conn, chunk_id, error)

    def release_claims(self, conn: Connection, chunk_ids: list[int]) -> None:
        for chunk_id in chunk_ids:
            if chunk_id in self._owned:
                conn.execute(
                    "DELETE FROM chunk_claims WHERE chunk_id = %s AND artifact = %s "
                    "AND claimed_at = %s",
                    (chunk_id, self.model_name, self._owned[chunk_id]),
                )

    def outstanding_reason(self, conn: Connection) -> str | None:
        """Job completion requires actual artifacts for the still-current revision."""
        rows = self._current_rows(conn)
        if rows is None:
            return self.problem
        if any(row["status"] == "failed" for row in rows):
            return "selected embedding failed; inspect processing and explicitly retry"
        if not all(self._ready(row) for row in rows):
            return (
                "selected embeddings remain pending (possibly leased by another worker)"
            )
        return None
