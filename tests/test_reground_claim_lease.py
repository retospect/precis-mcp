"""reground_claim renews its lease between hubs and stops when superseded
(gr369528). Offline: scope, embedder and the per-hub pass are faked."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from precis.workers import hub_refine
from precis.workers.job_types import reground_claim


def _ctx(renew: Any, events: list[tuple[str, str]], meta_calls: list[dict]) -> Any:
    return SimpleNamespace(
        store=None,
        ref_id=1,
        meta={"params": {"hub_ids": [1, 2, 3]}},
        append_chunk=lambda k, t: events.append((k, t)),
        set_meta=lambda **f: meta_calls.append(f),
        record_failure=lambda r, **_k: events.append(("fail", r)),
        is_cancel_requested=lambda: False,
        renew_lease=renew,
    )


@pytest.fixture
def faked(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    seen: list[int] = []
    monkeypatch.setattr(reground_claim, "_resolve_scope", lambda *_a, **_k: [1, 2, 3])
    monkeypatch.setattr(reground_claim, "_build_embedder", lambda _s: object())
    monkeypatch.setattr(hub_refine, "prune_interlock_open", lambda: False)

    def one(_store: Any, hub_id: int, **_k: Any) -> Any:
        seen.append(hub_id)
        return SimpleNamespace(
            confirmed_adds=0, pruned=0, withheld=0, clean=True, as_dict=lambda: {}
        )

    monkeypatch.setattr(hub_refine, "reground_one_hub", one)
    return seen


def test_renews_every_hub_and_finishes(faked: list[int]) -> None:
    renewals: list[int] = []
    events: list[tuple[str, str]] = []
    metas: list[dict] = []

    def renew() -> bool:
        renewals.append(1)
        return True

    reground_claim._dispatch(_ctx(renew, events, metas), None)

    assert faked == [1, 2, 3]
    assert len(renewals) == 3
    assert any(k == "job_summary" for k, _ in events)


def test_lost_lease_stops_without_summary(faked: list[int]) -> None:
    answers = iter([True, False])
    events: list[tuple[str, str]] = []
    metas: list[dict] = []

    reground_claim._dispatch(_ctx(lambda: next(answers), events, metas), None)

    assert faked == [1]  # hub 2 never started
    assert not any(k == "job_summary" for k, _ in events)
    assert any("lease lost" in t for _, t in events)
