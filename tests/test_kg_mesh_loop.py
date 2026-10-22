"""kg-mesh-loop slice 1 — cluster selector + coverage metrics.

``select_clusters`` / ``collect_stats`` run plain SELECTs over the test
DB; the fixtures below hand-shape the Pd-hydride cluster (quest, ten
finding hubs reachable only through their serving papers' evidence edges
— the prod gap where no hub links to the quest — plus paper chunks and a
few stance edges) and a hand-computed mini cluster for the metrics.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from precis.graph.mesh_loop.clusters import select_clusters
from precis.graph.mesh_loop.stats import collect_stats
from precis.store import Tag
from precis.store.types import ChunkInsert


def _quest(store: Any, title: str, *, prio: int | None = None) -> Any:
    q = store.insert_ref(kind="quest", slug=None, title=title, prio=prio)
    store.add_tag(q.id, Tag.closed("STATUS", "active"), set_by="system")
    return q


def _finding(store: Any, title: str) -> Any:
    return store.insert_ref(kind="finding", slug=None, title=title)


def _paper(store: Any, slug: str, *, serves: Any = None) -> Any:
    p = store.insert_ref(kind="paper", slug=slug, title=slug)
    if serves is not None:
        store.add_link(src_ref_id=p.id, dst_ref_id=serves.id, relation="serves")
    return p


def _pd_cluster(store: Any, *, prio: int | None = 5) -> tuple[Any, list[Any]]:
    """The Pd-hydride shape: a quest, 10 finding hubs, serving papers
    carrying chunks — and NO direct quest<->finding link. Nine hubs join
    via paper evidence edges; the tenth only through a member hub
    (fi504780's real shape: its paper serves no quest)."""
    q = _quest(store, "Pd(111) hydride mesh", prio=prio)
    papers = [_paper(store, f"pdh{i:02d}", serves=q) for i in range(3)]
    for p in papers:
        store.chunks.insert_chunks(
            p.id, [ChunkInsert(ord=0, text=f"numbers for {p.slug}")]
        )
    hubs = [_finding(store, f"pd hub {i}") for i in range(10)]
    for i, h in enumerate(hubs[:9]):
        store.add_link(
            src_ref_id=papers[i % 3].id, dst_ref_id=h.id, relation="corroborates"
        )
    store.add_link(src_ref_id=hubs[8].id, dst_ref_id=hubs[9].id, relation="qualifies")
    # a "handful of stance edges" inside the set
    store.add_link(src_ref_id=hubs[0].id, dst_ref_id=hubs[1].id, relation="disputes")
    store.add_link(src_ref_id=hubs[2].id, dst_ref_id=hubs[3].id, relation="supports")
    return q, hubs


class TestSelector:
    def test_pd_cluster_selects_first(self, store: Any) -> None:
        pd_q, _ = _pd_cluster(store)
        # distractors that must not outrank or qualify
        small = _quest(store, "thin quest", prio=1)
        for i in range(4):
            f = _finding(store, f"thin hub {i}")
            store.add_link(src_ref_id=f.id, dst_ref_id=small.id, relation="supports")
        dense = _quest(store, "fully meshed quest", prio=2)
        dh = [_finding(store, f"dense hub {i}") for i in range(9)]
        for i in range(9):
            store.add_link(
                src_ref_id=dh[i].id,
                dst_ref_id=dh[(i + 1) % 9].id,
                relation="disputes",
            )
            store.add_link(
                src_ref_id=dh[i].id, dst_ref_id=dense.id, relation="supports"
            )
        store.insert_ref(kind="quest", slug=None, title="no STATUS")
        dormant = store.insert_ref(kind="quest", slug=None, title="dormant")
        store.add_tag(dormant.id, Tag.closed("STATUS", "dormant"), set_by="system")

        with store.pool.connection() as conn:
            rows = select_clusters(conn)

        assert [r.quest_id for r in rows] == [pd_q.id]
        c = rows[0]
        assert c.hubs == 10
        assert c.stance_edges == 3  # disputes + supports + the qualifies hop
        assert c.stance_density == 0.3
        assert c.prio == 5

    def test_ranking_order(self, store: Any) -> None:
        def _qualifying(
            name: str, prio: int, stance_pairs: list[tuple[int, int]]
        ) -> tuple[Any, list[Any]]:
            q = _quest(store, name, prio=prio)
            hs = [_finding(store, f"{name} hub {i}") for i in range(8)]
            for h in hs:
                store.add_link(src_ref_id=h.id, dst_ref_id=q.id, relation="supports")
            for a, b in stance_pairs:
                store.add_link(
                    src_ref_id=hs[a].id,
                    dst_ref_id=hs[b].id,
                    relation="disputes",
                )
            return q, hs

        q_urgent, _ = _qualifying("urgent dense", 2, [(0, 1), (2, 3), (4, 5)])
        q_flat_a, _ = _qualifying("flat a", 7, [(0, 1)])
        q_flat_b, _ = _qualifying("flat b", 7, [(0, 1)])
        # same prio, same density: the newest serving paper wins
        pa = _paper(store, "old-paper", serves=q_flat_a)
        pb = _paper(store, "new-paper", serves=q_flat_b)
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE refs SET created_at = '2026-01-01'::timestamptz "
                "WHERE ref_id = %s",
                (pa.id,),
            )
            conn.execute(
                "UPDATE refs SET created_at = '2026-06-01'::timestamptz "
                "WHERE ref_id = %s",
                (pb.id,),
            )

        with store.pool.connection() as conn:
            rows = select_clusters(conn)

        assert [r.quest_id for r in rows] == [q_urgent.id, q_flat_b.id, q_flat_a.id]

    def test_membership_doors(self, store: Any) -> None:
        q = _quest(store, "membership quest")
        # door 1a: quest -> finding (the pre-fix supports shape)
        direct_out = _finding(store, "direct out")
        store.add_link(src_ref_id=q.id, dst_ref_id=direct_out.id, relation="supports")
        # door 1b: finding -> quest serves (the post-fix direction)
        direct_in = _finding(store, "direct in")
        store.add_link(src_ref_id=direct_in.id, dst_ref_id=q.id, relation="serves")
        # door 2: claim edge to a serving paper
        p = _paper(store, "server", serves=q)
        via_paper = _finding(store, "via paper")
        store.add_link(
            src_ref_id=p.id, dst_ref_id=via_paper.id, relation="corroborates"
        )
        # door 3: one hub->hub hop
        via_hub = _finding(store, "via hub")
        store.add_link(
            src_ref_id=via_paper.id, dst_ref_id=via_hub.id, relation="qualifies"
        )
        # NOT a member: weak edge to a serving paper
        weak = _finding(store, "weak")
        store.add_link(src_ref_id=p.id, dst_ref_id=weak.id, relation="related-to")
        # NOT a member: retired hub
        retired = _finding(store, "retired")
        store.add_link(src_ref_id=retired.id, dst_ref_id=q.id, relation="supports")
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE refs SET retired_at = now() WHERE ref_id = %s",
                (retired.id,),
            )
            from precis.graph.mesh_loop.clusters import hub_membership

            members = {h for qq, h in hub_membership(conn) if qq == q.id}

        assert members == {
            direct_out.id,
            direct_in.id,
            via_paper.id,
            via_hub.id,
        }

    def test_quest_pin_selects_regardless_of_rank(self, store: Any) -> None:
        from precis.graph.mesh_loop.clusters import cluster_for_quest

        pd_q, _ = _pd_cluster(store, prio=5)
        hotter = _quest(store, "hotter cluster", prio=2)
        for i in range(8):
            f = _finding(store, f"hot hub {i}")
            store.add_link(src_ref_id=f.id, dst_ref_id=hotter.id, relation="serves")

        with store.pool.connection() as conn:
            ranked = select_clusters(conn)
            assert [r.quest_id for r in ranked] == [hotter.id, pd_q.id]
            pinned = cluster_for_quest(conn, pd_q.id)

        assert pinned.quest_id == pd_q.id
        assert pinned.hubs == 10

    def test_quest_pin_refuses_unqualified_and_missing(self, store: Any) -> None:
        from precis.errors import BadInput
        from precis.graph.mesh_loop.clusters import cluster_for_quest

        thin = _quest(store, "thin quest", prio=1)
        for i in range(4):
            f = _finding(store, f"thin hub {i}")
            store.add_link(src_ref_id=f.id, dst_ref_id=thin.id, relation="serves")

        with store.pool.connection() as conn:
            with pytest.raises(BadInput, match="4 hubs < 8"):
                cluster_for_quest(conn, thin.id)
            with pytest.raises(BadInput, match="no live quest"):
                cluster_for_quest(conn, 999999)

        dormant = store.insert_ref(kind="quest", slug=None, title="dormant")
        store.add_tag(dormant.id, Tag.closed("STATUS", "dormant"), set_by="system")
        with store.pool.connection() as conn:
            with pytest.raises(BadInput, match="no STATUS:active"):
                cluster_for_quest(conn, dormant.id)


def _review(store: Any, target: int, actor: str, verdict: str, **kg: Any) -> None:
    note = json.dumps({"kg_mesh": kg}, sort_keys=True)
    store.record_target_review("ref", target, actor=actor, verdict=verdict, note=note)


class TestStats:
    def test_hand_computed_cluster(self, store: Any) -> None:
        q = _quest(store, "mini cluster")
        hubs = [_finding(store, f"m hub {i}") for i in range(3)]
        for h in hubs:
            store.add_link(src_ref_id=h.id, dst_ref_id=q.id, relation="serves")
        # 1 stance edge among 3 hubs -> density 1/3
        store.add_link(
            src_ref_id=hubs[0].id, dst_ref_id=hubs[1].id, relation="disputes"
        )
        # 2 stamped (one stamp goes stale after a title edit), 1 unstamped
        _review(store, hubs[0].id, "kg-mesh", "approved", kind="stamp", quest=q.id)
        _review(store, hubs[1].id, "kg-mesh", "approved", kind="stamp", quest=q.id)
        store.update_ref(hubs[1].id, title="m hub 1 (edited)")
        # ledger rows hang on the quest ref: 2 proposals; key a approved,
        # key b rejected (= reverted)
        _review(
            store,
            q.id,
            "kg-mesh-propose",
            "proposed",
            quest=q.id,
            key="disputes:finding:1:2",
            run_id="run-1",
        )
        _review(
            store,
            q.id,
            "kg-mesh-propose",
            "proposed",
            quest=q.id,
            key="supports:finding:3:1",
            run_id="run-1",
        )
        _review(
            store,
            q.id,
            "kg-mesh-write",
            "approved",
            quest=q.id,
            key="disputes:finding:1:2",
            run_id="run-1",
        )
        _review(
            store,
            q.id,
            "kg-mesh-write",
            "rejected",
            quest=q.id,
            key="supports:finding:3:1",
            run_id="run-1",
        )
        with store.pool.connection() as conn:
            conn.execute(
                "INSERT INTO llm_call_log (source, features, input_tokens, "
                "output_tokens) VALUES ('kg_mesh:write', %s::jsonb, %s, %s)",
                (json.dumps({"run_id": "run-1"}), 4000, 2000),
            )
            rep = collect_stats(conn)

        assert rep.active_quests == 1
        assert rep.clusters_left == 0  # 3 hubs < 8
        row = rep.rows[0]
        assert row.cluster.quest_id == q.id
        assert row.cluster.hubs == 3
        assert row.stamped_hubs == 1  # the edited hub's stamp went stale
        assert row.cluster.stance_edges == 1
        assert abs(row.cluster.stance_density - 1 / 3) < 1e-9
        assert row.proposed == 2
        assert row.accepted == 1
        assert row.reverted == 1
        assert row.writer_tokens == 6000
        assert row.tokens_per_accepted == 6000.0
        assert row.agreement == 0.5

    def test_stats_counts_pd_cluster(self, store: Any) -> None:
        pd_q, _ = _pd_cluster(store)
        with store.pool.connection() as conn:
            rep = collect_stats(conn)
        row = next(r for r in rep.rows if r.cluster.quest_id == pd_q.id)
        assert row.cluster.hubs == 10
        assert rep.clusters_left == 1


class TestProdReadOnly:
    """``--prod`` goes through ``_prod_ro_dsn``: the agent_ro rewrite plus
    the verified-role guard — a write-capable role refuses."""

    def test_writer_role_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from precis.cli.mesh import _prod_ro_dsn

        secrets = tmp_path / ".secrets" / "pw"
        secrets.mkdir(parents=True)
        (secrets / "PRECIS_DATABASE_URL").write_text(
            # fake credential, built at runtime for the secrets scanner
            "postgresql://agent_rw:" + "nox" + "@127.0.0.1:6432/precis",
            encoding="utf-8",
        )
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        class _Conn:
            def __enter__(self) -> _Conn:
                return self

            def __exit__(self, *a: object) -> None:
                return None

            def execute(self, sql: str) -> _Conn:
                return self

            def fetchone(self) -> tuple[str, bool]:
                # the rewrite asked for agent_ro but the DB answers a
                # writer with INSERT — the guard must refuse
                return ("precis_test", True)

        monkeypatch.setattr("psycopg.connect", lambda *a, **kw: _Conn())

        # _prod_ro_dsn loads readonly_dsn.py by file path, so the
        # ReadOnlyRefused class object is fresh per call — match the
        # refusal by name and message rather than class identity.
        with pytest.raises(RuntimeError) as ei:
            _prod_ro_dsn()
        assert type(ei.value).__name__ == "ReadOnlyRefused"
        assert "agent_ro" in str(ei.value)

    def test_loop_clusters_parses_and_dispatches(self) -> None:
        from precis.cli.main import _build_parser

        args = _build_parser(("mesh", "loop", "clusters")).parse_args(
            ["mesh", "loop", "clusters", "--prod", "--min-hubs", "12"]
        )
        assert args.mesh_cmd == "loop"
        assert args.loop_cmd == "clusters"
        assert args.prod is True
        assert args.min_hubs == 12
