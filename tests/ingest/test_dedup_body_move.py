"""gr479362: ``merge_duplicate`` hands a bodied duplicate's body, PDF and
content ids to a bodiless survivor, and leaves a bodied survivor alone."""

from __future__ import annotations

from typing import Any

from precis.ingest.dedup import merge_duplicate
from precis.store import Store

SHA = "ab" * 32
CONTENT = "cd" * 32


def _seed_bodied(store: Store, slug: str, *, n_body: int = 3) -> int:
    """A paper with a PDF, content ids, a card and ``n_body`` body chunks."""
    ref = store.insert_ref(kind="paper", slug=slug, title=f"Bodied {slug}")
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO pdfs (pdf_sha256, content_hash, page_count, size_bytes, "
            "storage_path) VALUES (%s, %s, 1, 10, '/tmp/x.pdf') "
            "ON CONFLICT DO NOTHING",
            (SHA, CONTENT),
        )
        conn.execute(
            "UPDATE refs SET pdf_sha256 = %s, pdf_role = 'main' WHERE ref_id = %s",
            (SHA, ref.id),
        )
        for kind, val in (
            ("pdf_sha256", SHA),
            ("content_hash", CONTENT),
            ("paper_id", f"pid-{slug}"),
        ):
            conn.execute(
                "INSERT INTO ref_identifiers (ref_id, id_kind, id_value, source) "
                "VALUES (%s, %s, %s, 'test')",
                (ref.id, kind, val),
            )
        for i in range(n_body):
            conn.execute(
                "INSERT INTO chunks (ref_id, ord, chunk_kind, text) "
                "VALUES (%s, %s, 'paragraph', %s)",
                (ref.id, i, f"body text {slug} {i}"),
            )
        conn.commit()
    return int(ref.id)


def _merge(store: Store, survivor: int, dup: int) -> None:
    with store.tx() as conn:
        merge_duplicate(
            store,
            survivor_ref_id=survivor,
            duplicate_ref_id=dup,
            source="test",
            reason="unit-test",
            conn=conn,
        )


def _one(store: Store, sql: str, *params: Any) -> Any:
    with store.pool.connection() as conn:
        row = conn.execute(sql, params).fetchone()
    return None if row is None else row[0]


def _body_texts(store: Store, ref_id: int) -> list[str]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT text FROM chunks WHERE ref_id = %s AND ord >= 0 ORDER BY ord",
            (ref_id,),
        ).fetchall()
    return [r[0] for r in rows]


def _owner(store: Store, kind: str, value: str) -> int:
    return int(
        _one(
            store,
            "SELECT ref_id FROM ref_identifiers WHERE id_kind=%s AND id_value=%s",
            kind,
            value,
        )
    )


def test_bodiless_stub_survivor_takes_body_pdf_and_content_ids(store: Store) -> None:
    stub = int(store.insert_ref(kind="paper", slug="wanted24", title="Wanted").id)
    twin = _seed_bodied(store, "twin24")

    _merge(store, stub, twin)

    assert _body_texts(store, stub) == [f"body text twin24 {i}" for i in range(3)]
    assert _body_texts(store, twin) == []
    assert _one(store, "SELECT pdf_sha256 FROM refs WHERE ref_id=%s", stub) == SHA
    assert _one(store, "SELECT pdf_role FROM refs WHERE ref_id=%s", stub) == "main"
    assert _one(store, "SELECT pdf_sha256 FROM refs WHERE ref_id=%s", twin) is None
    # A re-ingest of the same file probes onto the survivor, not the retired twin.
    assert _owner(store, "pdf_sha256", SHA) == stub
    assert _owner(store, "content_hash", CONTENT) == stub
    assert _owner(store, "paper_id", "pid-twin24") == stub
    # Twin retired with provenance; stub live.
    assert _one(store, "SELECT retired_at FROM refs WHERE ref_id=%s", twin) is not None
    assert _one(store, "SELECT retired_at FROM refs WHERE ref_id=%s", stub) is None
    assert _one(
        store, "SELECT meta->>'superseded_by' FROM refs WHERE ref_id=%s", twin
    ) == str(stub)
    # Moved chunks carry no embedding yet: the worker refills them.
    assert (
        _one(
            store,
            "SELECT count(*) FROM chunk_embeddings e JOIN chunks c USING (chunk_id) "
            "WHERE c.ref_id = %s",
            stub,
        )
        == 0
    )


def test_survivor_keeps_own_paper_id(store: Store) -> None:
    """``paper_id`` is single-valued per ref: a survivor that has one keeps it
    and the twin's stays behind (the ``v_refs`` view reads it as a scalar)."""
    stub = int(store.insert_ref(kind="paper", slug="wanted25", title="Wanted").id)
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO ref_identifiers (ref_id, id_kind, id_value, source) "
            "VALUES (%s, 'paper_id', 'stub-own-pid', 'test')",
            (stub,),
        )
        conn.commit()
    twin = _seed_bodied(store, "twin25")

    _merge(store, stub, twin)

    assert _owner(store, "paper_id", "stub-own-pid") == stub
    assert _owner(store, "paper_id", "pid-twin25") == twin
    assert _owner(store, "pdf_sha256", SHA) == stub
    assert len(_body_texts(store, stub)) == 3


def test_bodied_survivor_keeps_current_behaviour(store: Store) -> None:
    """Regression: a survivor that already has a body and PDF keeps both; the
    duplicate's body and content ids stay on the retired duplicate."""
    survivor = _seed_bodied(store, "keeper24", n_body=2)
    # A second, different body on the duplicate (own sha so rows don't collide).
    dup = int(store.insert_ref(kind="paper", slug="loser24", title="Loser").id)
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO ref_identifiers (ref_id, id_kind, id_value, source) "
            "VALUES (%s, 'arxiv', '2401.00001', 'test')",
            (dup,),
        )
        conn.execute(
            "INSERT INTO ref_identifiers (ref_id, id_kind, id_value, source) "
            "VALUES (%s, 'content_hash', 'dup-content', 'test')",
            (dup,),
        )
        conn.execute(
            "INSERT INTO chunks (ref_id, ord, chunk_kind, text) "
            "VALUES (%s, 0, 'paragraph', 'dup body')",
            (dup,),
        )
        conn.commit()

    _merge(store, survivor, dup)

    assert _body_texts(store, survivor) == [
        "body text keeper24 0",
        "body text keeper24 1",
    ]
    assert _body_texts(store, dup) == ["dup body"]  # untouched, not moved
    assert _one(store, "SELECT pdf_sha256 FROM refs WHERE ref_id=%s", survivor) == SHA
    assert _owner(store, "content_hash", "dup-content") == dup  # stays behind
    assert _owner(store, "arxiv", "2401.00001") == survivor  # still migrates
    assert _one(store, "SELECT retired_at FROM refs WHERE ref_id=%s", dup) is not None


def test_two_bodiless_refs_move_nothing(store: Store) -> None:
    a = int(store.insert_ref(kind="paper", slug="stuba24", title="A").id)
    b = int(store.insert_ref(kind="paper", slug="stubb24", title="B").id)
    _merge(store, a, b)
    assert _body_texts(store, a) == []
    assert _one(store, "SELECT pdf_sha256 FROM refs WHERE ref_id=%s", a) is None


def _id_rows(store: Store, ref_id: int, kinds: list[str]) -> list[tuple[str, str]]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT id_kind, id_value FROM ref_identifiers "
            "WHERE ref_id = %s AND id_kind = ANY(%s) ORDER BY id_kind, id_value",
            (ref_id, kinds),
        ).fetchall()
    return [(str(r[0]), str(r[1])) for r in rows]


def test_survivor_stale_pdf_ids_deleted_on_move(store: Store) -> None:
    """gr515805: (ref_id, id_kind) has no unique constraint — a bodiless,
    pdf-less survivor holding stale pdf-kind rows would double up with the
    re-homed ones. The stale rows are deleted in the same transaction."""
    stub = int(store.insert_ref(kind="paper", slug="staleid24", title="Wanted").id)
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO ref_identifiers (ref_id, id_kind, id_value, source) "
            "VALUES (%s, 'pdf_sha256', %s, 'test'), "
            "       (%s, 'content_hash', %s, 'test')",
            (stub, "d" * 64, stub, "e" * 64),
        )
        conn.commit()
    twin = _seed_bodied(store, "twinst24")

    _merge(store, stub, twin)

    assert _id_rows(store, stub, ["pdf_sha256", "content_hash"]) == [
        ("content_hash", CONTENT),
        ("pdf_sha256", SHA),
    ]


def test_pdf_only_move_keeps_survivor_body_content_hash(store: Store) -> None:
    """gr515805: ``content_hash`` covers the canonical body text, not file
    bytes — a survivor with a live markup body keeps its own row when only
    the pdf moves; its stale ``pdf_sha256`` row is still dropped."""
    survivor = int(store.insert_ref(kind="paper", slug="mkup24", title="Mk").id)
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO chunks (ref_id, ord, chunk_kind, text) "
            "VALUES (%s, 0, 'paragraph', 'markup body')",
            (survivor,),
        )
        conn.execute(
            "INSERT INTO ref_identifiers (ref_id, id_kind, id_value, source) "
            "VALUES (%s, 'content_hash', 'mk-body-hash', 'test'), "
            "       (%s, 'pdf_sha256', %s, 'test')",
            (survivor, survivor, "b" * 64),
        )
        conn.commit()
    # Duplicate: pdf but no body — the pdf moves, the body does not.
    dup = int(store.insert_ref(kind="paper", slug="dupdf24", title="Dup").id)
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO pdfs (pdf_sha256, content_hash, page_count, size_bytes, "
            "storage_path) VALUES (%s, %s, 1, 10, '/tmp/y.pdf') "
            "ON CONFLICT DO NOTHING",
            ("f" * 64, "dup-content"),
        )
        conn.execute(
            "UPDATE refs SET pdf_sha256 = %s, pdf_role = 'main' WHERE ref_id = %s",
            ("f" * 64, dup),
        )
        conn.execute(
            "INSERT INTO ref_identifiers (ref_id, id_kind, id_value, source) "
            "VALUES (%s, 'pdf_sha256', %s, 'test'), "
            "       (%s, 'content_hash', 'dup-content', 'test')",
            (dup, "f" * 64, dup),
        )
        conn.commit()

    _merge(store, survivor, dup)

    assert _one(store, "SELECT pdf_sha256 FROM refs WHERE ref_id=%s", survivor) == (
        "f" * 64
    )
    assert _body_texts(store, survivor) == ["markup body"]  # untouched
    assert _id_rows(store, survivor, ["pdf_sha256", "content_hash"]) == [
        ("content_hash", "dup-content"),
        ("content_hash", "mk-body-hash"),
        ("pdf_sha256", "f" * 64),
    ]


def test_body_move_keeps_survivor_pdf_sha256_aliases(store: Store) -> None:
    """gr515805: a survivor that holds a live pdf keeps its whole
    ``pdf_sha256`` set — the historical-alias rows are deliberate
    (``pdf_sha256_aliases``), not stale — while its ``content_hash``
    row still drops (no body yet, so it described nothing it held).
    The donor's pdf-kind rows re-home alongside them."""
    survivor = int(store.insert_ref(kind="paper", slug="keepal24", title="K").id)
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO pdfs (pdf_sha256, content_hash, page_count, size_bytes, "
            "storage_path) VALUES (%s, %s, 1, 10, '/tmp/z.pdf') "
            "ON CONFLICT DO NOTHING",
            ("a" * 64, "keep-content"),
        )
        conn.execute(
            "UPDATE refs SET pdf_sha256 = %s, pdf_role = 'main' WHERE ref_id = %s",
            ("a" * 64, survivor),
        )
        conn.execute(
            "INSERT INTO ref_identifiers (ref_id, id_kind, id_value, source) "
            "VALUES (%s, 'pdf_sha256', %s, 'test'), "
            "       (%s, 'pdf_sha256', %s, 'test'), "
            "       (%s, 'content_hash', 'stale-hash', 'test')",
            (survivor, "a" * 64, survivor, "b" * 64, survivor),
        )
        conn.commit()
    twin = _seed_bodied(store, "twinal24")

    _merge(store, survivor, twin)

    # Body moved, pdf did not.
    assert _one(store, "SELECT pdf_sha256 FROM refs WHERE ref_id=%s", survivor) == (
        "a" * 64
    )
    assert len(_body_texts(store, survivor)) == 3
    assert _id_rows(store, survivor, ["pdf_sha256", "content_hash"]) == [
        ("content_hash", CONTENT),  # donor's — survivor's stale row dropped
        ("pdf_sha256", "a" * 64),  # survivor's live pdf
        ("pdf_sha256", SHA),  # donor's re-homed row
        ("pdf_sha256", "b" * 64),  # survivor's historical alias
    ]
