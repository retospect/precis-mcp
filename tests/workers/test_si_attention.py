"""SI attention trigger: the shared helper, the MCP get hook, claim ordering."""

from __future__ import annotations

from typing import Any

from psycopg.types.json import Jsonb

from precis.store.si_links import queue_si_on_attention
from precis.workers import si_fetch

DOI = "10.1021/acscatal.3c01963"


def _seed(store, slug="smith2023cat", doi: str | None = DOI) -> int:
    ref = store.insert_ref(kind="paper", slug=slug, title="Parent", meta={})
    if doi:
        with store.pool.connection() as conn:
            conn.execute(
                "INSERT INTO ref_identifiers (ref_id, id_kind, id_value, source) "
                "VALUES (%s, 'doi', %s, 'manual')",
                (ref.id, doi),
            )
            conn.commit()
    return ref.id


def _meta(store, ref_id: int) -> dict[str, Any]:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    assert row is not None
    return row[0]


def _sql(store, sql: str, params: tuple[Any, ...]) -> None:
    with store.pool.connection() as conn:
        conn.execute(sql, params)
        conn.commit()


def test_queues_once_and_dedupes(store) -> None:
    pid = _seed(store)
    assert queue_si_on_attention(store, pid, "web") is True
    first = _meta(store, pid)["si_fetch"]
    assert first["by"] == "web"
    assert first["trigger"] == "attention"
    assert first["requested_at"].endswith("Z")
    assert queue_si_on_attention(store, pid, "mcp_get") is False
    assert _meta(store, pid)["si_fetch"] == first


def test_skips_ineligible_papers(store) -> None:
    supp = _seed(store, slug="supp1", doi=DOI + "s")
    _sql(store, "UPDATE refs SET pdf_role = 'supplement' WHERE ref_id = %s", (supp,))
    retired = _seed(store, slug="gone1", doi=DOI + "g")
    _sql(store, "UPDATE refs SET retired_at = now() WHERE ref_id = %s", (retired,))
    nodoi = _seed(store, slug="nodoi1", doi=None)
    checked = _seed(store, slug="checked1", doi=DOI + "k")
    _sql(
        store,
        "UPDATE refs SET meta = meta || jsonb_build_object('si_checked_at', 'x') "
        "WHERE ref_id = %s",
        (checked,),
    )
    for rid in (supp, retired, nodoi, checked):
        assert queue_si_on_attention(store, rid, "web") is False
        assert "si_fetch" not in _meta(store, rid)


def test_off_switch(store, monkeypatch) -> None:
    pid = _seed(store)
    monkeypatch.setenv("PRECIS_SI_ATTENTION", "0")
    assert queue_si_on_attention(store, pid, "web") is False
    assert "si_fetch" not in _meta(store, pid)


def test_db_error_returns_false() -> None:
    class Boom:
        @property
        def pool(self) -> Any:
            raise RuntimeError("db down")

    assert queue_si_on_attention(Boom(), 1, "web") is False


def test_mcp_get_overview_queues_once(store) -> None:
    from precis.dispatch import Hub
    from precis.embedder import MockEmbedder
    from precis.handlers.paper import PaperHandler

    pid = _seed(store)
    handler = PaperHandler(hub=Hub(store=store, embedder=MockEmbedder(dim=1024)))
    handler.get(id="smith2023cat")
    first = _meta(store, pid)["si_fetch"]
    assert first["by"] == "mcp_get"
    handler.get(id="smith2023cat")
    assert _meta(store, pid)["si_fetch"] == first


def test_claim_orders_explicit_before_attention(store) -> None:
    att_old = _seed(store, slug="att1")
    att_new = _seed(store, slug="att2", doi=DOI + "b")
    exp = _seed(store, slug="exp1", doi=DOI + "c")
    for rid, at, trigger in (
        (att_old, "2026-01-01T00:00:00.000000Z", "attention"),
        (att_new, "2026-01-02T00:00:00.000000Z", "attention"),
        (exp, "2026-06-01T00:00:00.000000Z", None),
    ):
        body: dict[str, str] = {"requested_at": at, "by": "x"}
        if trigger:
            body["trigger"] = trigger
        _sql(
            store,
            "UPDATE refs SET meta = meta || jsonb_build_object('si_fetch', %s::jsonb) "
            "WHERE ref_id = %s",
            (Jsonb(body), rid),
        )
    with store.pool.connection() as conn:
        got = si_fetch.claim_si_parents(conn, limit=3)
        conn.commit()
    assert [p.ref_id for p in got] == [exp, att_old, att_new]


def test_many_dedupes_and_returns_counts(store) -> None:
    from precis.store.si_links import queue_si_on_attention_many

    ids = [_seed(store, slug=f"m{i}", doi=f"{DOI}m{i}") for i in range(3)]
    assert queue_si_on_attention_many(store, ids, "walker") == 3
    assert queue_si_on_attention_many(store, ids, "walker") == 0
    assert queue_si_on_attention_many(store, [], "walker") == 0


def test_db_tier_off_switch(store) -> None:
    from precis import settings

    pid = _seed(store)
    settings.set_setting("si.attention_enabled", "false", store=store)
    try:
        assert queue_si_on_attention(store, pid, "web") is False
        assert "si_fetch" not in _meta(store, pid)
    finally:
        settings.set_setting("si.attention_enabled", "true", store=store)


def test_fetch_si_rearms_attention_checked_paper(store) -> None:
    from precis.dispatch import Hub
    from precis.embedder import MockEmbedder
    from precis.handlers.paper import PaperHandler

    pid = _seed(store)
    assert queue_si_on_attention(store, pid, "web") is True
    with store.pool.connection() as conn:
        assert [p.ref_id for p in si_fetch.claim_si_parents(conn)] == [pid]
        conn.commit()
    with store.pool.connection() as conn:
        assert si_fetch.claim_si_parents(conn) == []
        conn.commit()
    before = _meta(store, pid)["si_fetch"]["requested_at"]
    handler = PaperHandler(hub=Hub(store=store, embedder=MockEmbedder(dim=1024)))
    handler.put(id="smith2023cat", mode="fetch-si")
    after = _meta(store, pid)["si_fetch"]
    assert after["requested_at"] > before and after["by"] == "agent"
    assert "trigger" not in after
    with store.pool.connection() as conn:
        assert [p.ref_id for p in si_fetch.claim_si_parents(conn)] == [pid]
        conn.commit()
