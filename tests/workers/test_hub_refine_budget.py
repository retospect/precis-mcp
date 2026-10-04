"""Pass wall budget + per-hub verifier cap for ``run_hub_refine_pass``.

2026-10-04: one pass claimed 8 hubs and ran 231 serial Haiku verifies
(~60 min), holding the fetcher host's single worker. The pass now stops
starting hubs once ``PRECIS_TAPROOT_REFINE_PASS_WALL_S`` is spent (releasing
the unstarted ones back to the due-set) and one hub stops after
``PRECIS_TAPROOT_REFINE_VERIFY_PER_HUB`` verifier calls.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from precis.store.types import Tag
from precis.workers import hub_refine
from precis.workers.hub_refine import _ATTEMPT_NS, _ATTEMPT_VALUE, run_hub_refine_pass
from tests.workers._helpers import make_mock_bge_m3
from tests.workers.test_hub_refine import (
    _VERIFY_NO,
    _VERIFY_PATH,
    _hub_meta,
    _seed_hub,
    _seed_paper_chunk,
)

_WALL_ENV = "PRECIS_TAPROOT_REFINE_PASS_WALL_S"
_CAP_ENV = "PRECIS_TAPROOT_REFINE_VERIFY_PER_HUB"


def _has_lease(store: Any, hub: int) -> bool:
    return bool(store.has_tag(hub, _ATTEMPT_NS, _ATTEMPT_VALUE))


def _is_tagged_due(store: Any, hub: int) -> bool:
    return bool(store.has_tag(hub, "TAPROOT_DUE", "1"))


def test_wall_budget_runs_first_hub_and_releases_the_rest(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the wall budget already spent (0 s) the first hub still runs
    (progress guarantee); the unstarted ones are released — due tag back,
    claim-time lease gone — counted as ``deferred``, and picked up by the
    next pass."""
    embedder = make_mock_bge_m3()
    hubs = [
        _seed_hub(store, sentence=f"Wall budget claim number {i} about a device.")
        for i in range(3)
    ]
    monkeypatch.setenv(_WALL_ENV, "0")
    with patch(_VERIFY_PATH, return_value=_VERIFY_NO):
        result = run_hub_refine_pass(store, limit=10, embedder=embedder, topk=8)
    assert result == {"claimed": 3, "ok": 1, "failed": 0, "deferred": 2}

    assert _hub_meta(store, hubs[0]).get("last_refined_at") is not None
    for hub in hubs[1:]:
        assert _hub_meta(store, hub).get("last_refined_at") is None
        assert _is_tagged_due(store, hub)
        assert not _has_lease(store, hub)

    monkeypatch.delenv(_WALL_ENV)
    with patch(_VERIFY_PATH, return_value=_VERIFY_NO):
        second = run_hub_refine_pass(store, limit=10, embedder=embedder, topk=8)
    assert second == {"claimed": 2, "ok": 2, "failed": 0}
    for hub in hubs[1:]:
        assert _hub_meta(store, hub).get("last_refined_at") is not None


def test_wall_budget_deferred_refined_hub_stays_due_via_the_tag(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refined hub is due only through its re-trigger tag — the release
    must put that back, or the claim-time pop would strand it until the
    90-day backstop."""
    embedder = make_mock_bge_m3()
    hubs = [
        _seed_hub(store, sentence=f"Refined wall claim number {i} about a device.")
        for i in range(2)
    ]
    with patch(_VERIFY_PATH, return_value=_VERIFY_NO):
        run_hub_refine_pass(store, limit=10, embedder=embedder, topk=8)
    for hub in hubs:
        store.add_tag(hub, Tag.closed("TAPROOT_DUE", "1"), set_by="system")

    monkeypatch.setenv(_WALL_ENV, "0")
    with patch(_VERIFY_PATH, return_value=_VERIFY_NO):
        result = run_hub_refine_pass(store, limit=10, embedder=embedder, topk=8)
    assert result["deferred"] == 1
    tagged = [h for h in hubs if _is_tagged_due(store, h)]
    assert len(tagged) == 1
    assert not _has_lease(store, tagged[0])

    monkeypatch.delenv(_WALL_ENV)
    with patch(_VERIFY_PATH, return_value=_VERIFY_NO):
        second = run_hub_refine_pass(store, limit=10, embedder=embedder, topk=8)
    assert second == {"claimed": 1, "ok": 1, "failed": 0}
    assert not _is_tagged_due(store, tagged[0])


def test_wall_budget_not_spent_defers_nothing(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    embedder = make_mock_bge_m3()
    for i in range(2):
        _seed_hub(store, sentence=f"Ample budget claim number {i} about a device.")
    monkeypatch.setenv(_WALL_ENV, "3600")
    with patch(_VERIFY_PATH, return_value=_VERIFY_NO):
        result = run_hub_refine_pass(store, limit=10, embedder=embedder, topk=8)
    assert result == {"claimed": 2, "ok": 2, "failed": 0}


def test_env_knob_parse_fallbacks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_WALL_ENV, raising=False)
    assert hub_refine._pass_wall_s() == 600.0
    monkeypatch.setenv(_WALL_ENV, "bogus")
    assert hub_refine._pass_wall_s() == 600.0
    monkeypatch.setenv(_WALL_ENV, "45")
    assert hub_refine._pass_wall_s() == 45.0
    monkeypatch.delenv(_CAP_ENV, raising=False)
    assert hub_refine._verify_per_hub() == 12
    monkeypatch.setenv(_CAP_ENV, "x")
    assert hub_refine._verify_per_hub() == 12


def test_per_hub_verify_cap_stops_discovery_and_hub_is_due_again(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cap stops discovery after N verifier calls; the hub is stamped
    (verdicts reached are memoed) but re-marked due, and each following pass
    verifies the next batch until the remainder fits under the cap."""
    embedder = make_mock_bge_m3()
    hub = _seed_hub(store, sentence="A claim with many near candidate papers.")
    for i in range(5):
        _seed_paper_chunk(
            store, embedder, cite_key=f"cap{i}", text=f"Candidate passage {i}."
        )
    monkeypatch.setenv(_CAP_ENV, "2")

    with patch(_VERIFY_PATH, return_value=_VERIFY_NO) as mock_verify:
        first = run_hub_refine_pass(store, limit=10, embedder=embedder, topk=8)
        assert first == {"claimed": 1, "ok": 1, "failed": 0}
        assert mock_verify.call_count == 2
        assert _hub_meta(store, hub).get("last_refined_at") is not None
        assert len(_hub_meta(store, hub)["taproot_rejected"]) == 2
        assert _is_tagged_due(store, hub)
        assert not _has_lease(store, hub)

        run_hub_refine_pass(store, limit=10, embedder=embedder, topk=8)
        assert mock_verify.call_count == 4
        assert _is_tagged_due(store, hub)

        # Last candidate: 1 call < cap, nothing cut off -> not re-marked due.
        run_hub_refine_pass(store, limit=10, embedder=embedder, topk=8)
        assert mock_verify.call_count == 5
        assert not _is_tagged_due(store, hub)
        assert len(_hub_meta(store, hub)["taproot_rejected"]) == 5


def test_per_hub_verify_cap_with_a_dead_verifier_does_not_redue(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every call returning None (verifier down) made no progress, so the cap
    must not re-mark the hub due — that would re-spend the same failing
    calls every tick."""
    embedder = make_mock_bge_m3()
    hub = _seed_hub(store, sentence="A claim while the verifier is down.")
    for i in range(4):
        _seed_paper_chunk(
            store, embedder, cite_key=f"dead{i}", text=f"Candidate passage {i}."
        )
    monkeypatch.setenv(_CAP_ENV, "2")
    with patch(_VERIFY_PATH, return_value=None) as mock_verify:
        run_hub_refine_pass(store, limit=10, embedder=embedder, topk=8)
    assert mock_verify.call_count == 2
    assert not _is_tagged_due(store, hub)


def test_a_failed_release_neither_fails_the_pass_nor_strands_the_others(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each unstarted hub is released on its own: one raising release is
    logged, the pass still returns, and the other hubs get their tag back."""
    embedder = make_mock_bge_m3()
    hubs = [
        _seed_hub(store, sentence=f"Release failure claim number {i} about a device.")
        for i in range(3)
    ]
    real_remove = store.remove_tag

    def remove_tag(ref_id: int, tag: Tag, **kw: Any) -> Any:
        if ref_id == hubs[1] and tag.prefix == _ATTEMPT_NS:
            raise RuntimeError("release failed")
        return real_remove(ref_id, tag, **kw)

    monkeypatch.setattr(store, "remove_tag", remove_tag)
    monkeypatch.setenv(_WALL_ENV, "0")
    with patch(_VERIFY_PATH, return_value=_VERIFY_NO):
        result = run_hub_refine_pass(store, limit=10, embedder=embedder, topk=8)
    assert result == {"claimed": 3, "ok": 1, "failed": 0, "deferred": 2}
    assert _has_lease(store, hubs[1])
    assert _is_tagged_due(store, hubs[2])
    assert not _has_lease(store, hubs[2])


def test_a_capped_hub_goes_behind_the_other_due_hubs(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cap's stamp moves ``last_refined_at`` to now, so the re-marked hub
    sorts behind every due hub refined earlier and cannot hold the front of
    each pass."""
    embedder = make_mock_bge_m3()
    hubs = [
        _seed_hub(
            store, sentence=f"A claim number {i} with many near candidate papers."
        )
        for i in range(2)
    ]
    with patch(_VERIFY_PATH, return_value=_VERIFY_NO):
        run_hub_refine_pass(store, limit=10, embedder=embedder, topk=8)
    for i in range(4):
        _seed_paper_chunk(
            store, embedder, cite_key=f"order{i}", text=f"Candidate passage {i}."
        )
    for hub in hubs:
        store.add_tag(hub, Tag.closed("TAPROOT_DUE", "1"), set_by="system")
    monkeypatch.setenv(_CAP_ENV, "2")

    def memo(hub: int) -> int:
        return len(_hub_meta(store, hub).get("taproot_rejected") or {})

    with patch(_VERIFY_PATH, return_value=_VERIFY_NO):
        run_hub_refine_pass(store, limit=1, embedder=embedder, topk=8)
        capped = next(h for h in hubs if memo(h) == 2)
        other = next(h for h in hubs if h != capped)
        assert _is_tagged_due(store, capped)
        assert memo(other) == 0

        run_hub_refine_pass(store, limit=1, embedder=embedder, topk=8)
    assert memo(other) == 2
    assert memo(capped) == 2
