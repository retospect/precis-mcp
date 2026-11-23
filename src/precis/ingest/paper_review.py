"""Rolling clerical review todos with a generated table and durable row keys."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from precis.store.types import TODO_BODY_KIND, Tag
from precis.todo import terminal_todo_sql
from precis.utils.handle_registry import format_handle

if TYPE_CHECKING:
    from psycopg import Connection

    from precis.store import Store


@dataclass(frozen=True)
class PaperReview:
    source: str
    tag: str
    title: str
    instructions: str
    columns: tuple[str, ...]

    @property
    def batch_source(self) -> str:
        return f"{self.source}:batch"

    def _lock(self, conn: Connection) -> None:
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (self.batch_source,),
        )

    def _open(self, conn: Connection) -> tuple[int, dict[str, list[str]]] | None:
        return conn.execute(
            "SELECT t.ref_id, t.meta->'review_rows' FROM refs t "
            "WHERE t.kind = 'todo' AND t.retired_at IS NULL AND t.meta->>'source' = %s "
            f"AND NOT EXISTS ({_status_query(f'tg.value IN {terminal_todo_sql()}')}) "
            "ORDER BY t.ref_id LIMIT 1",
            (self.batch_source,),
        ).fetchone()

    def _write(
        self,
        store: Store,
        todo_id: int,
        rows: dict[str, list[str]],
        *,
        conn: Connection,
    ) -> None:
        store.update_ref(todo_id, meta_patch={"review_rows": rows}, conn=conn)
        header = [
            "| " + " | ".join(self.columns) + " |",
            "| " + " | ".join("---" for _ in self.columns) + " |",
        ]
        table = header + [
            "| " + " | ".join(map(_cell, cells)) + " |" for cells in rows.values()
        ]
        body = self.instructions + "\n\n" + "\n".join(table)
        store.chunks.replace_body_chunk(
            todo_id,
            body,
            chunk_kind=TODO_BODY_KIND,
            source=self.batch_source,
            conn=conn,
        )

    def add(
        self, store: Store, rows: dict[str, list[str]], *, conn: Connection
    ) -> int | None:
        self._lock(conn)
        batch = self._open(conn)
        todo_id, current = batch if batch else (None, {})
        pending = {key: cells for key, cells in rows.items() if key not in current}
        if not pending:
            return todo_id
        if todo_id is None:
            todo_id = store.insert_ref(
                kind="todo",
                slug=None,
                title=self.title,
                meta={"source": self.batch_source},
                conn=conn,
            ).id
            _status(store, todo_id, "open", conn=conn)
            store.add_tag(todo_id, Tag.open(self.tag), set_by="system", conn=conn)
        self._write(store, todo_id, current | pending, conn=conn)
        return todo_id

    def remove(self, store: Store, key: str, *, conn: Connection) -> bool:
        self._lock(conn)
        batch = self._open(conn)
        if batch is None or key not in batch[1]:
            return False
        todo_id, rows = batch
        del rows[key]
        if rows:
            self._write(store, todo_id, rows, conn=conn)
        else:
            # Keep the final table and row keys on the closed todo for audit.
            _status(store, todo_id, "done", conn=conn)
        return True


def _cell(value: str) -> str:
    return " ".join(value.split()).replace("|", r"\|")


def _status_query(predicate: str) -> str:
    return (
        "SELECT 1 FROM ref_tags rt JOIN tags tg USING (tag_id) "
        f"WHERE rt.ref_id = t.ref_id AND tg.namespace = 'STATUS' AND {predicate}"
    )


def _status(store: Store, todo_id: int, status: str, *, conn: Connection) -> None:
    store.add_tag(
        todo_id,
        Tag.closed("STATUS", status),
        set_by="system",
        replace_prefix=True,
        conn=conn,
    )


TITLE_REVIEW = PaperReview(
    "paper_hygiene:title_fix",
    "paper-title-fix",
    "Review paper titles",
    "Check each paper's front matter and correct its title and any missing authors or year. "
    "Repairs clear rows automatically. Close the todo when the review is complete.",
    ("Paper", "Cite key", "Stored title"),
)
STUB_REVIEW = PaperReview(
    "ingest:stub-reconcile-review",
    "paper-stub-identity-review",
    "Review paper stub identities",
    "Compare each stub with the held paper. Merge only when the identifiers and content "
    "describe the same work. Close the todo when the review is complete.",
    ("Stub", "Held paper", "PDF", "Refusal"),
)


def title_row(ref_id: int, cite_key: str, title: str) -> list[str]:
    return [format_handle("paper", ref_id), cite_key or "—", repr(title[:160])]


def add_stub_identity_review(
    store: Store,
    stub_id: int,
    survivor_id: int,
    file_stem: str,
    reason: str,
    *,
    conn: Connection,
) -> None:
    """Queue a refused pair unless an existing review still covers it."""
    review = STUB_REVIEW
    key = f"{stub_id}:{survivor_id}"
    review._lock(conn)
    standing = conn.execute(
        "SELECT 1 FROM refs t "
        "WHERE t.kind = 'todo' AND t.retired_at IS NULL "
        "AND ((t.meta->>'source' = %s AND t.meta->>'stub_ref_id' = %s "
        "      AND t.meta->>'survivor_ref_id' = %s) "
        "  OR (t.meta->>'source' = %s AND (t.meta->'review_rows') ? %s)) "
        f"AND NOT EXISTS ({_status_query("tg.value = 'done'")}) LIMIT 1",
        (review.source, str(stub_id), str(survivor_id), review.batch_source, key),
    ).fetchone()
    if standing is None:
        review.add(
            store, {key: stub_row(stub_id, survivor_id, file_stem, reason)}, conn=conn
        )


def stub_row(stub_id: int, survivor_id: int, file_stem: str, reason: str) -> list[str]:
    return [
        format_handle("paper", stub_id),
        format_handle("paper", survivor_id),
        file_stem,
        reason,
    ]


def consolidate_legacy_paper_reviews(store: Store) -> None:
    """Carry the open legacy cases over and close their source todos atomically."""
    for review in (TITLE_REVIEW, STUB_REVIEW):
        with store.tx() as conn:
            review._lock(conn)
            legacy = conn.execute(
                "SELECT t.ref_id, t.meta, p.title, "
                "(SELECT min(i.id_value) FROM ref_identifiers i "
                " WHERE i.ref_id = p.ref_id AND i.id_kind = 'cite_key') "
                "FROM refs t LEFT JOIN refs p ON p.ref_id::text = t.meta->>'paper_ref_id' "
                "WHERE t.kind = 'todo' AND t.retired_at IS NULL AND t.meta->>'source' = %s "
                f"AND NOT EXISTS ({_status_query(f'tg.value IN {terminal_todo_sql()}')}) "
                "ORDER BY t.ref_id",
                (review.source,),
            ).fetchall()
            rows: dict[str, list[str]] = {}
            for _, meta, title, cite_key in legacy:
                if review is TITLE_REVIEW:
                    paper_id = int(meta["paper_ref_id"])
                    rows[str(paper_id)] = title_row(
                        paper_id, cite_key or "", title or ""
                    )
                else:
                    stub_id, survivor_id = (
                        int(meta["stub_ref_id"]),
                        int(meta["survivor_ref_id"]),
                    )
                    rows[f"{stub_id}:{survivor_id}"] = stub_row(
                        stub_id,
                        survivor_id,
                        meta.get("file_stem", ""),
                        meta.get("reason", ""),
                    )
            batch_id = review.add(store, rows, conn=conn)
            for todo_id, *_ in legacy:
                _status(store, todo_id, "done", conn=conn)
                wait_tags = conn.execute(
                    "SELECT tg.value FROM ref_tags rt JOIN tags tg USING (tag_id) "
                    "WHERE rt.ref_id = %s AND tg.namespace = 'OPEN' "
                    "AND starts_with(tg.value, 'waiting-for:')",
                    (todo_id,),
                ).fetchall()
                for (tag,) in wait_tags:
                    store.remove_tag(todo_id, Tag.open(tag), conn=conn)
                store.append_event(
                    todo_id,
                    source=review.source,
                    event="batched",
                    payload={"batch_ref_id": batch_id},
                    conn=conn,
                )
