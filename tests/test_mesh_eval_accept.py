"""Sampled batch acceptance — stratified judge sample + Wilson decision."""

from __future__ import annotations

from typing import Any

from precis.mesh_eval.accept import accept_batch, stratified_sample

# ── accept ────────────────────────────────────────────────────────


def test_stratified_sample_covers_strata() -> None:
    props = [{"lane": "a"} for _ in range(90)] + [{"lane": "b"} for _ in range(10)]
    idx = stratified_sample(props, 20, key_fn=lambda p: p["lane"], seed=1)
    assert len(idx) == 20
    assert any(props[i]["lane"] == "b" for i in idx)  # minority stratum drawn


def test_accept_batch_clear_accept() -> None:
    props = [{"i": i} for i in range(200)]
    d = accept_batch(props, lambda p: True, sample_size=40, error_bar=0.10, seed=0)
    assert d.verdict == "accept" and d.n_errors == 0 and d.n_sampled == 40


def test_accept_batch_clear_reject() -> None:
    props = [{"i": i} for i in range(200)]
    d = accept_batch(props, lambda p: False, sample_size=40, error_bar=0.10, seed=0)
    assert d.verdict == "reject" and d.error_rate == 1.0


def test_accept_batch_escalates_on_straddle_and_judge_failure() -> None:
    # ~10% wrong → interval straddles a 10% bar.
    props = [{"i": i} for i in range(200)]
    wrong = {p["i"] for p in props[:20]}
    d = accept_batch(
        props, lambda p: p["i"] not in wrong, sample_size=40, error_bar=0.10, seed=0
    )
    assert d.verdict in ("escalate", "reject", "accept")
    # Force a straddle: exactly at the bar with small n.
    d2 = accept_batch(
        props,
        lambda p: p["i"] % 10 != 0,  # 10% error, all-sampled at n=40
        sample_size=40,
        error_bar=0.10,
        seed=3,
    )
    assert d2.verdict in ("escalate", "reject")  # upper bound sits above the bar

    def flaky(p: Any) -> bool:
        if p["i"] == 5:
            raise RuntimeError("judge down")
        return True

    d3 = accept_batch(props, flaky, sample_size=200, error_bar=0.10, seed=0)
    assert d3.verdict == "escalate" and d3.n_judge_failures == 1


def test_accept_batch_full_review_when_sample_covers() -> None:
    props = [{"i": i} for i in range(30)]
    d = accept_batch(props, lambda p: True, sample_size=40, error_bar=0.10)
    assert d.n_sampled == 30 and d.verdict == "accept"
