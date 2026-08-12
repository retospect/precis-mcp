"""Tests for the factory ``service_config`` live run control (slice 2).

Two layers: the :class:`ServiceConfigResolver` read/write behaviour against
a real DB (prio override, exact-host precedence, TTL cache), and the
``run_loop`` per-cycle ``pass_gate`` hook (no DB) that a live flip rides.
"""

from __future__ import annotations

import pytest

from precis.workers.runner import BatchResult, run_loop
from precis.workers.service_config import (
    DEFAULT_PRIO,
    ServiceConfigResolver,
    clear_service_config,
    list_service_config,
    seed_service_prio,
    set_service_concurrency,
    set_service_model,
    set_service_prio,
)

# ---------------------------------------------------------------------------
# Resolver — real DB
# ---------------------------------------------------------------------------


def test_resolver_defaults_when_table_empty(store) -> None:
    """No row ⇒ the env/profile default decides (byte-identical to today)."""
    r = ServiceConfigResolver(store, host="melchior", ttl_s=0.0)
    # default_on carries the env/profile verdict through untouched.
    assert r.enabled("classify", default_on=True) is True
    assert r.enabled("classify", default_on=False) is False
    # prio() returns the supplied default when unset.
    assert r.prio("classify", default=DEFAULT_PRIO) == DEFAULT_PRIO


def test_prio_zero_disables_and_nonzero_enables(store) -> None:
    """prio 0 forces off even when default_on; prio>=1 forces on when off."""
    set_service_prio(store, "melchior", "classify", 0, actor="test")
    set_service_prio(store, "melchior", "llm_reconcile", 3, actor="test")
    r = ServiceConfigResolver(store, host="melchior", ttl_s=0.0)
    # a default-on pass turned off by the row
    assert r.enabled("classify", default_on=True) is False
    # a default-off pass turned on by the row
    assert r.enabled("llm_reconcile", default_on=False) is True
    assert r.prio("llm_reconcile", default=0) == 3


def test_exact_host_wins_over_wildcard(store) -> None:
    """A concrete-host row overrides the ``*`` all-hosts default."""
    set_service_prio(store, "*", "classify", 7, actor="test")
    set_service_prio(store, "melchior", "classify", 0, actor="test")
    r_mel = ServiceConfigResolver(store, host="melchior", ttl_s=0.0)
    r_cas = ServiceConfigResolver(store, host="caspar", ttl_s=0.0)
    # melchior has an exact 0 → off; caspar falls to the wildcard 7 → on.
    assert r_mel.prio("classify", default=DEFAULT_PRIO) == 0
    assert r_cas.prio("classify", default=DEFAULT_PRIO) == 7


def test_ttl_cache_holds_then_invalidate_refreshes(store) -> None:
    """A long TTL caches the first read; invalidate() forces a re-read."""
    r = ServiceConfigResolver(store, host="melchior", ttl_s=3600.0)
    assert r.prio("classify", default=DEFAULT_PRIO) == DEFAULT_PRIO  # warms cache
    set_service_prio(store, "melchior", "classify", 0, actor="test")
    # still cached (TTL not expired) → old value
    assert r.prio("classify", default=DEFAULT_PRIO) == DEFAULT_PRIO
    r.invalidate()
    assert r.prio("classify", default=DEFAULT_PRIO) == 0


def test_set_clear_and_list_roundtrip(store) -> None:
    set_service_prio(store, "melchior", "classify", 2, actor="reto")
    set_service_model(store, "melchior", "briefing", "claude-opus-4-8", actor="reto")
    rows = list_service_config(store)
    by_key = {(r["host"], r["service"]): r for r in rows}
    assert by_key[("melchior", "classify")]["prio"] == 2
    assert by_key[("melchior", "briefing")]["model_pref"] == "claude-opus-4-8"
    # model set didn't disturb prio (defaulted to DEFAULT_PRIO for the new row)
    assert by_key[("melchior", "briefing")]["prio"] == DEFAULT_PRIO

    assert clear_service_config(store, "melchior", "classify") is True
    assert clear_service_config(store, "melchior", "classify") is False  # gone now
    left = {(r["host"], r["service"]) for r in list_service_config(store)}
    assert ("melchior", "classify") not in left
    assert ("melchior", "briefing") in left


def test_set_prio_rejects_out_of_range(store) -> None:
    with pytest.raises(ValueError):
        set_service_prio(store, "melchior", "classify", 11)
    with pytest.raises(ValueError):
        set_service_prio(store, "melchior", "classify", -1)


def test_model_pin_survives_prio_flip(store) -> None:
    """`service prio` must not wipe a separately-set model_pref."""
    set_service_model(store, "melchior", "briefing", "claude-opus-4-8")
    set_service_prio(store, "melchior", "briefing", 0)  # no model_pref supplied
    rows = {(r["host"], r["service"]): r for r in list_service_config(store)}
    row = rows[("melchior", "briefing")]
    assert row["prio"] == 0
    assert row["model_pref"] == "claude-opus-4-8"  # preserved by COALESCE


# ---------------------------------------------------------------------------
# concurrency (migration 0091) — mirrors the prio resolver tests above
# ---------------------------------------------------------------------------


def test_concurrency_defaults_when_no_row_or_value(store) -> None:
    """No row, or a row with ``concurrency`` left NULL, both fall through to
    the caller-supplied default (1, serial) — byte-identical to today."""
    r = ServiceConfigResolver(store, host="melchior", ttl_s=0.0)
    assert r.concurrency("classify", default=1) == 1
    # a row that only sets prio (concurrency stays NULL) still defaults.
    set_service_prio(store, "melchior", "classify", 3, actor="test")
    r.invalidate()
    assert r.concurrency("classify", default=1) == 1


def test_concurrency_override_roundtrip(store) -> None:
    set_service_concurrency(store, "melchior", "classify", 6, actor="test")
    r = ServiceConfigResolver(store, host="melchior", ttl_s=0.0)
    assert r.concurrency("classify", default=1) == 6


def test_concurrency_exact_host_wins_over_wildcard(store) -> None:
    """Same exact-host-over-``*`` precedence as ``prio``."""
    set_service_concurrency(store, "*", "classify", 4, actor="test")
    set_service_concurrency(store, "melchior", "classify", 8, actor="test")
    r_mel = ServiceConfigResolver(store, host="melchior", ttl_s=0.0)
    r_cas = ServiceConfigResolver(store, host="caspar", ttl_s=0.0)
    assert r_mel.concurrency("classify", default=1) == 8
    assert r_cas.concurrency("classify", default=1) == 4  # falls to the wildcard


def test_concurrency_clear_reverts_to_default(store) -> None:
    """Setting ``concurrency=None`` reverts to the default without touching
    a separately-set prio."""
    set_service_prio(store, "melchior", "classify", 3, actor="test")
    set_service_concurrency(store, "melchior", "classify", 6, actor="test")
    set_service_concurrency(store, "melchior", "classify", None, actor="test")
    r = ServiceConfigResolver(store, host="melchior", ttl_s=0.0)
    assert r.concurrency("classify", default=1) == 1
    assert r.prio("classify", default=DEFAULT_PRIO) == 3  # untouched


def test_set_service_concurrency_rejects_non_positive(store) -> None:
    with pytest.raises(ValueError):
        set_service_concurrency(store, "melchior", "classify", 0)
    with pytest.raises(ValueError):
        set_service_concurrency(store, "melchior", "classify", -1)


def test_list_service_config_reports_concurrency(store) -> None:
    set_service_concurrency(store, "melchior", "classify", 5, actor="reto")
    rows = {(r["host"], r["service"]): r for r in list_service_config(store)}
    assert rows[("melchior", "classify")]["concurrency"] == 5


# ---------------------------------------------------------------------------
# seed_service_prio (§L deploy-time seed — INSERT ... ON CONFLICT DO NOTHING)
# ---------------------------------------------------------------------------


def test_seed_inserts_when_absent(store) -> None:
    inserted = seed_service_prio(store, "melchior", "classify", 5, actor="deploy")
    assert inserted is True
    rows = {(r["host"], r["service"]): r for r in list_service_config(store)}
    assert rows[("melchior", "classify")]["prio"] == 5


def test_seed_never_clobbers_an_existing_row(store) -> None:
    """The whole point of ``seed`` vs ``set_service_prio``: a console
    operator's override must survive a redeploy re-running the seed task."""
    set_service_prio(store, "melchior", "classify", 0, actor="console-operator")
    inserted = seed_service_prio(store, "melchior", "classify", 5, actor="deploy")
    assert inserted is False
    rows = {(r["host"], r["service"]): r for r in list_service_config(store)}
    assert rows[("melchior", "classify")]["prio"] == 0  # untouched
    assert rows[("melchior", "classify")]["actor"] == "console-operator"


def test_seed_rejects_out_of_range_prio(store) -> None:
    with pytest.raises(ValueError):
        seed_service_prio(store, "melchior", "classify", 11)


# ---------------------------------------------------------------------------
# run_loop pass_gate — no DB
# ---------------------------------------------------------------------------


def test_run_loop_pass_gate_skips_disabled_pass() -> None:
    """A ref-pass whose gate returns False is skipped that cycle.

    The gate receives the service name derived from the closure
    ``__name__`` (``_beta_pass`` → ``beta``), mirroring the ref-pass
    priority table's ``__name__`` keying.
    """
    calls: list[str] = []

    def _alpha_pass(batch_size: int) -> BatchResult:
        calls.append("alpha")
        return BatchResult("alpha", 0, 0, 0)

    def _beta_pass(batch_size: int) -> BatchResult:
        calls.append("beta")
        return BatchResult("beta", 0, 0, 0)

    run_loop(
        handlers=[],
        store=None,  # type: ignore[arg-type]  # ref-only loop never touches store
        once=True,
        ref_passes=[_alpha_pass, _beta_pass],
        pass_gate=lambda service: service != "beta",
    )
    assert calls == ["alpha"]  # beta gated off


def test_run_loop_no_gate_runs_everything() -> None:
    """With no gate, every registered pass runs (unchanged behaviour)."""
    calls: list[str] = []

    def _alpha_pass(batch_size: int) -> BatchResult:
        calls.append("alpha")
        return BatchResult("alpha", 0, 0, 0)

    def _beta_pass(batch_size: int) -> BatchResult:
        calls.append("beta")
        return BatchResult("beta", 0, 0, 0)

    run_loop(
        handlers=[],
        store=None,  # type: ignore[arg-type]  # ref-only loop never touches store
        once=True,
        ref_passes=[_alpha_pass, _beta_pass],
    )
    assert calls == ["alpha", "beta"]


def test_run_loop_stamps_activity_around_each_ref_pass(monkeypatch) -> None:
    """``run_loop`` wraps each ref-pass call with
    ``activity.set_pass``/``activity.clear`` (precis.workers.activity) — a
    long, log-silent pass is otherwise indistinguishable from a dead
    worker (the 2026-08-09 fetch_oa monopolization)."""
    from precis.workers import activity

    monkeypatch.setattr(activity, "_state", {})
    seen_mid_pass: dict[str, object] = {}

    def _alpha_pass(batch_size: int) -> BatchResult:
        # Snapshot activity state WHILE the pass is "running" — this is
        # exactly what the heartbeat thread reads concurrently in prod.
        seen_mid_pass.update(activity.snapshot())
        return BatchResult("alpha", 0, 0, 0)

    run_loop(
        handlers=[],
        store=None,  # type: ignore[arg-type]  # ref-only loop never touches store
        once=True,
        ref_passes=[_alpha_pass],
    )

    assert seen_mid_pass.get("pass") == "_alpha_pass"
    # cleared after the pass returns.
    assert activity.snapshot().get("idle") is True
    assert activity.snapshot().get("last_pass") == "_alpha_pass"


def test_run_loop_clears_activity_even_when_a_pass_raises(monkeypatch) -> None:
    """A raising ref-pass must still clear activity (the ``finally``) — a
    poison pass must not leave a stale "running" stamp forever."""
    from precis.workers import activity

    monkeypatch.setattr(activity, "_state", {})

    def _boom_pass(batch_size: int) -> BatchResult:
        raise RuntimeError("boom")

    run_loop(
        handlers=[],
        store=None,  # type: ignore[arg-type]  # ref-only loop never touches store
        once=True,
        ref_passes=[_boom_pass],
    )

    assert activity.snapshot().get("idle") is True
    assert activity.snapshot().get("last_pass") == "_boom_pass"
