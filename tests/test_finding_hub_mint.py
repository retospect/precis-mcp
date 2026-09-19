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
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, Unsupported
from precis.handlers import _finding_hub_mint
from precis.handlers.finding import FindingHandler
from precis.store.types import ChunkInsert
from precis.taproot.canon import CanonicalClaim, MergeCandidate, Verdict, Verdict3
from precis.taproot.hub import mint_hub


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
