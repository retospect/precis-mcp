"""Closed-chain sampling, atomic minting, and the model-free audit brief."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest

from precis.store import Store
from precis.store.types import Tag
from precis.workers import transcript_audit as ta
from tests._lane_rows import seed_lane, use_lanes


def _chain(
    store: Store,
    *,
    status: str = "done",
    age: int = 1,
    job: bool = True,
    sampled: bool = True,
) -> int:
    root = store.insert_ref(kind="todo", slug=None, title="Factory chain")
    # Walk the sequence rather than assuming a test database's starting id.
    while (root.id % 100 < 10) != sampled:
        root = store.insert_ref(kind="todo", slug=None, title="Factory chain")
    for tag in (f"plan:{root.id}", f"STATUS:{status}"):
        store.add_tag(
            root.id,
            Tag.parse_strict(tag, kind="todo"),
            set_by="agent",
            replace_prefix=tag.startswith("STATUS:"),
        )
    if job:
        child = store.insert_ref(
            kind="todo", slug=None, title="Build", parent_id=root.id
        )
        store.insert_ref(
            kind="job",
            slug=None,
            title="Build job",
            parent_id=child.id,
            meta={"job_type": "run_slice", "lane": "codex"},
        )
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET updated_at = now() - %s * interval '1 day' "
            "WHERE ref_id = %s",
            (age, root.id),
        )
    return root.id


def _light(store: Store, prio: int = 1, host: str = "*") -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO service_config (host, service, prio) "
            "VALUES (%s, 'transcript_audit', %s) ON CONFLICT (host, service) "
            "DO UPDATE SET prio = EXCLUDED.prio",
            (host, prio),
        )


def test_candidates(store: Store) -> None:
    yes = _chain(store)
    no = _chain(store, sampled=False)
    wont = _chain(store, status="won't-do", age=2)
    _chain(store, status="open")
    _chain(store, job=False)
    _chain(store, age=8)
    with store.pool.connection() as conn:
        assert ta.candidates(conn, datetime.now(UTC), 10) == [wont, yes]
        assert ta.candidates(conn, datetime.now(UTC), 0) == []
        assert no in ta.candidates(conn, datetime.now(UTC), 100)


def test_mint_and_second_fire(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    chain = _chain(store)
    _light(store)
    monkeypatch.setenv("PRECIS_TRANSCRIPT_AUDIT_PERCENT", "100")
    result = ta.run_transcript_audit_pass(store, host="h1")
    assert (result.claimed, result.ok, result.failed) == (1, 1, 0)
    root_id = result.extra["minted"][0]["root"]
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id, parent_id, meta FROM refs "
            "WHERE ref_id = %s OR parent_id = %s ORDER BY ref_id",
            (root_id, root_id),
        ).fetchall()
        assert len(rows) == 2
        assert rows[0][1] is None and rows[1][1] == root_id
        assert rows[1][2]["op"] == {
            "type": "run_slice",
            "params": {"lane": "claude-sub", "lane_capability": "mcp"},
        }
        assert all("llm_tier" not in row[2] for row in rows)
        for row in rows:
            assert f"plan:{root_id}" in [t.value for t in store.tags_for(row[0])]
        assert conn.execute(
            "SELECT 1 FROM links WHERE src_ref_id = %s "
            "AND dst_ref_id = %s AND relation = 'related-to'",
            (root_id, chain),
        ).fetchone()
        notes = conn.execute(
            "SELECT text FROM chunks WHERE ref_id = %s AND chunk_kind = 'todo_comment'",
            (chain,),
        ).fetchall()
        assert notes == [(f"audit: td{root_id} samples this chain's transcripts",)]
    assert ta.run_transcript_audit_pass(store, host="h1").claimed == 0


def test_audit_task_falls_back_when_the_pin_is_dead(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr604579: the audit task's ``claude-sub`` pin gets the same
    mint-time ``pick_lane`` fallback as the rescue diagnose — the pick
    stays on write-MCP lanes (a free code-fit lane like devin is NOT
    one: the audit files gripes through the MCP)."""
    sub = replace(seed_lane("claude-sub"), enabled=False)
    alt = replace(seed_lane("claude-sub"), name="claude-sub-2")
    use_lanes(monkeypatch, seed_lane("devin"), seed_lane("codex"), sub, alt)
    _chain(store)
    _light(store)
    monkeypatch.setenv("PRECIS_TRANSCRIPT_AUDIT_PERCENT", "100")

    result = ta.run_transcript_audit_pass(store, host="h1")

    assert result.ok == 1
    root_id = result.extra["minted"][0]["root"]
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta FROM refs WHERE parent_id = %s", (root_id,)
        ).fetchone()
        assert row is not None
        assert row[0]["op"] == {
            "type": "run_slice",
            "params": {"lane": "claude-sub-2", "lane_capability": "mcp"},
        }
        comment = conn.execute(
            "SELECT c.text FROM chunks c JOIN refs r ON r.ref_id = c.ref_id "
            "WHERE r.parent_id = %s AND c.chunk_kind = 'todo_comment'",
            (root_id,),
        ).fetchone()
        assert comment is not None and "claude-sub-2" in str(comment[0])


def test_audit_task_stays_mcp_bound_when_no_mcp_lane_is_free(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """td610544 (td610252 review): with every write-MCP lane down the
    audit still mints bound — ``params.lane_capability: "mcp"`` — so
    submit's lane-wait park holds it. An unpinned ``code`` task would
    pick a code-fit lane like devin and launch with no write MCP to
    file the gripes through."""
    sub = replace(seed_lane("claude-sub"), enabled=False)
    use_lanes(monkeypatch, seed_lane("devin"), seed_lane("codex"), sub)
    _chain(store)
    _light(store)
    monkeypatch.setenv("PRECIS_TRANSCRIPT_AUDIT_PERCENT", "100")

    result = ta.run_transcript_audit_pass(store, host="h1")

    assert result.ok == 1
    root_id = result.extra["minted"][0]["root"]
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta FROM refs WHERE parent_id = %s", (root_id,)
        ).fetchone()
        assert row is not None
        assert row[0]["op"] == {
            "type": "run_slice",
            "params": {"lane_capability": "mcp"},
        }


@pytest.mark.parametrize(
    "percent, expected", [("0", 0), ("-1", 0), ("100", 1), ("150", 1)]
)
def test_percent_override(
    store: Store, monkeypatch: pytest.MonkeyPatch, percent: str, expected: int
) -> None:
    _chain(store, sampled=False)
    _light(store)
    monkeypatch.setenv("PRECIS_TRANSCRIPT_AUDIT_PERCENT", percent)
    assert ta.run_transcript_audit_pass(store, host="h1").ok == expected


def test_enablement(store: Store) -> None:
    assert not ta.transcript_audit_enabled(store, host="h1")
    assert ta.run_transcript_audit_pass(store, host="h1").claimed == 0
    _light(store)
    _light(store, 0, "dim")
    assert ta.transcript_audit_enabled(store, host="h1")
    assert not ta.transcript_audit_enabled(store, host="dim")
    with store.pool.connection() as conn:
        assert ta.transcript_audit_enabled(store, conn=conn, host="h1")
        assert not ta.transcript_audit_enabled(store, conn=conn, host="dim")


def test_cap_and_oldest_first(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    newer = _chain(store)
    older = _chain(store, age=3)
    _light(store)
    monkeypatch.delenv("PRECIS_TRANSCRIPT_AUDIT_PERCENT", raising=False)
    result = ta.run_transcript_audit_pass(store, host="h1")
    assert result.ok == 1 and result.extra["minted"][0]["chain"] == older
    assert result.extra["percent"] == 10
    assert (
        ta.run_transcript_audit_pass(store, host="h1").extra["minted"][0]["chain"]
        == newer
    )


def test_brief(store: Store) -> None:
    root = store.get_ref(kind="todo", id=_chain(store))
    assert root is not None
    jobs: list[dict[str, Any]] = [
        {
            "id": 123,
            "meta": {
                "job_type": "run_slice",
                "lane": "codex",
                "model": "example",
                "wall_s": 12,
            },
            "status": "done",
        },
        {"id": 124, "meta": {"job_type": "land_slice"}, "status": "failed"},
    ]
    brief = ta.render_brief(root, jobs)
    assert "jo123 run_slice lane=codex model=example status=done wall=12s" in brief
    assert "jo124 land_slice lane=? model=? status=failed wall=?s" in brief
    assert "First-run rule:" in brief and "do not guess at what the agent did" in brief
    assert (
        "must have cost five or more tool calls or two or more identical retries."
        in brief
    )
    assert "At most three gripes per audit." in brief
    assert "rel='supported-by'" in brief and "No commits." in brief


def test_cadence() -> None:
    from precis.workers.registry import SERVICES_BY_NAME
    from precis.workers.scheduler import CADENCES

    cadence = next(c for c in CADENCES if c.name == ta.SERVICE_NAME)
    assert cadence.interval_s == 1800
    assert cadence.service is None and cadence.spends is False
    assert SERVICES_BY_NAME[ta.SERVICE_NAME].uses_model is False
