"""Passive relation spellings are stored as the active edge (ends swapped).

``Store.add_link`` flips ``_CANONICAL_FLIP`` spellings so a hand-written
reader that walks only the active spelling sees every row; migration 0325
rewrites the literal passive rows written before that.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.memory import MemoryHandler
from precis.store import Store
from precis.utils.link_tag_ops import apply_link_ops
from tests.conftest import MIGRATIONS_DIR, _active_dsn, id_of

# passive spelling -> stored active spelling
PAIRS = [
    ("served-by", "serves"),
    ("cited-by", "cites"),
    ("has-draft", "draft-of"),
    ("has-plan", "plan-of"),
    ("prerequisite-of", "has-prerequisite"),
    ("represented-by", "represents"),
    ("has-dossier", "dossier-of"),
    ("has-paper", "paper-of"),
]


def _ref(store: Store, kind: str, title: str, slug: str | None = None) -> int:
    return store.insert_ref(kind=kind, slug=slug, title=title).id


def _rows(store: Store, *ids: int) -> list[tuple[int, int, str]]:
    with store.pool.connection() as conn:
        return [
            (int(r[0]), int(r[1]), str(r[2]))
            for r in conn.execute(
                "SELECT src_ref_id, dst_ref_id, relation FROM links "
                "WHERE src_ref_id = ANY(%s) OR dst_ref_id = ANY(%s) "
                "ORDER BY link_id",
                (list(ids), list(ids)),
            ).fetchall()
        ]


@pytest.mark.parametrize(("passive", "active"), PAIRS)
def test_passive_spelling_stores_active_edge_swapped(
    store: Store, passive: str, active: str
) -> None:
    a = _ref(store, "memory", "end a")
    b = _ref(store, "memory", "end b")

    store.add_link(src_ref_id=a, dst_ref_id=b, relation=passive)
    assert _rows(store, a, b) == [(b, a, active)]

    # the active spelling from the other side is the identical row
    store.add_link(src_ref_id=b, dst_ref_id=a, relation=active)
    assert _rows(store, a, b) == [(b, a, active)]

    # each side reads it once under the active spelling
    assert len(store.links_for(a, direction="in", relation=active)) == 1
    assert len(store.links_for(b, direction="out", relation=active)) == 1
    assert len(store.links_for(a, direction="both")) == 1

    assert store.remove_link(src_ref_id=a, dst_ref_id=b, relation=passive) == 1
    assert _rows(store, a, b) == []


@pytest.mark.parametrize(("passive", "active"), PAIRS)
def test_handler_ack_names_the_stored_edge(
    hub: Hub, store: Store, passive: str, active: str
) -> None:
    h = MemoryHandler(hub=hub)
    mem = id_of(h.put(text="a memory").body)
    paper = store.insert_ref(kind="paper", slug="flip2020", title="Flip", meta={}).id

    ack = h.link(id=mem, target="paper:flip2020", rel=passive)
    assert ack.body == f"linked paper:flip2020 {active} memory id={mem}"
    assert _rows(store, mem, paper) == [(paper, mem, active)]

    rm = h.link(id=mem, target="paper:flip2020", rel=passive, mode="remove")
    assert rm.body == (
        f"unlinked paper:flip2020 {active} memory id={mem} (1 edge removed)"
    )
    assert _rows(store, mem, paper) == []


def test_active_spelling_ack_is_unchanged(hub: Hub, store: Store) -> None:
    h = MemoryHandler(hub=hub)
    mem = id_of(h.put(text="a memory").body)
    store.insert_ref(kind="paper", slug="flip2020", title="Flip", meta={})
    ack = h.link(id=mem, target="paper:flip2020", rel="cites")
    assert ack.body == f"linked memory id={mem} → paper:flip2020"


class TestHasDraftConstraint:
    """The constraint validator runs on the passive spelling before the
    store flips it: ``has-draft`` is checked as the ``draft-of`` edge."""

    def test_has_draft_is_accepted_and_stored_as_draft_of(self, store: Store) -> None:
        proj = _ref(store, "todo", "project")
        draft = _ref(store, "draft", "d one", slug="done")
        assert apply_link_ops(
            store, proj, link="draft:done", unlink=None, rel="has-draft"
        ) == (1, 0, [])
        assert _rows(store, proj, draft) == [(draft, proj, "draft-of")]

    def test_functional_rule_holds_through_the_passive_spelling(
        self, store: Store
    ) -> None:
        proj = _ref(store, "todo", "project")
        _ref(store, "draft", "d one", slug="done")
        _ref(store, "draft", "d two", slug="dtwo")
        apply_link_ops(store, proj, link="draft:done", unlink=None, rel="has-draft")
        with pytest.raises(BadInput, match="functional"):
            apply_link_ops(store, proj, link="draft:dtwo", unlink=None, rel="has-draft")


# ── migration 0325 ──────────────────────────────────────────────────


@pytest.fixture
def conn(store: Store) -> Iterator[psycopg.Connection[dict[str, Any]]]:
    c = psycopg.connect(_active_dsn(), autocommit=True, row_factory=dict_row)
    try:
        yield c
    finally:
        c.close()


def _migrate(conn: psycopg.Connection[dict[str, Any]]) -> None:
    sql = (MIGRATIONS_DIR / "0325_links_passive_spellings_flip.sql").read_text(
        encoding="utf-8"
    )
    conn.execute(sql)


def _insert_literal(
    conn: psycopg.Connection[dict[str, Any]], src: int, dst: int, relation: str
) -> int:
    row = conn.execute(
        "INSERT INTO links (src_ref_id, dst_ref_id, relation, set_by, meta) "
        "VALUES (%s, %s, %s, 'agent', '{\"k\": 1}') RETURNING link_id",
        (src, dst, relation),
    ).fetchone()
    assert row is not None
    return int(row["link_id"])


@pytest.mark.db
def test_migration_flips_literal_passive_rows(
    store: Store, conn: psycopg.Connection[dict[str, Any]]
) -> None:
    seeded = []
    for i, (passive, active) in enumerate(PAIRS):
        a = _ref(store, "memory", f"a{i}")
        b = _ref(store, "memory", f"b{i}")
        lid = _insert_literal(conn, a, b, passive)
        seeded.append((a, b, active, lid))

    _migrate(conn)
    for a, b, active, lid in seeded:
        rows = conn.execute(
            "SELECT link_id, src_ref_id, dst_ref_id, relation, meta FROM links "
            "WHERE %s IN (src_ref_id, dst_ref_id)",
            (a,),
        ).fetchall()
        assert [(r["src_ref_id"], r["dst_ref_id"], r["relation"]) for r in rows] == [
            (b, a, active)
        ]
        assert rows[0]["link_id"] == lid  # flipped in place
        assert rows[0]["meta"] == {"k": 1}

    # idempotent: a replay changes nothing
    before = conn.execute("SELECT count(*) AS n FROM links").fetchone()
    _migrate(conn)
    after = conn.execute("SELECT count(*) AS n FROM links").fetchone()
    assert before == after
    left = conn.execute(
        "SELECT count(*) AS n FROM links WHERE relation = ANY(%s)",
        ([p for p, _ in PAIRS],),
    ).fetchone()
    assert left is not None and left["n"] == 0


@pytest.mark.db
def test_migration_drops_a_passive_row_whose_twin_exists(
    store: Store, conn: psycopg.Connection[dict[str, Any]]
) -> None:
    a = _ref(store, "memory", "a")
    b = _ref(store, "memory", "b")
    _insert_literal(conn, b, a, "cites")
    _insert_literal(conn, a, b, "cited-by")

    _migrate(conn)
    rows = conn.execute(
        "SELECT src_ref_id, dst_ref_id, relation FROM links "
        "WHERE %s IN (src_ref_id, dst_ref_id)",
        (a,),
    ).fetchall()
    assert [(r["src_ref_id"], r["dst_ref_id"], r["relation"]) for r in rows] == [
        (b, a, "cites")
    ]
    # the vocabulary rows are untouched
    n = conn.execute(
        "SELECT count(*) AS n FROM relations WHERE slug = 'cited-by'"
    ).fetchone()
    assert n is not None and n["n"] == 1
