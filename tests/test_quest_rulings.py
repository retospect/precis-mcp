"""Code-minted measurement rulings (``src/precis/quest/rulings.py`` —
quest-dossier-dialectic §Mechanism, the sims→findings anchor).

Covers: the mint happy path (templated finding, entry-chunk bookmark,
logbook entry), the trust gate (untrusted/missing barrier never mints),
settled/refuted blocks skipped, idempotency (bookmark skip + the
``sim_ruling_key`` converge belt), the ``tests`` edge when the measuring
pathway is findable (migration 0142), the ``measured:`` render line, and
the tick pre-pass wiring (``outcome.rulings_minted`` + the ruling visible
in the same tick's prompt). Runs against real PG (the ``store`` fixture).

The pathway kind row is seeded directly (``ON CONFLICT DO NOTHING``) so
these tests never import ``precis_pathway``/``autocatpath`` — all they
need is a refs row of kind ``pathway`` for the FK, not plugin behavior.
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any

from precis.dispatch import Hub
from precis.handlers.quest import QuestHandler
from precis.quest.dossier import apply_dialectic_op, read_dialectic
from precis.quest.rulings import mint_measurement_rulings
from precis.quest.tick import run_quest_tick
from tests.workers._helpers import seed_ref


def _mk_quest(store: Any, text: str) -> int:
    h = QuestHandler(hub=Hub(store=store))
    resp = h.put(text=text)
    m = re.search(r"\bqu(\d+)\b", resp.body)
    assert m is not None, resp.body
    return int(m.group(1))


def _mk_structure(
    store: Any, *, barrier: float | None, trusted: bool, tier: str = "neb"
) -> int:
    st = seed_ref(store, title="Pd(111) + Mo adatom 1/9", kind="structure")
    meta: dict[str, Any] = {"barrier_trusted": trusted, "barrier_tier": tier}
    if barrier is not None:
        meta["barrier"] = barrier
    store.stamp_ref_meta(st, meta)
    return st


def _preregister(store: Any, qid: int, hyp: int, st: int) -> None:
    ok = apply_dialectic_op(
        store,
        qid,
        {
            "op": "experiment",
            "hypothesis": f"fi{hyp}",
            "text": f"Relax + NEB [st{st}] under the ammonia network",
            "predicts": "barrier < 1.0 eV supports; >= 1.0 eV counters",
        },
    )
    assert ok


def _mk_pathway(store: Any, st: int, tier: str = "neb") -> int:
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO kinds (slug, is_numeric, title, description) VALUES "
            "('pathway', FALSE, 'Reaction pathway (test seed)', 'test seed') "
            "ON CONFLICT (slug) DO NOTHING"
        )
        conn.commit()
    pw = seed_ref(store, title="pathway run", kind="pathway")
    store.stamp_ref_meta(pw, {"candidate_ref": st, "tier": tier})
    return pw


def _ruling_ids(store: Any, hyp: int) -> list[int]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id FROM refs WHERE kind = 'finding' "
            "AND deleted_at IS NULL AND meta->>'hypothesis' = %s "
            "AND (meta->>'sim_ruling')::boolean IS TRUE ORDER BY ref_id",
            (str(hyp),),
        ).fetchall()
    return [int(r[0]) for r in rows]


class TestMint:
    def test_trusted_measurement_mints_templated_ruling(self, store: Any) -> None:
        qid = _mk_quest(store, "NO to NH3 on Pd(111)")
        hyp = seed_ref(store, title="Mo lowers the RLS barrier", kind="finding")
        st = _mk_structure(store, barrier=1.895, trusted=True)
        _preregister(store, qid, hyp, st)

        assert mint_measurement_rulings(store, qid) == 1
        (fid,) = _ruling_ids(store, hyp)
        ref = store.get_ref(kind="finding", id=fid)
        assert "1.895 eV" in ref.title
        assert "trusted" in ref.title
        meta = ref.meta or {}
        assert meta.get("sim_ruling") is True
        assert meta.get("structure") == st
        assert meta.get("quest") == qid
        # The body is the templated text: handles + the internal-only fence.
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT text FROM chunks WHERE ref_id = %s AND ord = 0", (fid,)
            ).fetchone()
        assert row is not None
        body = row[0]
        assert f"[st{st}]" in body
        assert f"[fi{hyp}]" in body
        assert "no LLM authored it" in body
        assert "never nanopub evidence" in body

    def test_untrusted_or_barrierless_structure_never_mints(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        hyp = seed_ref(store, kind="finding")
        st_untrusted = _mk_structure(store, barrier=0.6, trusted=False)
        _preregister(store, qid, hyp, st_untrusted)
        assert mint_measurement_rulings(store, qid) == 0

        hyp2 = seed_ref(store, kind="finding")
        st_bare = _mk_structure(store, barrier=None, trusted=True)
        _preregister(store, qid, hyp2, st_bare)
        assert mint_measurement_rulings(store, qid) == 0
        assert _ruling_ids(store, hyp) == []
        assert _ruling_ids(store, hyp2) == []

    def test_second_run_is_a_no_op(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        hyp = seed_ref(store, kind="finding")
        st = _mk_structure(store, barrier=0.994, trusted=True)
        _preregister(store, qid, hyp, st)

        assert mint_measurement_rulings(store, qid) == 1
        assert mint_measurement_rulings(store, qid) == 0
        assert len(_ruling_ids(store, hyp)) == 1

    def test_settled_block_is_skipped(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        hyp = seed_ref(store, kind="finding")
        st = _mk_structure(store, barrier=0.5, trusted=True)
        _preregister(store, qid, hyp, st)
        assert apply_dialectic_op(
            store,
            qid,
            {"op": "settle", "hypothesis": f"fi{hyp}", "text": "resolved."},
        )
        assert mint_measurement_rulings(store, qid) == 0

    def test_sim_ruling_key_converges_when_bookmark_lost(self, store: Any) -> None:
        """The entry-meta bookmark is suspenders; ``sim_ruling_key`` is the
        belt — losing the bookmark re-links the existing ruling rather than
        minting a duplicate."""
        qid = _mk_quest(store, "A striving")
        hyp = seed_ref(store, kind="finding")
        st = _mk_structure(store, barrier=0.75, trusted=True)
        _preregister(store, qid, hyp, st)
        assert mint_measurement_rulings(store, qid) == 1

        # Wipe the bookmark off THIS quest's experiment entry chunk (scoped
        # to its dossier — the test DB is shared, never wipe globally).
        from precis.quest.dossier import dossier_ref_id

        did = dossier_ref_id(store, qid)
        assert did is not None
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE chunks SET meta = meta - 'rulings' "
                "WHERE ref_id = %s AND meta->>'pinned' = 'dialectic-entry' "
                "AND meta->>'role' = 'experiment'",
                (did,),
            )
            conn.commit()
        assert mint_measurement_rulings(store, qid) == 0  # converged, not re-minted
        assert len(_ruling_ids(store, hyp)) == 1

    def test_measuring_pathway_gets_the_tests_edge(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        hyp = seed_ref(store, kind="finding")
        st = _mk_structure(store, barrier=1.2, trusted=True, tier="neb")
        pw = _mk_pathway(store, st, tier="neb")
        _preregister(store, qid, hyp, st)

        assert mint_measurement_rulings(store, qid) == 1
        (fid,) = _ruling_ids(store, hyp)
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT relation, meta FROM links "
                "WHERE src_ref_id = %s AND dst_ref_id = %s",
                (pw, hyp),
            ).fetchone()
        assert row is not None
        assert row[0] == "tests"
        assert (row[1] or {}).get("ruling") == fid
        ref = store.get_ref(kind="finding", id=fid)
        assert (ref.meta or {}).get("pathway") == pw


class TestDegradePaths:
    """The defensive arms — a bad handle, a vanished structure, a raising
    store call, or a broken pass must all skip/degrade, never crash."""

    def test_unparseable_handle_and_missing_structure_are_skipped(
        self, store: Any
    ) -> None:
        qid = _mk_quest(store, "A striving")
        hyp = seed_ref(store, kind="finding")
        # A block with NO experiment entry (open only) is skipped outright.
        assert apply_dialectic_op(store, qid, {"op": "open", "hypothesis": f"fi{hyp}"})
        hyp2 = seed_ref(store, kind="finding")
        # [zz…] parses to no kind; [st999999999] resolves to no live ref.
        ok = apply_dialectic_op(
            store,
            qid,
            {
                "op": "experiment",
                "hypothesis": f"fi{hyp2}",
                "text": "Probe [zz123] and [st999999999]",
            },
        )
        assert ok
        assert mint_measurement_rulings(store, qid) == 0

    def test_measuring_pathway_lookup_raising_degrades_to_none(
        self, store: Any, monkeypatch: Any
    ) -> None:
        from precis.quest import rulings as rulings_mod

        def _boom(*a: Any, **kw: Any) -> int:
            raise RuntimeError("pathway lookup down")

        monkeypatch.setattr("precis.quest.compute._find_tier_pathway", _boom)
        assert rulings_mod._measuring_pathway(store, 1, "neb") is None

    def test_tests_edge_failure_never_costs_the_mint(
        self, store: Any, monkeypatch: Any
    ) -> None:
        qid = _mk_quest(store, "A striving")
        hyp = seed_ref(store, kind="finding")
        st = _mk_structure(store, barrier=0.9, trusted=True, tier="neb")
        _mk_pathway(store, st, tier="neb")
        _preregister(store, qid, hyp, st)

        def _boom(*a: Any, **kw: Any) -> Any:
            raise RuntimeError("links down")

        monkeypatch.setattr(type(store), "add_link", _boom, raising=False)
        assert mint_measurement_rulings(store, qid) == 1
        assert len(_ruling_ids(store, hyp)) == 1

    def test_mint_raising_skips_the_candidate_not_the_pass(
        self, store: Any, monkeypatch: Any
    ) -> None:
        qid = _mk_quest(store, "A striving")
        hyp = seed_ref(store, kind="finding")
        st = _mk_structure(store, barrier=0.9, trusted=True)
        _preregister(store, qid, hyp, st)

        def _boom(*a: Any, **kw: Any) -> int:
            raise RuntimeError("insert down")

        monkeypatch.setattr("precis.quest.rulings._mint_ruling_finding", _boom)
        assert mint_measurement_rulings(store, qid) == 0
        assert _ruling_ids(store, hyp) == []

    def test_garbage_rulings_meta_renders_without_crash(self, store: Any) -> None:
        """A non-int bookmark value is skipped; a bookmark pointing at a
        vanished finding renders the explicit missing marker."""
        qid = _mk_quest(store, "A striving")
        hyp = seed_ref(store, kind="finding")
        st = _mk_structure(store, barrier=0.5, trusted=True)
        _preregister(store, qid, hyp, st)
        from precis.quest.dossier import dossier_ref_id

        did = dossier_ref_id(store, qid)
        assert did is not None
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE chunks SET meta = meta || "
                """'{"rulings": {"a": "garbage", "b": 999999999}}'::jsonb """
                "WHERE ref_id = %s AND meta->>'pinned' = 'dialectic-entry' "
                "AND meta->>'role' = 'experiment'",
                (did,),
            )
            conn.commit()
        md = read_dialectic(store, qid)
        assert "(ruling missing)" in md
        assert "garbage" not in md

    def test_broken_pass_never_costs_the_tick(
        self, store: Any, monkeypatch: Any
    ) -> None:
        def _boom(*a: Any, **kw: Any) -> int:
            raise RuntimeError("rulings pass down")

        monkeypatch.setattr("precis.quest.rulings.mint_measurement_rulings", _boom)
        qid = _mk_quest(store, "A striving")
        out = run_quest_tick(
            store,
            qid,
            dispatch_fn=lambda _req: SimpleNamespace(
                data={"logbook": []}, text="", error=None, cost_usd=0.01, paused=False
            ),
        )
        assert out.status == "succeeded"
        assert out.rulings_minted == 0


class TestRenderAndTick:
    def test_render_shows_measured_line(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        hyp = seed_ref(store, title="a hypothesis", kind="finding")
        st = _mk_structure(store, barrier=1.895, trusted=True)
        _preregister(store, qid, hyp, st)
        assert mint_measurement_rulings(store, qid) == 1
        (fid,) = _ruling_ids(store, hyp)

        md = read_dialectic(store, qid)
        assert f"measured: [fi{fid}]" in md
        assert "1.895 eV" in md

    def test_tick_prepass_mints_and_prompts_the_ruling(self, store: Any) -> None:
        """The pre-pass runs before prompt assembly, so the SAME tick's
        prompt shows the fresh ``measured:`` line and the outcome carries
        the count."""
        qid = _mk_quest(store, "A striving")
        hyp = seed_ref(store, kind="finding")
        st = _mk_structure(store, barrier=0.42, trusted=True)
        _preregister(store, qid, hyp, st)

        reqs: list[Any] = []

        def disp(req: Any) -> Any:
            reqs.append(req)
            return SimpleNamespace(
                data={"logbook": []}, text="", error=None, cost_usd=0.01, paused=False
            )

        out = run_quest_tick(store, qid, dispatch_fn=disp)
        assert out.status == "succeeded"
        assert out.rulings_minted == 1
        assert len(reqs) == 1
        (fid,) = _ruling_ids(store, hyp)
        assert f"measured: [fi{fid}]" in reqs[0].prompt
        # Idempotent across ticks: the next tick re-scans and mints nothing.
        out2 = run_quest_tick(store, qid, dispatch_fn=disp)
        assert out2.rulings_minted == 0
