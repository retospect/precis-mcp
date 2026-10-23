"""Gardener proposal yield (gr504843) — budget sharing, rejected-key
suppression, dangling-edge narrowing, memory body dup, scan cursors.

DB-backed (``store`` fixture), same harness as ``test_garden.py``: the
MockEmbedder puts identical text at cosine distance ~0 and unrelated text
near ~1.0. The LLM is never invoked — ``propose=`` takes a deterministic
verdict.
"""

from __future__ import annotations

from typing import Any

import pytest
from psycopg.errors import QueryCanceled

from precis.embedder import MockEmbedder
from precis.graph.garden import candidates as cand
from precis.graph.garden import proposer, runner
from precis.graph.garden.candidates import DetectorRun, GardenCandidate
from precis.graph.garden.proposals import (
    emit_proposal,
    key_block,
    proposal_key_pending,
    resolve_proposal,
)
from precis.graph.garden.proposer import Verdict
from precis.graph.garden.runner import run_garden_tick
from precis.graph.garden.service import saved_cursors
from precis.store import ChunkInsert, Store
from precis.store.revision_context import revision_context
from precis.store.types import Tag

_EMB = MockEmbedder(dim=1024)
_EMBEDDER = "bge-m3"  # the migration-seeded default embedder


def _ref(
    store: Store, kind: str, title: str, meta: dict[str, Any] | None = None
) -> int:
    return int(
        store.insert_ref(kind=kind, slug=None, title=title, meta=dict(meta or {})).id
    )


def _accept(_store: Store, _cand: GardenCandidate) -> Verdict:
    return Verdict(accept=True, rationale="seeded accept", model="test-model")


# ── 1: the proposal budget is shared round-robin across detectors ──────────


class TestBudgetRoundRobin:
    def _cands(self, store: Store, detector: str, n: int) -> list[GardenCandidate]:
        return [
            GardenCandidate(
                detector=detector,
                action="prune",
                target_kind="ref",
                target_id=_ref(store, "concept", f"{detector} {i}"),
                title=f"{detector} {i}",
            )
            for i in range(n)
        ]

    def test_first_detector_cannot_take_the_whole_budget(
        self, store: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """dangling_edge sorts first and has 20 candidates; with a budget
        of 6 over three detectors each gets two."""
        cands = (
            self._cands(store, "cycle_edge", 20)
            + self._cands(store, "dangling_edge", 20)
            + self._cands(store, "dup_pair", 5)
        )
        monkeypatch.setattr(
            runner, "run_detectors", lambda *_a, **_kw: DetectorRun(candidates=cands)
        )
        out = run_garden_tick(store, max_proposals=6, propose=_accept)
        assert out.proposals == 6
        assert out.proposals_by_detector == {
            "cycle_edge": 2,
            "dangling_edge": 2,
            "dup_pair": 2,
        }

    def test_unspent_share_goes_to_the_other_detectors(
        self, store: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A detector with one candidate leaves its share to the rest."""
        cands = self._cands(store, "dangling_edge", 20) + self._cands(
            store, "orphan", 1
        )
        monkeypatch.setattr(
            runner, "run_detectors", lambda *_a, **_kw: DetectorRun(candidates=cands)
        )
        out = run_garden_tick(store, max_proposals=6, propose=_accept)
        assert out.proposals_by_detector == {"dangling_edge": 5, "orphan": 1}

    def test_rejects_do_not_spend_the_share(
        self, store: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The share counts proposals written, not proposer calls."""
        cands = self._cands(store, "dangling_edge", 4) + self._cands(
            store, "dup_pair", 4
        )
        monkeypatch.setattr(
            runner, "run_detectors", lambda *_a, **_kw: DetectorRun(candidates=cands)
        )

        def _reject_dangling(_s: Store, c: GardenCandidate) -> Verdict:
            return Verdict(accept=c.detector != "dangling_edge", rationale="r")

        out = run_garden_tick(store, max_proposals=4, propose=_reject_dangling)
        assert out.proposals_by_detector == {"dup_pair": 4}
        assert out.rejected == 4


# ── 2: a rejected key is not re-proposed while its target is unchanged ─────


class TestRejectedKeyStaysClosed:
    def _propose_and_resolve(
        self, store: Store, target: int, verdict: str
    ) -> GardenCandidate:
        c = GardenCandidate(
            detector="stale",
            action="prune",
            target_kind="ref",
            target_id=target,
            title="stale",
        )
        rid = emit_proposal(store, c, rationale="r", model="m")
        assert rid is not None
        assert resolve_proposal(store, rid, actor="judge", verdict=verdict)
        return c

    def test_rejected_unchanged_blocks_then_edit_reopens(self, store: Store) -> None:
        target = _ref(store, "concept", "old thing")
        c = self._propose_and_resolve(store, target, "rejected")
        assert key_block(store, "ref", target, c.key) == "rejected"
        assert proposal_key_pending(store, c)
        assert emit_proposal(store, c, rationale="again") is None
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE refs SET title = 'old thing, edited' WHERE ref_id = %s",
                (target,),
            )
        assert key_block(store, "ref", target, c.key) is None
        assert emit_proposal(store, c, rationale="again") is not None

    def test_approved_key_reopens(self, store: Store) -> None:
        target = _ref(store, "concept", "old thing")
        c = self._propose_and_resolve(store, target, "approved")
        assert key_block(store, "ref", target, c.key) is None

    def test_rejected_link_target_blocks(self, store: Store) -> None:
        a = _ref(store, "memory", "live note")
        b = _ref(store, "concept", "dead thing")
        store.add_link(src_ref_id=a, dst_ref_id=b, relation="related-to")
        store.retire_ref(b)
        (first,) = cand.dangling_edge_candidates(store)
        rid = emit_proposal(store, first, rationale="r")
        assert rid is not None
        resolve_proposal(store, rid, actor="judge", verdict="rejected")
        out = run_garden_tick(store, passes=("prune",), propose=_accept)
        assert out.proposals == 0 and out.skipped_rejected == 1


# ── 3: dangling_edge skips both-retired, provenance and history edges ──────


class TestDanglingEdgeNarrowing:
    def test_only_live_to_retired_plain_edges(self, store: Store) -> None:
        live = _ref(store, "memory", "live note")
        dead = _ref(store, "concept", "dead thing")
        dead2 = _ref(store, "concept", "dead thing two")
        old_paper = int(
            store.insert_ref(kind="paper", slug="old2020v", title="old version").id
        )
        new_paper = int(
            store.insert_ref(kind="paper", slug="new2021v", title="new version").id
        )
        done = _ref(store, "todo", "done task")
        store.add_link(src_ref_id=live, dst_ref_id=dead, relation="related-to")
        store.add_link(src_ref_id=dead2, dst_ref_id=dead, relation="related-to")
        store.add_link(
            src_ref_id=new_paper, dst_ref_id=old_paper, relation="supersedes"
        )
        store.add_link(src_ref_id=live, dst_ref_id=done, relation="related-to")
        for r in (dead, dead2, old_paper, done):
            store.retire_ref(r)
        hits = cand.dangling_edge_candidates(store)
        assert [(h.evidence["src"], h.evidence["dst"]) for h in hits] == [(live, dead)]

    def test_provenance_set_names_the_relations(self) -> None:
        assert {"supersedes", "derived-from", "fixes"} <= cand.PROVENANCE_RELATIONS
        assert "related-to" not in cand.PROVENANCE_RELATIONS


# ── 4: memory dup compares bodies; the proposer reads excerpts ─────────────

_DREAM_TITLE = "I notice conv:discord/1234/5678 keeps returning to the same " + (
    "question about hydride lattice expansion and whether it"
)


def _embed(store: Store, chunk_id: int, text: str) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO chunk_embeddings (chunk_id, embedder, vector, status, "
            "attempts) VALUES (%s, %s, %s, 'ok', 1)",
            (chunk_id, _EMBEDDER, _EMB.embed_one(text)),
        )


def _memory(store: Store, title: str, body: str) -> int:
    """A memory as the write path leaves it: a title-only header card and
    a ``memory_body`` chunk, both embedded."""
    rid = _ref(store, "memory", title)
    _embed(store, store.chunks.upsert_card_combined(rid, title[:80]), title[:80])
    (row,) = store.chunks.insert_chunks(
        rid, [ChunkInsert(ord=0, text=body, meta={"chunk_kind": "memory_body"})]
    )
    _embed(store, row.id, body)
    return rid


class TestMemoryBodyDup:
    def test_same_title_different_bodies_is_not_a_pair(self, store: Store) -> None:
        _memory(store, _DREAM_TITLE, "Palladium hydride expands by 3.5% at x=0.6.")
        _memory(store, _DREAM_TITLE, "The Discord thread on Anki sync stalled.")
        assert cand.duplicate_candidates(store, kinds=("memory",)) == []

    def test_identical_bodies_are_a_pair(self, store: Store) -> None:
        a = _memory(store, "first wording", "The same consolidated claim.")
        b = _memory(store, "second wording", "The same consolidated claim.")
        (hit,) = cand.duplicate_candidates(store, kinds=("memory",))
        assert (hit.target_id, hit.other_id) == (b, a)
        assert hit.evidence["body_identical"] is True
        assert hit.evidence["vector"] == "memory_body"
        assert hit.evidence["distance"] < cand.DUP_MAX_DISTANCE

    def test_prompt_carries_bounded_body_excerpts(self, store: Store) -> None:
        long_body = "Hydride lattice note. " * 100
        a = _memory(store, _DREAM_TITLE, long_body)
        b = _memory(store, _DREAM_TITLE, "short distinct body")
        c = GardenCandidate(
            detector="dup_pair",
            action="merge",
            target_kind="ref",
            target_id=b,
            other_id=a,
        )
        excerpts = proposer.ref_excerpts(store, proposer._excerpt_ids(c))
        assert excerpts[f"memory:{b}"] == "short distinct body"
        assert len(excerpts[f"memory:{a}"]) <= proposer.EXCERPT_CHARS + 1
        assert excerpts[f"memory:{a}"].startswith("Hydride lattice note.")
        prompt = proposer._prompt_for(c, excerpts)
        assert "short distinct body" in prompt and '"excerpts"' in prompt


# ── 5: wide scans run per kind past a saved cursor ─────────────────────────


class TestScanCursor:
    def test_each_kind_gets_its_own_window(self, store: Store) -> None:
        """Many old memories no longer starve the newer taxa: every kind
        is scanned from its own cursor."""
        for i in range(5):
            _ref(store, "memory", f"old memory {i}")
        a = _ref(store, "taxon", "band gap", meta={"name": "band gap"})
        b = _ref(store, "taxon", "band gap", meta={"name": "band gap"})
        nxt: dict[str, int] = {}
        hits = cand.duplicate_candidates(
            store, kinds=("memory", "taxon"), scan_cap=3, next_cursors=nxt
        )
        assert [(h.target_id, h.other_id) for h in hits] == [(b, a)]
        assert set(nxt) == {"dup_pair:memory", "dup_pair:taxon"}

    def test_cursor_walks_the_kind_and_wraps(self, store: Store) -> None:
        seeded = _ref(store, "finding", "tagged finding")
        store.add_tag(seeded, Tag.closed("DOMAIN", "materials"), set_by="system")
        plain = [_ref(store, "finding", f"untagged {i}") for i in range(4)]
        seen: list[int] = []
        cursors: dict[str, int] = {}
        for _ in range(3):
            nxt: dict[str, int] = {}
            hits = cand.untagged_candidates(
                store,
                namespaces=("DOMAIN",),
                kinds=("finding",),
                scan_cap=2,
                cursors=cursors,
                next_cursors=nxt,
            )
            seen += [h.target_id for h in hits]
            cursors = nxt
        # Window 1: seeded + plain[0]; 2: plain[1..2]; 3: plain[3] (short,
        # so the cursor wraps to 0).
        assert seen == plain
        assert cursors == {"untagged:finding": 0}

    def test_skipped_detector_keeps_its_cursor(
        self, store: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(*_a: Any, **kw: Any) -> list[GardenCandidate]:
            kw["next_cursors"]["untagged:finding"] = 999
            raise QueryCanceled("canceling statement due to statement timeout")

        monkeypatch.setattr(cand, "untagged_candidates", _boom)
        run = cand.run_detectors(
            store, passes=("tag-sweep",), cursors={"untagged:finding": 7}
        )
        assert run.skipped == ["untagged"] and run.cursors == {}

    def test_dispatch_saves_and_resumes_cursors(
        self, store: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """meta.garden.cursors on the newest run is where the next run
        starts; keys a run did not touch are carried forward."""
        from precis.workers.job_types.graph_garden import SPEC, _dispatch

        with store.pool.connection() as conn:
            conn.execute(
                "INSERT INTO service_config (host, service, prio) "
                "VALUES ('test-host', 'graph_garden', 1) "
                "ON CONFLICT (host, service) DO UPDATE SET prio = 1"
            )
        _ref(
            store,
            "job",
            "earlier garden run",
            meta={
                "job_type": "graph_garden",
                "garden": {"proposals": 1, "cursors": {"dup_pair:memory": 41}},
            },
        )
        assert saved_cursors(store) == {"dup_pair:memory": 41}
        seen: dict[str, Any] = {}

        def _tick(_store: Store, **kw: Any) -> runner.GardenOutcome:
            seen.update(kw["cursors"])
            return runner.GardenOutcome(cursors={"dup_pair:taxon": 5})

        monkeypatch.setattr(runner, "run_garden_tick", _tick)
        ctx = _Ctx(store, {"pass": "merge", "kinds": ["taxon"]})
        _dispatch(ctx, SPEC)
        assert seen == {"dup_pair:memory": 41}
        assert ctx.meta["garden"]["cursors"] == {
            "dup_pair:memory": 41,
            "dup_pair:taxon": 5,
        }


# ── 6: redundant_link probes neighbours in batches, closest first ──────────


def _machine_link(store: Store, a: int, b: int) -> int:
    with revision_context(trust_origin="generated", trust_actor_type="agent"):
        return int(store.add_link(src_ref_id=a, dst_ref_id=b, relation="related-to").id)


class TestRedundantLinkBatch:
    def _pairs(self, store: Store, n: int) -> list[int]:
        links = []
        for i in range(n):
            x = _ref(store, "concept", f"x {i}")
            y = _ref(store, "concept", f"y {i}")
            for r in (x, y):
                _embed(
                    store,
                    store.chunks.upsert_card_combined(r, f"shared {i}"),
                    f"shared card {i}",
                )
            links.append(_machine_link(store, x, y))
        return links

    def test_one_probe_statement_for_the_batch(
        self, store: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        links = self._pairs(store, 3)
        calls: list[set[int]] = []
        real = cand._top_k_card_neighbours

        def _spy(s: Store, ids: set[int], *a: Any, **kw: Any) -> Any:
            calls.append(set(ids))
            return real(s, ids, *a, **kw)

        monkeypatch.setattr(cand, "_top_k_card_neighbours", _spy)
        hits = cand.redundant_link_candidates(store)
        assert sorted(h.target_id for h in hits) == sorted(links)
        assert len(calls) == 1 and len(calls[0]) == 6

    def test_stops_probing_at_limit_and_budget(
        self, store: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._pairs(store, 3)
        monkeypatch.setattr(cand, "PRUNE_PROBE_BATCH", 1)
        calls: list[int] = []
        real = cand._top_k_card_neighbours

        def _spy(s: Store, ids: set[int], *a: Any, **kw: Any) -> Any:
            calls.append(len(ids))
            return real(s, ids, *a, **kw)

        monkeypatch.setattr(cand, "_top_k_card_neighbours", _spy)
        assert len(cand.redundant_link_candidates(store, limit=1)) == 1
        assert calls == [2]
        calls.clear()
        assert cand.redundant_link_candidates(store, probe_budget_s=-1) == []
        assert calls == []


# ── 7: paper-identity — one work ingested as two papers ────────────────────

_TITLE = "Hydrogen Absorption in Palladium Nanocubes"


def _paper(
    store: Store,
    slug: str,
    title: str = _TITLE,
    *,
    year: int | None = 2020,
    author: str | None = "Smith",
    doi: str | None = None,
) -> int:
    rid = int(
        store.insert_ref(
            kind="paper",
            slug=slug,
            title=title,
            year=year,
            authors=[{"family": author, "given": "A."}] if author else None,
        ).id
    )
    if doi:
        with store.pool.connection() as conn:
            conn.execute(
                "INSERT INTO ref_identifiers (id_kind, id_value, ref_id) "
                "VALUES ('doi', %s, %s)",
                (doi, rid),
            )
    return rid


class TestPaperIdentity:
    def test_same_normalized_title_is_a_merge_candidate(self, store: Store) -> None:
        """A DOI is unique per ref (ref_identifiers PK), so a duplicate
        carries it on one side at most: title + year + author pair them."""
        a = _paper(store, "smith2020a", doi="10.1/x")
        b = _paper(store, "smith2020b", _TITLE.upper() + ".", year=2021)
        (hit,) = cand.paper_identity_candidates(store)
        assert (hit.action, hit.target_id, hit.other_id) == ("merge", b, a)
        assert hit.evidence["doi"] == "missing"
        assert hit.evidence["years"] == [2020, 2021]
        assert hit.evidence["first_authors"] == ["smith", "smith"]
        assert {k: v for k, v in hit.payload.items() if k != "shas"} == {
            "loser": b,
            "winner": a,
            "kind": "paper",
        }
        assert set(hit.payload["shas"]) == {str(a), str(b)}

    def test_conflicting_evidence_gates_the_pair(self, store: Store) -> None:
        _paper(store, "p1", doi="10.1/x")
        _paper(store, "p2", doi="10.1/y")  # distinct DOIs
        _paper(store, "p3", "Another Long Enough Paper Title", year=2001)
        _paper(store, "p4", "Another Long Enough Paper Title", year=2015)
        _paper(store, "p5", "Third Long Enough Paper Title", author="Jones")
        _paper(store, "p6", "Third Long Enough Paper Title", author="Wu")
        _paper(store, "p7", "Editorial")
        _paper(store, "p8", "Editorial")
        assert cand.paper_identity_candidates(store) == []

    def test_cursor_walks_groups(self, store: Store) -> None:
        a1 = _paper(store, "a1", "First Long Enough Paper Title")
        a2 = _paper(store, "a2", "First Long Enough Paper Title")
        b1 = _paper(store, "b1", "Second Long Enough Paper Title")
        b2 = _paper(store, "b2", "Second Long Enough Paper Title")
        nxt: dict[str, int] = {}
        first = cand.paper_identity_candidates(store, limit=1, next_cursors=nxt)
        assert [(h.target_id, h.other_id) for h in first] == [(a2, a1)]
        assert nxt == {"paper_identity": a1}
        second = cand.paper_identity_candidates(store, limit=1, cursors=nxt)
        assert [(h.target_id, h.other_id) for h in second] == [(b2, b1)]

    def test_paper_identity_pass_runs_the_detector(self, store: Store) -> None:
        _paper(store, "smith2020a")
        _paper(store, "smith2020b")
        out = run_garden_tick(store, passes=("paper-identity",), propose=_accept)
        assert out.proposals_by_detector == {"paper_identity": 1}


class _Ctx:
    """A minimal DispatchContext stand-in (see test_garden.TestDispatchGlue)."""

    def __init__(self, store: Any, params: dict[str, Any]) -> None:
        self.store = store
        self.meta: dict[str, Any] = {"params": params}
        self.events: list[tuple[str, str]] = []
        self.failure: str | None = None

    def append_chunk(self, kind: str, text: str) -> None:
        self.events.append((kind, text))

    def set_meta(self, **fields: Any) -> None:
        self.meta.update(fields)

    def record_failure(self, reason: str, **_kw: Any) -> None:
        self.failure = reason

    def defer(self, _delay: Any, _reason: str) -> bool:
        return True
