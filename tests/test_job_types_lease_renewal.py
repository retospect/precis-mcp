"""taproot_backfill and reground_claim's verify mode renew their lease per item
and stop quietly when superseded (gr369528). Offline: scope, embedder and the
per-item passes are faked. Sibling of ``test_reground_claim_lease.py``."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from precis.handlers import draft as draft_mod
from precis.taproot import backfill
from precis.workers import hub_refine
from precis.workers.job_types import reground_claim, taproot_backfill


def _ctx(renew: Any, events: list[tuple[str, str]], params: dict) -> Any:
    return SimpleNamespace(
        store=None,
        ref_id=1,
        meta={"params": params},
        append_chunk=lambda k, t: events.append((k, t)),
        set_meta=lambda **_f: None,
        record_failure=lambda r, **_k: events.append(("fail", r)),
        is_cancel_requested=lambda: False,
        renew_lease=renew,
    )


# ── taproot_backfill ─────────────────────────────────────────────────────


@pytest.fixture
def backfill_seen(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    seen: list[int] = []
    pairs = [
        ("d", SimpleNamespace(chunk_kind="paragraph", chunk_id=i)) for i in (1, 2, 3)
    ]

    class FakeHandler:
        def __init__(self, **_k: Any) -> None: ...

        def _scope_chunks(self, *_a: Any, **_k: Any) -> Any:
            return pairs, "scope"

    def apply(_s: Any, _e: Any, _h: Any, chunk_id: int, **_k: Any) -> Any:
        seen.append(chunk_id)
        return SimpleNamespace(
            plans=[], n_ungrounded=0, rewritten_text=None, chunk_id=chunk_id
        )

    monkeypatch.setattr(taproot_backfill, "_build_embedder", lambda _s: object())
    monkeypatch.setattr(draft_mod, "DraftHandler", FakeHandler)
    monkeypatch.setattr(backfill, "apply_chunk", apply)
    return seen


def test_taproot_backfill_renews_every_chunk(backfill_seen: list[int]) -> None:
    renewals: list[int] = []
    events: list[tuple[str, str]] = []

    def renew() -> bool:
        renewals.append(1)
        return True

    taproot_backfill._dispatch(_ctx(renew, events, {"scope": "x"}), None)

    assert backfill_seen == [1, 2, 3]
    assert len(renewals) == 3
    assert any(k == "job_summary" for k, _ in events)


def test_taproot_backfill_lost_lease_stops(backfill_seen: list[int]) -> None:
    answers = iter([True, False])
    events: list[tuple[str, str]] = []

    taproot_backfill._dispatch(
        _ctx(lambda: next(answers), events, {"scope": "x"}), None
    )

    assert backfill_seen == [1]
    assert not any(k == "job_summary" for k, _ in events)
    assert any("lease lost" in t for _, t in events)


# ── reground_claim verify mode ───────────────────────────────────────────


@pytest.fixture
def verify_seen(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    seen: list[int] = []
    monkeypatch.setattr(reground_claim, "_resolve_scope", lambda *_a, **_k: [1, 2, 3])

    def verify(_store: Any, hub_id: int) -> Any:
        seen.append(hub_id)
        return SimpleNamespace(has_intent=True, clean=True)

    monkeypatch.setattr(hub_refine, "verify_hub_intent", verify)
    return seen


def test_verify_mode_renews_every_hub(verify_seen: list[int]) -> None:
    renewals: list[int] = []
    events: list[tuple[str, str]] = []

    def renew() -> bool:
        renewals.append(1)
        return True

    reground_claim._dispatch(_ctx(renew, events, {"mode": "verify"}), None)

    assert verify_seen == [1, 2, 3]
    assert len(renewals) == 3
    assert any(k == "job_summary" for k, _ in events)


def test_verify_mode_lost_lease_stops(verify_seen: list[int]) -> None:
    answers = iter([True, False])
    events: list[tuple[str, str]] = []

    reground_claim._dispatch(
        _ctx(lambda: next(answers), events, {"mode": "verify"}), None
    )

    assert verify_seen == [1]
    assert not any(k == "job_summary" for k, _ in events)
    assert any("lease lost" in t for _, t in events)
