"""Bounded cost of the term-coverage scan (``precis.nanopub.term_coverage``):
the claim page runs it on every render, so it caps what it loads, loads once
per render, memoises the result, and says when it only scanned part of the
papers. DB-backed."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from precis.nanopub import evidence, term_coverage
from precis.nanopub.keys import generate_keypair
from precis.taproot.hub import attach_evidence
from precis_web.nanopub_render import hub_context
from tests.test_nanopub_gates_mint import _seed_hub, _seed_paper
from tests.test_nanopub_term_coverage import _add_chunk

_SENTENCE = "TEM shows MOFs can be anisotropic up to 400:1."


@pytest.fixture(autouse=True)
def _env(monkeypatch: Any) -> Any:
    priv, _pub = generate_keypair(2048)
    monkeypatch.setenv("NANOPUB_BOT_PRIVATE_KEY", priv)
    term_coverage._RESULT_CACHE.clear()
    yield
    term_coverage._RESULT_CACHE.clear()


def _big_paper_hub(store: Any, n_body: int) -> tuple[int, int, int]:
    """An unminted method-claim hub whose one paper has ``n_body`` plain body
    chunks (ord 10..) plus a TEM figure caption at the very last ord."""
    paper, chunk, _sha = _seed_paper(store)
    hub = _seed_hub(store, _SENTENCE, paper, chunk)
    for i in range(n_body):
        _add_chunk(store, paper, 10 + i, f"Paragraph {i} about the study.", ["Intro"])
    caption = _add_chunk(
        store,
        paper,
        10 + n_body,
        "Figure 9 TEM image of the MOF crystals.",
        ["Results"],
    )
    return hub, paper, caption


def test_load_honours_the_per_paper_cap_and_prefers_captions(store: Any) -> None:
    _hub, paper, caption = _big_paper_hub(store, 30)
    scan = term_coverage.CoverageScan(store, [paper], per_paper=5)
    chunks, _abstracts = scan.load()
    assert len(chunks) == 5  # 32 live body chunks, capped at 5
    assert caption in {c.chunk_id for c in chunks}  # last ord, but a caption
    assert [c.ord for c in chunks] == sorted(c.ord for c in chunks)
    assert scan.fingerprint[0] == 32  # the aggregate still sees every chunk


def test_load_honours_the_character_budget(store: Any) -> None:
    _hub, paper, _caption = _big_paper_hub(store, 30)
    scan = term_coverage.CoverageScan(store, [paper], budget=100)
    chunks, _ = scan.load()
    # best-ranked first until the running sum passes the budget; never empty
    assert 1 <= len(chunks) < 10
    assert sum(len(c.text) for c in chunks[:-1]) < 100 + max(
        len(c.text) for c in chunks
    )


def test_aggregate_and_load_are_one_query_each(store: Any) -> None:
    _hub, paper, _caption = _big_paper_hub(store, 3)
    scan = term_coverage.CoverageScan(store, [paper])
    scan.sizes()
    scan.load()
    scan.sizes()
    scan.load()
    assert scan.queries == 2


def test_partial_scan_is_said_in_the_warning(store: Any) -> None:
    hub, paper, _caption = _big_paper_hub(store, 30)
    bundle = evidence.load_bundle(store, hub)
    grounding = {"passages": [{"quote": "The crystals are anisotropic at 400:1."}]}
    capped = term_coverage.CoverageScan(store, [paper], per_paper=5)
    result = term_coverage.coverage_result(
        store, hub, _SENTENCE, grounding, bundle=bundle, scan=capped
    )
    assert result.partial and (result.scanned, result.total) == (5, 32)
    assert [i.term.text for i in result.items] == ["TEM"]
    assert result.items[0].suggestions  # the caption survived the cap
    msg = term_coverage.coverage_warning(
        store, hub, _SENTENCE, grounding, bundle=bundle, scan=capped
    )
    assert msg is not None and "Partial scan: searched the best 5 of 32" in msg

    whole = term_coverage.coverage_result(store, hub, _SENTENCE, grounding)
    assert not whole.partial and whole.scanned == whole.total == 32
    full_msg = term_coverage.coverage_warning(store, hub, _SENTENCE, grounding)
    assert full_msg is not None and "Partial scan" not in full_msg


def test_second_render_hits_the_cache(store: Any) -> None:
    hub, _paper, _caption = _big_paper_hub(store, 3)
    with patch.object(
        term_coverage, "_load_capped", wraps=term_coverage._load_capped
    ) as spy:
        first = hub_context(store, hub, embedder=None)
        second = hub_context(store, hub, embedder=None)
    assert first is not None and second is not None
    assert spy.call_count == 1  # the reload recomputed nothing
    cov = [i for i in second["preflight"] if i.check == "term-coverage"]
    assert len(cov) == 1 and "TEM" in cov[0].message


def test_new_chunk_in_the_papers_busts_the_cache(store: Any) -> None:
    hub, paper, _caption = _big_paper_hub(store, 3)
    with patch.object(
        term_coverage, "_load_capped", wraps=term_coverage._load_capped
    ) as spy:
        hub_context(store, hub, embedder=None)
        _add_chunk(store, paper, 99, "One more paragraph.", ["Intro"])
        hub_context(store, hub, embedder=None)
    assert spy.call_count == 2


def test_one_render_loads_the_papers_once(store: Any) -> None:
    """The approve render builds the prefill and runs the coverage check; both
    share one scan — one capped text load, no unbounded ``paper_chunks``."""
    hub, _paper, caption = _big_paper_hub(store, 3)
    attach_evidence(
        store,
        hub_ref_id=hub,
        paper_ref_id=_paper,
        role="corroborates",
        meta={"source_handle": f"pc{caption}"},
        check_retraction=False,
    )
    with (
        patch.object(
            term_coverage, "_load_capped", wraps=term_coverage._load_capped
        ) as load,
        patch.object(term_coverage, "paper_chunks") as unbounded,
    ):
        ctx = hub_context(store, hub, embedder=None)
    assert ctx is not None
    assert load.call_count == 1
    unbounded.assert_not_called()
    # the prefill (caption first) came out of the same render
    assert f'"chunk_id": {caption}' in ctx["suggested_payload"]


def test_cache_is_bounded(store: Any, monkeypatch: Any) -> None:
    monkeypatch.setattr(term_coverage, "_CACHE_SIZE", 2)
    hub, _paper, _caption = _big_paper_hub(store, 3)
    for n in range(4):
        term_coverage.coverage_result(
            store, hub, f"TEM shows {n} MOFs.", {"passages": [{"quote": "MOFs."}]}
        )
    assert len(term_coverage._RESULT_CACHE) == 2
