"""Graph gardener proposal quality — the proposer's completion cap
(gr504896) and its remembered rejections (gr506046).

DB-backed (``store`` fixture) like ``test_garden_yield.py``. The LLM is
never called: the router's LOCAL provider is replaced by a recorder, or
``propose=`` takes a deterministic verdict. The MockEmbedder puts
identical text at cosine distance ~0.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.embedder import MockEmbedder
from precis.graph.garden import candidates as cand
from precis.graph.garden import proposer, runner
from precis.graph.garden.candidates import DetectorRun, GardenCandidate
from precis.graph.garden.proposals import (
    emit_proposal,
    key_block,
    pending_proposals,
)
from precis.graph.garden.proposer import Verdict
from precis.graph.garden.runner import run_garden_tick
from precis.store import Store
from precis.utils.llm import router
from precis.utils.llm.router import (
    FailoverProvider,
    LlmRequest,
    LlmResult,
    Tier,
    Transport,
)

#: The prod ``llm.op.graph_garden:propose`` chain shape (2026-10-23).
_PROD_CHAIN = [
    {
        "transport": "local",
        "model": "openai/gpt-oss-120b",
        "placement": "local",
        "max_tokens": 4000,
        "timeout_s": 300,
        "reasoning_effort": "medium",
    }
]


_EMB = MockEmbedder(dim=1024)
_EMBEDDER = "bge-m3"  # the migration-seeded default embedder

#: The legacy-import placeholder prod's component taxa carry (460093
#: Pedal, 460098 Wheel, 460097 Seating): every card is this sentence
#: with a different name in front, so the cards embed close together.
_TEMPLATE = (
    "a component category, carried over from the legacy component_categories "
    "registry, which recorded no fuller definition."
)


def _ref(
    store: Store, kind: str, title: str, meta: dict[str, Any] | None = None
) -> int:
    return int(
        store.insert_ref(kind=kind, slug=None, title=title, meta=dict(meta or {})).id
    )


def _card(store: Store, ref_id: int, text: str, embed_text: str | None = None) -> None:
    cid = store.chunks.upsert_card_combined(ref_id, text)
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO chunk_embeddings (chunk_id, embedder, vector, status, "
            "attempts) VALUES (%s, %s, %s, 'ok', 1)",
            (cid, _EMBEDDER, _EMB.embed_one(embed_text or text)),
        )


def _component_siblings(
    store: Store, names: tuple[str, ...] = ("Pedal", "Wheel", "Seating")
) -> list[int]:
    """Pedal, Wheel, Seating under one parent, placeholder definitions,
    cards embedded on the shared placeholder (the boilerplate dominates
    the prod vectors: 0.17-0.22 apart)."""
    parent = _ref(store, "taxon", "subject", meta={"name": "subject"})
    kids = []
    for name in names:
        definition = f"{name}: {_TEMPLATE}"
        rid = _ref(store, "taxon", name, meta={"name": name, "definition": definition})
        _card(store, rid, f"{name}\n{definition}", embed_text=_TEMPLATE)
        store.add_link(src_ref_id=rid, dst_ref_id=parent, relation="specialises")
        kids.append(rid)
    return kids


class _RunFn:
    def __init__(self, fn: Any) -> None:
        self._fn = fn

    def run(self, req: LlmRequest, *, model: str) -> LlmResult:
        return self._fn(req, model=model)


def _result(text: str, stop: str | None = "stop") -> LlmResult:
    return LlmResult(
        text=text,
        cost_usd=None,
        turns_used=None,
        model="m",
        tier=Tier.MEDIUM,
        stop_reason=stop,
    )


def _through_chain(
    monkeypatch: pytest.MonkeyPatch, reply: LlmResult
) -> list[int | None]:
    """Route the proposer's request through a FailoverProvider built from
    the prod chain row; return the ``max_tokens`` each rung call saw."""
    seen: list[int | None] = []

    def _local(req: LlmRequest, *, model: str) -> LlmResult:
        seen.append(req.max_tokens)
        return reply

    rungs, bad = router.parse_chain_rungs(_PROD_CHAIN)
    assert bad is None
    monkeypatch.setitem(router._PROVIDERS, Transport.LOCAL, _RunFn(_local))
    monkeypatch.setattr(
        router, "route", lambda req: FailoverProvider(rungs).run(req, model="m")
    )
    return seen


def _cand(store: Store) -> GardenCandidate:
    a = _ref(store, "concept", "band gap")
    b = _ref(store, "concept", "optical band gap")
    return GardenCandidate(
        detector="dup_pair", action="merge", target_kind="ref", target_id=b, other_id=a
    )


class TestProposerCap:
    def test_chain_cap_reaches_the_rung(
        self, store: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The proposer pins no cap, so the op chain's 4000 is what the
        rung sends — not the old call-site 400 that beat it."""
        seen = _through_chain(
            monkeypatch, _result('{"accept": false, "rationale": "different terms"}')
        )
        verdict = proposer.propose_llm(store, _cand(store))
        assert seen == [4000]
        assert verdict.accept is False and verdict.error is None

    def test_empty_length_cut_is_counted_as_truncated(
        self, store: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An empty reply stopped on the cap is an error AND marked
        truncated; a reply that is not JSON for another reason is not."""
        _through_chain(monkeypatch, _result("", stop="length"))
        cut = proposer.propose_llm(store, _cand(store))
        assert cut.error is not None and cut.truncated is True

        _through_chain(monkeypatch, _result("no json here"))
        unparsed = proposer.propose_llm(store, _cand(store))
        assert unparsed.error is not None and unparsed.truncated is False

    def test_tick_counts_truncated_apart(
        self, store: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cands = [
            GardenCandidate(
                detector="stale",
                action="prune",
                target_kind="ref",
                target_id=_ref(store, "concept", f"stale {i}"),
            )
            for i in range(3)
        ]
        monkeypatch.setattr(
            runner, "run_detectors", lambda *_a, **_kw: DetectorRun(candidates=cands)
        )
        verdicts = iter(
            [
                Verdict(accept=False, error="empty completion", truncated=True),
                Verdict(accept=False, error="proposer returned no JSON object"),
                Verdict(accept=False, rationale="no"),
            ]
        )
        out = run_garden_tick(store, propose=lambda _s, _c: next(verdicts))
        assert (out.llm_errors, out.llm_truncated, out.rejected) == (2, 1, 1)


# ── gr506046: a proposer rejection is remembered ───────────────────────────


class _Rejecter:
    """Rejects every candidate and records which keys it was asked."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    def __call__(self, _store: Store, cand: GardenCandidate) -> Verdict:
        self.asked.append(cand.key)
        return Verdict(accept=False, rationale="siblings name different parts")


def _merge_tick(store: Store, propose: Any) -> runner.GardenOutcome:
    return run_garden_tick(store, passes=("merge",), kinds=("taxon",), propose=propose)


#: Siblings the short-sibling rule does not decide (they share "wheel"),
#: so the model is asked.
_WHEELS = ("Front wheel", "Rear wheel", "Spare wheel")


class TestRejectionRemembered:
    def test_second_run_does_not_re_ask(self, store: Store) -> None:
        """Before: every run re-asked the same three sibling pairs (prod:
        16-20 rejects a run, one pair asked 16 times). After: asked once,
        then skipped while target and prompt are unchanged."""
        _component_siblings(store, _WHEELS)
        judge = _Rejecter()
        first = _merge_tick(store, judge)
        assert (first.candidates, first.rejected, len(judge.asked)) == (3, 3, 3)
        second = _merge_tick(store, judge)
        assert (second.rejected, second.skipped_rejected) == (0, 3)
        assert len(judge.asked) == 3
        assert pending_proposals(store) == []

    def test_a_new_prompt_version_re_asks(
        self, store: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _component_siblings(store, _WHEELS)
        judge = _Rejecter()
        _merge_tick(store, judge)
        monkeypatch.setattr(runner, "PROMPT_VERSION", "garden-propose.vtest")
        again = _merge_tick(store, judge)
        assert (again.rejected, again.skipped_rejected) == (3, 0)

    def test_editing_the_target_re_asks_its_pairs(self, store: Store) -> None:
        kids = _component_siblings(store, _WHEELS)
        judge = _Rejecter()
        _merge_tick(store, judge)
        edited = max(kids)  # the higher id is the target of its pairs
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE refs SET title = 'Saddle wheel' WHERE ref_id = %s", (edited,)
            )
        judge.asked.clear()
        again = _merge_tick(store, judge)
        assert judge.asked and all(f":ref:{edited}:" in k for k in judge.asked)
        assert again.skipped_rejected == 3 - len(judge.asked)

    def test_a_proposer_rejection_never_blocks_the_emit(self, store: Store) -> None:
        """The runner decides whether to re-ask; once it has, the emit
        path is not held by the proposer's own earlier rejection."""
        _component_siblings(store, _WHEELS)
        _merge_tick(store, _Rejecter())
        c = runner.run_detectors(store, passes=("merge",), kinds=("taxon",)).candidates[
            0
        ]
        tk, tid, key = c.target_kind, c.target_id, c.key
        assert (
            key_block(store, tk, tid, key, prompt_version=proposer.PROMPT_VERSION)
            == "rejected"
        )
        assert key_block(store, tk, tid, key) is None
        assert emit_proposal(store, c, rationale="accepted now") is not None
        assert [p.key for p in pending_proposals(store)] == [key]


# ── gr504843: dup_pair evidence names what it measured ──────────────────────


class TestDupEvidence:
    def test_placeholder_siblings_read_as_placeholder(self, store: Store) -> None:
        """Review 8288 (Pedal/Wheel) cited "lexical overlap 0.833": that
        number was name + placeholder definition. Now the names score 0,
        the definitions are flagged as one template, and the shared
        parent is named."""
        kids = _component_siblings(store)
        hits = cand.duplicate_candidates(store, kinds=("taxon",))
        assert len(hits) == 3
        for hit in hits:
            ev = hit.evidence
            assert "lexical_overlap" not in ev
            assert ev["name_overlap"] == 0.0
            assert ev["definition_overlap"] >= 0.8
            assert ev["definition_template"] is True
            assert len(ev["siblings_under"]) == 1
            assert {hit.target_id, hit.other_id} <= set(kids)

    def test_a_real_duplicate_keeps_its_name_evidence(self, store: Store) -> None:
        """Two "Maximum service temperature" taxa (prod 460122/460138,
        merged): the names agree, the definitions are not a template."""
        a = _ref(
            store,
            "taxon",
            "Maximum service temperature",
            meta={"definition": "Upper continuous-use temperature. Kelvin."},
        )
        b = _ref(
            store,
            "taxon",
            "Maximum service temperature",
            meta={"definition": "Upper use temperature before properties degrade."},
        )
        (hit,) = cand.duplicate_candidates(store, kinds=("taxon",))
        assert (hit.target_id, hit.other_id) == (b, a)
        assert hit.evidence["name_overlap"] == 1.0
        assert "definition_template" not in hit.evidence
        assert "siblings_under" not in hit.evidence

    def test_prompt_carries_the_rules_and_a_new_version(self, store: Store) -> None:
        _component_siblings(store)
        hit = cand.duplicate_candidates(store, kinds=("taxon",))[0]
        prompt = proposer._prompt_for(hit)
        for needle in ("name_overlap", "definition_template", "siblings_under"):
            assert needle in prompt
        assert "pedal vs wheel" in prompt
        assert proposer.PROMPT_VERSION != "garden-propose.v1"
