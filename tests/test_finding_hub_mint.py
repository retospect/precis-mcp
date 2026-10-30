"""``put(kind='finding', supporters=...)``'s semantic-dedup cascade +
``get(kind='finding', view='similar')`` — docs/backlog/read-for-question-
loop.md slice 2.

Two layers, mirroring ``tests/test_taproot_directed.py``'s split:

* The cascade branches (``attach``/``needs_review``/``new_contradicts``)
  are exercised directly against :func:`precis.handlers._finding_hub_mint.
  put_hub`, with ``block_fn``/``judge_fn`` injected — deterministic, no
  LLM/embedder dispatch.
* The handler-level wiring (``FindingHandler.put``'s ``dedup=`` kwarg,
  the embedder reachable via ``self.hub.embedder``, ``view='similar'``)
  goes through ``FindingHandler`` itself.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, cast

import pytest

from precis.dispatch import Hub
from precis.embedder import EmbedderUnavailable
from precis.errors import BadInput, Unsupported, Upstream
from precis.handlers import _finding_hub_mint
from precis.handlers.finding import FindingHandler
from precis.store.types import ChunkInsert, Tag
from precis.taproot.canon import (
    CanonicalClaim,
    MergeCandidate,
    Verdict,
    Verdict3,
    nearest_hubs,
)
from precis.taproot.hub import mint_hub

if TYPE_CHECKING:
    from precis.store.store import Store


def _search(pattern: str, text: str) -> re.Match[str]:
    m = re.search(pattern, text)
    assert m is not None, f"pattern {pattern!r} not found in {text!r}"
    return m


def _make_handler(store, *, embedder: Any = None) -> FindingHandler:
    return FindingHandler(hub=Hub(store=store, embedder=embedder))


def _seed_paper(store, *, cite_key: str = "miller23a") -> int:
    ref = store.insert_ref(
        kind="paper", slug=cite_key, title=f"Test paper {cite_key}", meta={}
    )
    store.chunks.insert_chunks(
        ref.id, [ChunkInsert(ord=0, text=f"Body chunk of {cite_key}.", meta={})]
    )
    return ref.id


def _verdict(v: Verdict3, c: float, rationale: str = "test") -> Verdict:
    return Verdict(verdict=v, confidence=c, rationale=rationale)


def _block_hit(hub_ref_id: int, claim: str, distance: float = 0.02):
    def _block(claim_obj: Any, store: Any, embedder: Any, **kw: Any):
        return [MergeCandidate(hub_ref_id=hub_ref_id, claim=claim, distance=distance)]

    return _block


def _block_none(claim_obj: Any, store: Any, embedder: Any, **kw: Any):
    return []


def _never_called(*_a: Any, **_kw: Any) -> Any:
    raise AssertionError("should not be called")


# ── handler-level wiring: dedup=, embedder reachability, pub_id removal ──


class TestPutHubDedupWiring:
    def test_no_embedder_dedup_true_raises_unsupported(self, store) -> None:
        _seed_paper(store, cite_key="miller23a")
        h = _make_handler(store, embedder=None)
        with pytest.raises(Unsupported, match="embedder"):
            h.put(
                title="a fresh claim nobody has minted",
                supporters=[{"paper": "miller23a"}],
            )

    def test_embedder_unavailable_is_upstream_not_internal(self, store) -> None:
        # gr459918: block()'s embed raising EmbedderUnavailable escaped as Internal.
        from precis.embedder import EmbedderUnavailable
        from precis.errors import Upstream
        from precis.handlers._finding_hub_mint import put_hub

        _seed_paper(store, cite_key="miller23a")

        def _boom(claim_obj: Any, store: Any, embedder: Any, **kw: Any):
            raise EmbedderUnavailable("busy")

        with pytest.raises(Upstream, match="embedder"):
            put_hub(
                store,
                sentence="a fresh claim nobody has minted",
                scope={},
                supporters=[{"paper": "miller23a"}],
                embedder=object(),
                block_fn=_boom,
            )

    def test_dedup_false_mints_unconditionally_and_says_so(self, store) -> None:
        _seed_paper(store, cite_key="miller23a")
        h = _make_handler(store, embedder=None)
        resp = h.put(
            title="a fresh claim nobody has minted",
            supporters=[{"paper": "miller23a"}],
            dedup=False,
        )
        assert "dedup skipped" in resp.body
        assert "claim hub fi" in resp.body

    def test_put_response_never_carries_pub_id(self, store) -> None:
        _seed_paper(store, cite_key="miller23a")
        h = _make_handler(store, embedder=None)
        resp = h.put(
            title="amine loading raises CO2 capacity",
            supporters=[{"paper": "miller23a"}],
            dedup=False,
        )
        assert "pub_id=" not in resp.body
        assert "pub_id" not in resp.body


class TestPutHubMintBlockingLint:
    """gr530995: the two sentence codes that hard-block at mint-preflight
    (no-epistemic-mode, over-long) surface on the put response — advisory,
    the hub still mints, but the author learns at write time that approve
    will refuse the sentence as written."""

    def test_modeless_claim_warns_it_will_fail_preflight(self, store) -> None:
        _seed_paper(store, cite_key="miller23a")
        h = _make_handler(store, embedder=None)
        resp = h.put(
            title="a fresh claim nobody has minted",
            supporters=[{"paper": "miller23a"}],
            dedup=False,
        )
        assert "mint-preflight will REFUSE these" in resp.body
        assert "no-epistemic-mode" in resp.body

    def test_over_long_claim_warns_it_will_fail_preflight(self, store) -> None:
        _seed_paper(store, cite_key="miller23a")
        h = _make_handler(store, embedder=None)
        sentence = "DFT finds a fresh claim " + "nobody has minted anywhere " * 13 + "."
        assert len(sentence) > 250
        resp = h.put(
            title=sentence,
            supporters=[{"paper": "miller23a"}],
            dedup=False,
        )
        assert "mint-preflight will REFUSE these" in resp.body
        assert "over-long" in resp.body

    def test_clean_claim_carries_no_preflight_blocker_note(self, store) -> None:
        _seed_paper(store, cite_key="miller23a")
        h = _make_handler(store, embedder=None)
        resp = h.put(
            title="DFT finds a fresh claim nobody has minted.",
            supporters=[{"paper": "miller23a"}],
            dedup=False,
        )
        assert "mint-preflight will REFUSE" not in resp.body


class TestMintBlockingSentenceCodesSync:
    """gr530995 fix round 1: authoring's copy of the gates blocking set
    must not drift. Literal equality with the full set is not the
    contract — the put door deliberately surfaces only the two codes an
    author can fix at write time — so the test pins the exact pair and
    asserts it stays inside the gates blocking set: if gates drops one,
    the put response would over-promise a refusal preflight no longer
    gives, and the subset assertion fails."""

    def test_authoring_codes_match_the_gates_blocking_set(self) -> None:
        from precis.nanopub.gates import _BLOCKING_LINT_CODES
        from precis.taproot import authoring

        assert frozenset({"no-epistemic-mode", "over-long"}) == (
            authoring._MINT_BLOCKING_SENTENCE_CODES
        )
        assert authoring._MINT_BLOCKING_SENTENCE_CODES <= _BLOCKING_LINT_CODES


# ── dedup-gate contention: capacity is not absence (gripe #450123) ───────


class TestPutHubDedupContention:
    """A busy embedder must fail the mint CLOSED with a capacity-not-absence
    message that steers off ``dedup=False``, not a raw read-path error that
    reads as "the claim doesn't exist" (how duplicate hubs are born).

    DB-free by construction: the injected ``block_fn`` raises the embedder
    failure before ``put_hub`` touches the store at all, so these exercise
    the reclassification without a live postgres. ``store`` is a sentinel
    that must never be dereferenced — dereferencing it would ``AttributeError``
    and fail the test, which is exactly the "gate fails closed before any
    write" invariant we want.
    """

    def _put(self, block_fn):
        return _finding_hub_mint.put_hub(
            cast("Store", object()),  # sentinel — never touched before block_fn raises
            sentence="a fresh claim under embedder contention",
            scope={},
            supporters=[{"paper": "miller23a"}],
            embedder=object(),
            block_fn=block_fn,
        )

    def test_embedder_unavailable_becomes_capacity_upstream_with_retry(
        self,
    ) -> None:
        from precis.embedder import EmbedderUnavailable
        from precis.errors import Upstream

        def _busy(_claim, _store, _embedder):
            raise EmbedderUnavailable(
                "embedder at capacity (429 after queueing)",
                retry_after_s=2,
                last_status=429,
            )

        with pytest.raises(Upstream) as ei:
            self._put(_busy)
        msg = f"{ei.value} {ei.value.next}"
        assert "capacity" in msg
        assert "~2 s" in msg
        # Must steer OFF the dedup=False escape hatch and NOT read as absence.
        assert "dedup=False" in msg
        assert "duplicate" in msg

    def test_embedder_unavailable_without_retry_hint_still_capacity_not_absence(
        self,
    ) -> None:
        from precis.embedder import EmbedderUnavailable
        from precis.errors import Upstream

        def _down(_claim, _store, _embedder):
            raise EmbedderUnavailable("all embedder endpoints failed")

        with pytest.raises(Upstream) as ei:
            self._put(_down)
        msg = f"{ei.value} {ei.value.next}"
        assert "dedup=False" in msg
        assert "duplicate" in msg

    def test_bulkhead_shed_upstream_is_reworded_for_the_write_path(
        self,
    ) -> None:
        from precis.errors import Upstream

        def _shed(_claim, _store, _embedder):
            # What BoundedConcurrencyEmbedder raises on a local shed — its
            # "degraded to lexical-only" wording is untrue for a mint.
            raise Upstream(
                "embedder busy — 4 embeds already in flight in this process, "
                "so this one was shed rather than queued; semantic search "
                "degraded to lexical-only for this call",
                next="retry shortly",
            )

        with pytest.raises(Upstream) as ei:
            self._put(_shed)
        msg = f"{ei.value} {ei.value.next}"
        assert "lexical-only" not in msg
        assert "dedup=False" in msg


# ── cascade branches, block_fn/judge_fn injected ─────────────────────────


class TestPutHubCascadeAttach:
    def test_same_verdict_attaches_no_new_hub(self, store) -> None:
        existing_sentence = "Pd/C catalyzes Suzuki coupling."
        existing_hub = mint_hub(
            store, CanonicalClaim(sentence=existing_sentence, scope={})
        )
        paper = _seed_paper(store, cite_key="miller23a")
        before = store.count_refs(kind="finding")

        resp = _finding_hub_mint.put_hub(
            store,
            sentence="Pd on carbon catalyzes the Suzuki reaction.",
            scope={},
            supporters=[{"paper": "miller23a"}],
            embedder=object(),
            block_fn=_block_hit(existing_hub, existing_sentence),
            judge_fn=lambda a, b: _verdict("same", 0.95),
        )

        assert "converged onto" in resp.body
        assert f"fi{existing_hub}" in resp.body
        assert existing_sentence in resp.body
        assert store.count_refs(kind="finding") == before

        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT relation FROM links WHERE src_ref_id = %s AND dst_ref_id = %s",
                (paper, existing_hub),
            ).fetchone()
        assert row is not None

    def test_multi_article_chunk_supporter_adds_advisory(self, store) -> None:
        existing_sentence = "Pd/C catalyzes Suzuki coupling."
        existing_hub = mint_hub(
            store, CanonicalClaim(sentence=existing_sentence, scope={})
        )
        paper = _seed_paper(store, cite_key="multipaper")
        store.add_tag(paper, Tag.open("multi-article"))
        with store.pool.connection() as conn:
            chunk_id = conn.execute(
                "SELECT chunk_id FROM chunks WHERE ref_id = %s", (paper,)
            ).fetchone()[0]

        resp = _finding_hub_mint.put_hub(
            store,
            sentence="Pd on carbon catalyzes the Suzuki reaction.",
            scope={},
            supporters=[{"paper": f"pc{chunk_id}"}],
            embedder=object(),
            block_fn=_block_hit(existing_hub, existing_sentence),
            judge_fn=lambda a, b: _verdict("same", 0.95),
        )

        assert "converged onto" in resp.body
        assert "advisory" in resp.body and "multi-article" in resp.body
        assert f"pc{chunk_id}" in resp.body

    def test_attach_bad_supporter_raises_and_writes_nothing(self, store) -> None:
        existing_sentence = "Pd/C catalyzes Suzuki coupling."
        existing_hub = mint_hub(
            store, CanonicalClaim(sentence=existing_sentence, scope={})
        )

        with pytest.raises(BadInput):
            _finding_hub_mint.put_hub(
                store,
                sentence="Pd on carbon catalyzes the Suzuki reaction.",
                scope={},
                supporters=[{"paper": "does-not-exist"}],
                embedder=object(),
                block_fn=_block_hit(existing_hub, existing_sentence),
                judge_fn=lambda a, b: _verdict("same", 0.95),
            )

    def test_attach_bad_supporter_names_the_fix(self, store) -> None:
        """gr494831 M3: the supporter-validation refusal names its fix."""
        existing_sentence = "Pd/C catalyzes Suzuki coupling."
        existing_hub = mint_hub(
            store, CanonicalClaim(sentence=existing_sentence, scope={})
        )

        with pytest.raises(BadInput, match="nothing attached") as ei:
            _finding_hub_mint.put_hub(
                store,
                sentence="Pd on carbon catalyzes the Suzuki reaction.",
                scope={},
                supporters=[{"paper": "does-not-exist"}],
                embedder=object(),
                block_fn=_block_hit(existing_hub, existing_sentence),
                judge_fn=lambda a, b: _verdict("same", 0.95),
            )
        assert "fix every listed supporter" in str(ei.value.next)

    def test_attach_loop_is_one_transaction_mid_loop_exception_rolls_back(
        self, store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A second supporter's ``attach_evidence`` raising must roll the
        FIRST supporter's already-written edge back too — the whole loop is
        one ``store.tx()`` (mirrors ``seed_claim_hub``'s atomicity
        backstop), not one transaction per supporter."""
        existing_sentence = "Pd/C catalyzes Suzuki coupling."
        existing_hub = mint_hub(
            store, CanonicalClaim(sentence=existing_sentence, scope={})
        )
        paper1 = _seed_paper(store, cite_key="miller23a")
        paper2 = _seed_paper(store, cite_key="jones24b")

        real_attach_evidence = _finding_hub_mint.attach_evidence

        def _flaky_attach_evidence(store: Any, *, paper_ref_id: int, **kw: Any) -> None:
            if paper_ref_id == paper2:
                raise RuntimeError("boom: second supporter's write fails")
            real_attach_evidence(store, paper_ref_id=paper_ref_id, **kw)

        monkeypatch.setattr(
            _finding_hub_mint, "attach_evidence", _flaky_attach_evidence
        )

        with pytest.raises(RuntimeError, match="boom"):
            _finding_hub_mint.put_hub(
                store,
                sentence="Pd on carbon catalyzes the Suzuki reaction.",
                scope={},
                supporters=[{"paper": "miller23a"}, {"paper": "jones24b"}],
                embedder=object(),
                block_fn=_block_hit(existing_hub, existing_sentence),
                judge_fn=lambda a, b: _verdict("same", 0.95),
            )

        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM links WHERE src_ref_id = %s AND dst_ref_id = %s",
                (paper1, existing_hub),
            ).fetchone()
        assert row is None, (
            "first supporter's edge must have rolled back with the second "
            "supporter's mid-loop exception"
        )

    def test_attach_runs_retraction_check_once_after_commit(
        self, store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The deferred retraction check runs exactly once, after the
        supporter-loop transaction has committed — never inside it (see
        ``attach_evidence``'s docstring: a Crossref round-trip inside an
        open transaction risks pool-exhaustion deadlock)."""
        existing_sentence = "Pd/C catalyzes Suzuki coupling."
        existing_hub = mint_hub(
            store, CanonicalClaim(sentence=existing_sentence, scope={})
        )
        paper = _seed_paper(store, cite_key="miller23a")

        calls: list[tuple[list[int], int | None]] = []

        def _fake_run_retraction_checks(
            store: Any, paper_ref_ids: list[int], *, hub_ref_id: int | None = None
        ) -> None:
            # If this ran before commit, the edge it's checking wouldn't be
            # visible on a fresh connection yet -- assert it already is.
            with store.pool.connection() as conn:
                row = conn.execute(
                    "SELECT 1 FROM links WHERE src_ref_id = %s AND dst_ref_id = %s",
                    (paper, existing_hub),
                ).fetchone()
            assert row is not None, "retraction check ran before the edge committed"
            calls.append((list(paper_ref_ids), hub_ref_id))

        monkeypatch.setattr(
            _finding_hub_mint, "run_retraction_checks", _fake_run_retraction_checks
        )

        _finding_hub_mint.put_hub(
            store,
            sentence="Pd on carbon catalyzes the Suzuki reaction.",
            scope={},
            supporters=[{"paper": "miller23a"}],
            embedder=object(),
            block_fn=_block_hit(existing_hub, existing_sentence),
            judge_fn=lambda a, b: _verdict("same", 0.95),
        )

        assert calls == [([paper], existing_hub)]


class TestPutHubCascadeNeedsReview:
    def test_needs_review_mints_and_files_todo_naming_both_hubs(self, store) -> None:
        existing_sentence = "Pd/C catalyzes Suzuki coupling."
        existing_hub = mint_hub(
            store, CanonicalClaim(sentence=existing_sentence, scope={})
        )
        _seed_paper(store, cite_key="miller23a")
        before = store.count_refs(kind="finding")

        resp = _finding_hub_mint.put_hub(
            store,
            sentence="Pd on carbon does something related to Suzuki coupling.",
            scope={},
            supporters=[{"paper": "miller23a"}],
            embedder=object(),
            block_fn=_block_hit(existing_hub, existing_sentence),
            judge_fn=lambda a, b: _verdict("same", 0.40),  # low-confidence same
            merge_confirm_fn=lambda a, b: _verdict("different", 0.9),  # not confirmed
        )

        new_hub = int(_search(r"claim hub fi(\d+)", resp.body).group(1))
        assert new_hub != existing_hub
        # A hub WAS minted this time (deviation from the automated cascade
        # callers, which attach nothing on needs_review).
        assert store.count_refs(kind="finding") == before + 1

        todo_handle = _search(r"filed (td\d+)", resp.body).group(1)
        todo_id = int(todo_handle.removeprefix("td"))
        todo_ref = store.get_ref(kind="todo", id=todo_id)
        assert todo_ref is not None
        assert f"fi{new_hub}" in todo_ref.title
        assert f"fi{existing_hub}" in todo_ref.title


class TestPutHubCascadeNew:
    def test_no_candidates_mints_new_hub(self, store) -> None:
        _seed_paper(store, cite_key="miller23a")
        resp = _finding_hub_mint.put_hub(
            store,
            sentence="A wholly novel claim with no neighbours.",
            scope={},
            supporters=[{"paper": "miller23a"}],
            embedder=object(),
            block_fn=_block_none,
            judge_fn=_never_called,
        )
        assert "claim hub fi" in resp.body
        assert "pub_id=" not in resp.body

    def test_new_contradicts_mints_and_links_disputes(self, store) -> None:
        existing_sentence = "Pd/C accelerates the Suzuki reaction at RT."
        existing_hub = mint_hub(
            store, CanonicalClaim(sentence=existing_sentence, scope={})
        )
        _seed_paper(store, cite_key="miller23a")

        resp = _finding_hub_mint.put_hub(
            store,
            sentence="Pd/C has no measurable effect on the Suzuki reaction at RT.",
            scope={},
            supporters=[{"paper": "miller23a"}],
            embedder=object(),
            block_fn=_block_hit(existing_hub, existing_sentence),
            judge_fn=lambda a, b: _verdict("contradicts", 0.9),
        )
        new_hub = int(_search(r"claim hub fi(\d+)", resp.body).group(1))

        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT relation FROM links WHERE src_ref_id = %s AND dst_ref_id = %s",
                (new_hub, existing_hub),
            ).fetchone()
        assert row is not None
        assert row[0] == "disputes"


# ── near-duplicate refusal (hub-mint-gate-fixes.md section 3) ─────────

# Real pair fi530867 vs fi350175 sat at distance 0.294 (< the 0.30 cutoff).
_COMPOUND = "Pd/C catalyzes Suzuki coupling and Heck coupling at room temperature."
_ATOM = "Pd/C catalyzes Suzuki coupling at room temperature."


def _different(a: str, b: str) -> Verdict:
    return _verdict("different", 0.9)


def _containment(kind: str, calls: list | None = None):
    def _fn(new: str, existing: str):
        if calls is not None:
            calls.append((new, existing))
        return {"verdict": kind, "confidence": 0.9, "rationale": f"judged {kind}"}

    return _fn


def _put_atom(store, **kw):
    return _finding_hub_mint.put_hub(
        store,
        sentence=_ATOM,
        scope={},
        supporters=kw.pop("supporters", [{"paper": "miller23a"}]),
        embedder=object(),
        block_fn=kw.pop("block_fn"),
        judge_fn=kw.pop("judge_fn", _different),
        containment_fn=kw.pop("containment_fn", _never_called),
        **kw,
    )


class TestNearDuplicateRefusal:
    def test_component_of_compound_refused_with_handle_in_options(self, store) -> None:
        compound = mint_hub(store, CanonicalClaim(sentence=_COMPOUND, scope={}))
        _seed_paper(store, cite_key="miller23a")
        before = store.count_refs(kind="finding")

        with pytest.raises(BadInput) as ei:
            _put_atom(
                store,
                block_fn=_block_hit(compound, _COMPOUND, distance=0.294),
                containment_fn=_containment("component"),
            )

        err = ei.value
        assert f"fi{compound}" in str(err)
        assert _COMPOUND in str(err)
        opts = " ".join(err.options or [])
        assert f"dedup='attach:fi{compound}'" in opts
        assert "refine" in opts and "dedup='force'" in opts
        assert "atom" not in opts.lower().replace("atomic", "")
        assert store.count_refs(kind="finding") == before

    def test_far_neighbour_not_sent_to_containment_judge(self, store) -> None:
        far = mint_hub(store, CanonicalClaim(sentence=_COMPOUND, scope={}))
        _seed_paper(store, cite_key="miller23a")
        resp = _put_atom(
            store,
            block_fn=_block_hit(far, _COMPOUND, distance=0.45),
            containment_fn=_never_called,
        )
        assert "claim hub fi" in resp.body

    def test_at_most_three_ann_candidates_judged(self, store) -> None:
        hubs = [
            mint_hub(store, CanonicalClaim(sentence=f"Neighbour claim {i}.", scope={}))
            for i in range(5)
        ]
        _seed_paper(store, cite_key="miller23a")

        def _block(claim_obj, st, emb, **kw):
            return [
                MergeCandidate(
                    hub_ref_id=h, claim=f"Neighbour claim {i}.", distance=0.1 + i / 100
                )
                for i, h in enumerate(hubs)
            ]

        calls: list = []
        resp = _put_atom(
            store, block_fn=_block, containment_fn=_containment("different", calls)
        )
        assert "claim hub fi" in resp.body
        assert len(calls) == 3

    def test_same_passage_sibling_without_embedding_found(self, store) -> None:
        paper = _seed_paper(store, cite_key="miller23a")
        with store.pool.connection() as conn:
            chunk_id = conn.execute(
                "SELECT chunk_id FROM chunks WHERE ref_id = %s", (paper,)
            ).fetchone()[0]
        sup = [{"paper": f"pc{chunk_id}", "source_handle": f"pc{chunk_id}"}]
        sibling = _finding_hub_mint.put_hub(
            store,
            sentence=_COMPOUND,
            scope={},
            supporters=sup,
            dedup=False,
        )
        sibling_id = int(_search(r"claim hub fi(\d+)", sibling.body).group(1))

        with pytest.raises(BadInput) as ei:
            _put_atom(
                store,
                supporters=sup,
                block_fn=_block_none,  # no embedding yet: ANN sees nothing
                containment_fn=_containment("component"),
            )
        assert f"fi{sibling_id}" in str(ei.value)

    def test_force_mints_and_stamps_override(self, store) -> None:
        compound = mint_hub(store, CanonicalClaim(sentence=_COMPOUND, scope={}))
        _seed_paper(store, cite_key="miller23a")

        resp = _put_atom(
            store,
            block_fn=_block_hit(compound, _COMPOUND, distance=0.294),
            containment_fn=_containment("generalization"),
            dedup="force",
        )
        new_hub = int(_search(r"claim hub fi(\d+)", resp.body).group(1))
        assert new_hub != compound
        meta = store.get_ref(kind="finding", id=new_hub).meta
        assert meta["dedup_override"] == {
            "neighbour": f"fi{compound}",
            "verdict": "generalization",
            "reason": "judged generalization",
        }

    def test_attach_choice_attaches_without_search_or_judge(self, store) -> None:
        compound = mint_hub(store, CanonicalClaim(sentence=_COMPOUND, scope={}))
        paper = _seed_paper(store, cite_key="miller23a")
        before = store.count_refs(kind="finding")

        resp = _put_atom(
            store,
            block_fn=_never_called,
            judge_fn=_never_called,
            dedup=f"attach:fi{compound}",
        )
        assert "converged onto" in resp.body and f"fi{compound}" in resp.body
        assert store.count_refs(kind="finding") == before
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM links WHERE src_ref_id = %s AND dst_ref_id = %s",
                (paper, compound),
            ).fetchone()
        assert row is not None

    def test_attach_choice_refuses_a_non_claim_hub(self, store) -> None:
        from tests.workers._helpers import seed_ref

        plain = seed_ref(store, title="Not a claim hub.", kind="finding")
        _seed_paper(store, cite_key="miller23a")
        with pytest.raises(BadInput) as ei:
            _put_atom(
                store,
                block_fn=_never_called,
                judge_fn=_never_called,
                dedup=f"attach:fi{plain}",
            )
        assert "not a live claim hub" in str(ei.value)
        assert ei.value.options and ei.value.next

    def test_identical_reput_converges_before_containment(self, store) -> None:
        paper = _seed_paper(store, cite_key="miller23a")
        with store.pool.connection() as conn:
            chunk_id = conn.execute(
                "SELECT chunk_id FROM chunks WHERE ref_id = %s", (paper,)
            ).fetchone()[0]
        sup = [{"paper": f"pc{chunk_id}", "source_handle": f"pc{chunk_id}"}]
        first = _put_atom(store, supporters=sup, block_fn=_never_called, dedup=False)
        hub_id = int(_search(r"claim hub fi(\d+)", first.body).group(1))
        before = store.count_refs(kind="finding")

        # hub has no embedding (block sees nothing) but holds the same
        # passage: containment would name it its own near-duplicate.
        resp = _put_atom(
            store,
            supporters=sup,
            block_fn=_block_none,
            containment_fn=_never_called,
        )
        assert "converged onto" in resp.body and f"fi{hub_id}" in resp.body
        assert store.count_refs(kind="finding") == before

    def test_force_survives_containment_judge_outage(self, store) -> None:
        compound = mint_hub(store, CanonicalClaim(sentence=_COMPOUND, scope={}))
        _seed_paper(store, cite_key="miller23a")

        def _down(new: str, existing: str):
            return {"verdict": "error", "confidence": 0.0, "rationale": "boom"}

        with pytest.raises(Upstream) as ei:
            _put_atom(
                store,
                block_fn=_block_hit(compound, _COMPOUND, distance=0.2),
                containment_fn=_down,
            )
        assert ei.value.next

        resp = _put_atom(
            store,
            block_fn=_block_hit(compound, _COMPOUND, distance=0.2),
            containment_fn=_down,
            dedup="force",
        )
        new_hub = int(_search(r"claim hub fi(\d+)", resp.body).group(1))
        meta = store.get_ref(kind="finding", id=new_hub).meta
        assert meta["dedup_override"]["verdict"] == "unjudged"

    def test_bad_dedup_string_refused_with_options(self, store) -> None:
        _seed_paper(store, cite_key="miller23a")
        with pytest.raises(BadInput) as ei:
            _put_atom(store, block_fn=_never_called, dedup="sometimes")
        assert ei.value.options

    def test_refine_not_offered_at_reviewed_or_later(self, store) -> None:
        compound = mint_hub(store, CanonicalClaim(sentence=_COMPOUND, scope={}))
        _seed_paper(store, cite_key="miller23a")
        with store.pool.connection() as conn:
            conn.execute(
                "INSERT INTO nanopub_publish (claim_ref_id, state) VALUES (%s, 'reviewed')",
                (compound,),
            )
            conn.commit()

        with pytest.raises(BadInput) as ei:
            _put_atom(
                store,
                block_fn=_block_hit(compound, _COMPOUND, distance=0.2),
                containment_fn=_containment("component"),
            )
        opts = " ".join(ei.value.options or [])
        assert "refine:" not in opts
        assert "refine unavailable" in str(ei.value)
        assert "reviewed" in str(ei.value)
        assert f"dedup='attach:fi{compound}'" in opts and "dedup='force'" in opts

    def test_dedup_false_unchanged(self, store) -> None:
        mint_hub(store, CanonicalClaim(sentence=_COMPOUND, scope={}))
        _seed_paper(store, cite_key="miller23a")
        resp = _put_atom(
            store,
            block_fn=_never_called,
            judge_fn=_never_called,
            dedup=False,
        )
        assert "dedup skipped" in resp.body

    def test_handler_threads_dedup_string(self, store) -> None:
        compound = mint_hub(store, CanonicalClaim(sentence=_COMPOUND, scope={}))
        _seed_paper(store, cite_key="miller23a")
        h = _make_handler(store, embedder=object())
        resp = h.put(
            title=_ATOM,
            supporters=[{"paper": "miller23a"}],
            dedup=f"attach:fi{compound}",
        )
        assert "converged onto" in resp.body

    def test_automated_paths_never_reference_containment_judge(self) -> None:
        import inspect

        from precis.taproot import backfill, canon, directed, hub
        from precis.workers import chase

        for mod in (backfill, directed, hub, chase):
            assert "containment" not in inspect.getsource(mod), mod.__name__
        for fn in (canon.judge_candidates, canon.place, canon.block):
            assert "containment" not in inspect.getsource(fn), fn.__name__


# ── view='similar' ────────────────────────────────────────────────────


class TestGetViewSimilar:
    def test_no_embedder_raises_unsupported(self, store) -> None:
        hub_id = mint_hub(
            store, CanonicalClaim(sentence="A claim with no embedder.", scope={})
        )
        h = _make_handler(store, embedder=None)
        with pytest.raises(Unsupported, match="embedder"):
            h.get(id=hub_id, view="similar")

    def test_excludes_self_and_lists_nearest(self, store) -> None:
        from tests.workers._helpers import make_mock_bge_m3

        embedder = make_mock_bge_m3()

        def _mint_and_embed(sentence: str) -> int:
            hub_id = mint_hub(store, CanonicalClaim(sentence=sentence, scope={}))
            with store.pool.connection() as conn:
                row = conn.execute(
                    "SELECT chunk_id FROM chunks WHERE ref_id = %s AND ord = 0 "
                    "AND chunk_kind = 'finding_body'",
                    (hub_id,),
                ).fetchone()
                assert row is not None
                conn.execute(
                    "INSERT INTO chunk_embeddings (chunk_id, embedder, vector, status) "
                    "VALUES (%s, %s, %s, 'ok')",
                    (row[0], "bge-m3", embedder.embed_one(sentence)),
                )
                conn.commit()
            return hub_id

        target = _mint_and_embed("Pd/C catalyzes Suzuki coupling at RT.")
        neighbour = _mint_and_embed("Ni catalyzes Suzuki coupling at RT.")

        h = _make_handler(store, embedder=embedder)
        body = h.get(id=target, view="similar").body

        assert f"fi{target}" not in body
        assert f"fi{neighbour}" in body
        assert "view='merge-plan'" in body  # the apply-door pointer


# ── view='merge-plan' — read-only dry run, apply is a human door ─────────


def _row_counts(store) -> tuple[int, int, int]:
    """(live+retired refs, links, nanopub_publish) — what a merge would touch."""
    with store.pool.connection() as conn:
        return tuple(
            conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            for t in ("refs", "links", "nanopub_publish")
        )


class TestGetViewMergePlan:
    def test_renders_plan_and_writes_nothing(self, store) -> None:
        from precis.taproot.hub import attach_evidence
        from tests.workers._helpers import seed_ref

        winner = mint_hub(store, CanonicalClaim(sentence="Winner hub claim.", scope={}))
        loser = mint_hub(store, CanonicalClaim(sentence="Loser hub claim.", scope={}))
        shared = seed_ref(store, title="Shared paper", kind="paper")
        only_loser = seed_ref(store, title="Loser-only paper", kind="paper")
        attach_evidence(
            store, hub_ref_id=loser, paper_ref_id=shared, role="corroborates"
        )
        attach_evidence(
            store, hub_ref_id=winner, paper_ref_id=shared, role="corroborates"
        )
        attach_evidence(
            store, hub_ref_id=loser, paper_ref_id=only_loser, role="corroborates"
        )
        before = _row_counts(store)

        h = _make_handler(store)
        body = h.get(id=winner, view="merge-plan", loser=f"fi{loser}").body

        assert f"fi{loser} -> fi{winner}" in body
        assert "can_merge: true" in body
        assert "## edges to repoint (1)" in body
        assert "## edges dropped as redundant (1)" in body
        assert "already holds it as link" in body
        assert "## self-loops dropped (0)" in body
        assert body.rstrip().endswith("This view never writes.")
        assert f"/nanopub/fi{winner}" in body
        assert _row_counts(store) == before
        with store.pool.connection() as conn:
            retired = conn.execute(
                "SELECT retired_at FROM refs WHERE ref_id = %s", (loser,)
            ).fetchone()[0]
        assert retired is None

    def test_loser_accepts_int_and_digit_string(self, store) -> None:
        winner = mint_hub(store, CanonicalClaim(sentence="Winner hub claim.", scope={}))
        loser = mint_hub(store, CanonicalClaim(sentence="Loser hub claim.", scope={}))
        h = _make_handler(store)
        assert (
            "can_merge: true" in h.get(id=winner, view="merge-plan", loser=loser).body
        )
        assert (
            "can_merge: true"
            in h.get(id=winner, view="merge-plan", loser=str(loser)).body
        )

    def test_past_candidate_reports_can_merge_false_with_reason(self, store) -> None:
        winner = mint_hub(store, CanonicalClaim(sentence="Winner hub claim.", scope={}))
        loser = mint_hub(store, CanonicalClaim(sentence="Loser hub claim.", scope={}))
        with store.pool.connection() as conn:
            conn.execute(
                "INSERT INTO nanopub_publish (claim_ref_id, state) VALUES (%s, 'reviewed')",
                (loser,),
            )
            conn.commit()
        before = _row_counts(store)

        body = (
            _make_handler(store)
            .get(id=winner, view="merge-plan", loser=f"fi{loser}")
            .body
        )

        assert "can_merge: false" in body
        assert "refused:" in body and "reviewed" in body
        assert _row_counts(store) == before

    def test_missing_loser_is_bad_input_with_call_shape(self, store) -> None:
        winner = mint_hub(store, CanonicalClaim(sentence="Winner hub claim.", scope={}))
        with pytest.raises(BadInput, match="loser") as ei:
            _make_handler(store).get(id=winner, view="merge-plan")
        assert "view='merge-plan'" in str(ei.value.next)

    def test_same_hub_both_sides_is_bad_input(self, store) -> None:
        hub = mint_hub(store, CanonicalClaim(sentence="Winner hub claim.", scope={}))
        with pytest.raises(BadInput, match="itself"):
            _make_handler(store).get(id=hub, view="merge-plan", loser=f"fi{hub}")


# ── embedder outage: typed Upstream, fail closed (gr459918) ───────────────


class _DownEmbedder:
    def embed_one(self, text: str) -> list[float]:
        raise EmbedderUnavailable("embedder at capacity", retry_after_s=2.0)


class TestEmbedderOutage:
    def test_put_hub_raises_upstream_and_mints_nothing(self, store) -> None:
        _seed_paper(store, cite_key="miller23a")
        before = store.count_refs(kind="finding")
        with pytest.raises(Upstream, match="near-duplicate check could not run") as ei:
            _finding_hub_mint.put_hub(
                store,
                sentence="a claim minted during an embedder outage",
                scope={},
                supporters=[{"paper": "miller23a"}],
                embedder=_DownEmbedder(),
            )
        assert "~2 s" in str(ei.value)
        assert "dedup=False" in str(ei.value.next)
        assert store.count_refs(kind="finding") == before

    def test_dispatch_put_is_error_upstream_not_internal(
        self, store, runtime_with_store
    ) -> None:
        _seed_paper(store, cite_key="miller23a")
        runtime_with_store.hub.embedder = _DownEmbedder()
        before = store.count_refs(kind="finding")
        out, is_error = runtime_with_store.dispatch_with_status(
            "put",
            {
                "kind": "finding",
                "title": "a claim minted during an embedder outage",
                "supporters": [{"paper": "miller23a"}],
            },
        )
        assert is_error, out
        assert "[error:Upstream]" in out
        assert "~2 s" in out
        assert "[error:Internal]" not in out
        assert store.count_refs(kind="finding") == before

    def test_nearest_hubs_raises_upstream(self, store) -> None:
        with pytest.raises(Upstream, match="embedder at capacity"):
            nearest_hubs("any sentence", {}, store, _DownEmbedder())
