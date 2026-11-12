"""``release_confirm`` (td573432): the evidence the factory attests with
after a deploy — every live worker's heartbeat ``code_sha`` plus the
web's boot-sha header when ``release.health_url`` is set — and the
store-side writers (:func:`precis.plans.release.pinned_deploy_sha`,
:func:`record_confirm`). HTTP and the process table are faked."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from precis import settings as psettings
from precis.plans import release as R
from precis.plans import templates as T
from precis.store import Store
from precis.utils.safe_fetch import SsrfBlocked
from precis.workers.job_types import release_confirm as rcf
from precis_web.bootsha import BOOT_SHA_HEADER

pytestmark = pytest.mark.db

SHA = "c0ffee" + "1" * 34
SHA2 = "beef" + "2" * 36
RNG = f"{'9' * 12}..{SHA[:12]}"
NOW = datetime(2026, 11, 6, 12, 0, 0, tzinfo=UTC)
HEALTH_URL = "https://health.example.invalid/healthz"
HEALTH_HOST = "health.example.invalid"

REL = {"rN": "r7", "sha": SHA, "range": RNG}


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRECIS_RELEASE_HEALTH_URL", raising=False)
    psettings.invalidate()


def _past_window() -> datetime:
    """A clock past the restart-race window of a deploy recorded just now
    (``record_prod_head`` stamps the real clock): a worker still at
    another sha is a problem, not still rolling."""
    return datetime.now(UTC) + rcf._CONFIRM_WINDOW + timedelta(minutes=1)


def _beat(store: Store, host: str, code_sha: str | None, **meta: Any) -> None:
    store.record_heartbeat(host, meta={"code_sha": code_sha, **meta})


def _units(process: str = "precis-worker") -> dict[str, Any]:
    """A ``worker_units`` snapshot observing one installed unit."""
    return {
        "observed_at": datetime.now(UTC).isoformat(),
        "units": {f"{process}.service": {"active_state": "active"}},
    }


def _ledger(store: Store, covered: list[str]) -> None:
    """Publish ``app_settings.deploy.fleet`` as ``scripts/deploy`` does."""
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO app_settings (key, value) VALUES ('deploy.fleet', %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (json.dumps({"sha": SHA, "covered": covered}),),
        )
        conn.commit()


def _stale(store: Store, host: str, minutes: int = 90) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE host_heartbeat SET ts = now() - make_interval(mins => %s) "
            "WHERE host = %s",
            (minutes, host),
        )


def _web(
    monkeypatch: pytest.MonkeyPatch,
    resp: Any = None,
    exc: Exception | None = None,
) -> None:
    def get(url: str) -> Any:
        assert url == HEALTH_URL
        if exc is not None:
            raise exc
        return resp

    monkeypatch.setattr(rcf, "_http_get", get)


def _ok_resp(sha: str | None) -> Any:
    headers = {} if sha is None else {BOOT_SHA_HEADER: sha}
    return SimpleNamespace(status_code=200, headers=headers)


def test_workers_all_at_the_pinned_sha_passes(store: Store) -> None:
    R.record_prod_head(store, SHA, [])
    _beat(store, "node-a", SHA)
    _beat(store, "node-b", SHA)
    ev = rcf.gather_evidence(store, REL, now=NOW)
    assert ev.ok and ev.sha == SHA and ev.problems == []
    assert ev.text == (
        f"r7 pinned {SHA[:12]}; workers: 2 live (node-a, node-b), "
        f"all at {SHA[:12]}; web: not attested (release.health_url unset)"
    )


def test_a_worker_behind_names_host_and_sha(store: Store) -> None:
    R.record_prod_head(store, SHA, [])
    _beat(store, "node-a", SHA)
    _beat(store, "node-b", SHA2)
    ev = rcf.gather_evidence(store, REL, now=_past_window())
    assert not ev.ok
    assert ev.problems == [f"worker node-b reports {SHA2[:12]}, release is {SHA[:12]}"]
    assert "node-b" in rcf.failure_reason(ev) and SHA2[:12] in rcf.failure_reason(ev)


def test_a_worker_with_no_code_sha_is_a_problem(store: Store) -> None:
    R.record_prod_head(store, SHA, [])
    _beat(store, "node-a", SHA)
    _beat(store, "node-b", None)
    _beat(store, "node-c", "unknown")
    ev = rcf.gather_evidence(store, REL, now=_past_window())
    assert not ev.ok
    assert ev.problems == [
        f"worker node-b reports no code_sha, release is {SHA[:12]}",
        f"worker node-c reports unknown, release is {SHA[:12]}",
    ]


def test_a_stale_heartbeat_is_ignored(store: Store) -> None:
    R.record_prod_head(store, SHA, [])
    _beat(store, "node-a", SHA)
    _beat(store, "node-z", SHA2)
    _stale(store, "node-z")
    ev = rcf.gather_evidence(store, REL, now=NOW)
    assert ev.ok, ev.problems
    assert "node-z" not in ev.text


def test_no_live_host_is_a_problem(store: Store) -> None:
    R.record_prod_head(store, SHA, [])
    _beat(store, "node-a", SHA)
    _stale(store, "node-a")
    ev = rcf.gather_evidence(store, REL, now=NOW)
    assert not ev.ok
    assert ev.problems == ["no live worker host (no heartbeat in the last 60 min)"]


def test_no_pinned_sha_is_a_problem(store: Store) -> None:
    _beat(store, "node-a", SHA)
    ev = rcf.gather_evidence(store, REL, now=NOW)
    assert not ev.ok and ev.sha is None
    assert ev.problems[-1].startswith("no pinned deploy sha")


def test_a_pinned_sha_other_than_the_plan_release_is_a_problem(
    store: Store,
) -> None:
    """Prod is on SHA2 but the plan cut SHA: the pin mismatch is the first
    problem (the worker's sha matches prod, so nothing else fires)."""
    R.record_prod_head(store, SHA2, [])
    _beat(store, "node-a", SHA2)
    ev = rcf.gather_evidence(store, REL, now=NOW)
    assert not ev.ok
    assert ev.problems == [
        f"pinned deploy sha {SHA2[:12]} is not the plan's release sha {SHA[:12]}"
    ]


# ── worker-less hosts (gr623769) ────────────────────────────────────


def test_a_workerless_checkout_is_a_note_not_a_failure(store: Store) -> None:
    """gr623769: a one-off heartbeat from a machine running a precis
    checkout — empty ``worker_units``, no ``activity``, no deploy-ledger
    membership — is a note, not the attestor that pins the pass."""
    R.record_prod_head(store, SHA, [])
    _beat(store, "node-a", SHA, worker_units=_units())
    _beat(store, "hephaestus", SHA2, worker_units={}, activity={})
    ev = rcf.gather_evidence(store, REL, now=NOW)
    assert ev.ok and ev.problems == [] and ev.noncohort == ("hephaestus",)
    assert "hephaestus (no workers)" in ev.text


def test_worker_units_snapshot_with_units_attests(store: Store) -> None:
    """A host whose snapshot observes installed precis-worker units runs
    workers — its stale sha is a problem, not a note."""
    R.record_prod_head(store, SHA, [])
    _beat(store, "node-a", SHA, worker_units=_units())
    _beat(store, "node-b", SHA2, worker_units=_units())
    ev = rcf.gather_evidence(store, REL, now=_past_window())
    assert not ev.ok
    assert ev.problems == [f"worker node-b reports {SHA2[:12]}, release is {SHA[:12]}"]


def test_empty_worker_units_with_activity_is_a_fleet_member(store: Store) -> None:
    """Complement of the dev-checkout skip (gr625282's shape): a launchd
    host reports no units but real worker ``activity`` — still fleet."""
    R.record_prod_head(store, SHA, [])
    _beat(store, "node-a", SHA, worker_units=_units())
    _beat(
        store,
        "node-mac",
        SHA2,
        worker_units={},
        activity={"precis-worker": {"idle": True}},
    )
    ev = rcf.gather_evidence(store, REL, now=_past_window())
    assert not ev.ok and ev.noncohort == ()
    assert ev.problems == [
        f"worker node-mac reports {SHA2[:12]}, release is {SHA[:12]}"
    ]


def test_a_workerless_host_in_the_deploy_ledger_is_a_member(store: Store) -> None:
    """gr623769's other arm: ``covered`` in ``app_settings.deploy.fleet``
    is a known fleet member even with empty worker evidence — a covered
    host that stopped running workers must still attest."""
    R.record_prod_head(store, SHA, [])
    _beat(store, "node-a", SHA, worker_units=_units())
    _beat(store, "hephaestus", SHA2, worker_units={}, activity={})
    _ledger(store, ["node-a", "hephaestus"])
    ev = rcf.gather_evidence(store, REL, now=_past_window())
    assert not ev.ok and ev.noncohort == ()
    assert ev.problems == [
        f"worker hephaestus reports {SHA2[:12]}, release is {SHA[:12]}"
    ]


def test_a_workerless_host_never_parks_the_probe(store: Store) -> None:
    """A worker-less host whose beat predates the deploy finish is not
    'not yet restarted' — it attests nothing, so the probe does not wait
    on it."""
    R.record_prod_head(store, SHA, [])
    rollout = (datetime.now(UTC) - timedelta(minutes=2)).strftime(rcf._TS_FMT)
    psettings.set_setting(
        "release.round",
        _round_row(7, "deployed", {"sha": SHA, "rollout_at": rollout}),
        store=store,
    )
    _beat(store, "node-a", SHA, worker_units=_units())
    _beat(store, "hephaestus", SHA2, worker_units={}, activity={})
    _stale(store, "hephaestus", minutes=10)  # predates the deploy finish
    ev = rcf.gather_evidence(store, REL, now=datetime.now(UTC))
    assert ev.ok and ev.waiting == () and ev.noncohort == ("hephaestus",)


def test_only_workerless_hosts_is_still_no_live_worker(store: Store) -> None:
    """Every live row worker-less → nothing attests: still 'no live
    worker host', and the note still names them."""
    R.record_prod_head(store, SHA, [])
    _beat(store, "hephaestus", SHA, worker_units={}, activity={})
    ev = rcf.gather_evidence(store, REL, now=NOW)
    assert not ev.ok and ev.noncohort == ("hephaestus",)
    assert ev.problems == ["no live worker host (no heartbeat in the last 60 min)"]
    assert "hephaestus (no workers)" in ev.text


def test_an_uncovered_host_running_workers_never_attests(store: Store) -> None:
    """td649547: once ``deploy.fleet`` publishes its ``covered`` list,
    only those configured members attest — installed units and recorded
    activity on an uncovered host are conditions' ``deploy-uncovered``
    finding, not release evidence."""
    R.record_prod_head(store, SHA, [])
    _beat(store, "node-a", SHA, worker_units=_units())
    _beat(store, "node-b", SHA2, worker_units=_units())
    _ledger(store, ["node-a"])
    ev = rcf.gather_evidence(store, REL, now=NOW)
    assert ev.ok and ev.problems == []
    assert ev.noncohort == ("node-b",)
    assert "node-b (not in deploy.fleet)" in ev.text


def test_an_uncovered_host_with_no_snapshot_never_attests(store: Store) -> None:
    """The absent ``worker_units`` key (pre-snapshot shape) and
    historical ``activity`` keys admit nothing either once membership
    is configured."""
    R.record_prod_head(store, SHA, [])
    _beat(store, "node-a", SHA, worker_units=_units())
    _beat(
        store,
        "node-b",
        SHA2,
        activity={"precis-worker": {"idle": True}},
    )
    _ledger(store, ["node-a"])
    ev = rcf.gather_evidence(store, REL, now=NOW)
    assert ev.ok and ev.problems == []
    assert ev.noncohort == ("node-b",)
    assert "node-b (not in deploy.fleet)" in ev.text


# ── the web clause ──────────────────────────────────────────────────


def test_web_unset_passes_with_the_not_attested_line(store: Store) -> None:
    R.record_prod_head(store, SHA, [])
    _beat(store, "node-a", SHA)
    ev = rcf.gather_evidence(store, REL, now=NOW)
    assert ev.ok
    assert ev.text.endswith("web: not attested (release.health_url unset)")


def test_web_at_the_pinned_sha_passes(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    R.record_prod_head(store, SHA, [])
    _beat(store, "node-a", SHA)
    psettings.set_setting(rcf.HEALTH_URL_KEY, HEALTH_URL, store=store)
    _web(monkeypatch, resp=_ok_resp(SHA))
    ev = rcf.gather_evidence(store, REL, now=NOW)
    assert ev.ok and ev.text.endswith(f"web: at {SHA[:12]}")


def test_web_another_sha_fails(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    R.record_prod_head(store, SHA, [])
    _beat(store, "node-a", SHA)
    psettings.set_setting(rcf.HEALTH_URL_KEY, HEALTH_URL, store=store)
    _web(monkeypatch, resp=_ok_resp(SHA2))
    ev = rcf.gather_evidence(store, REL, now=NOW)
    assert not ev.ok
    assert ev.problems == [f"web reports {SHA2[:12]}, release is {SHA[:12]}"]


def test_web_without_the_header_fails(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    R.record_prod_head(store, SHA, [])
    _beat(store, "node-a", SHA)
    psettings.set_setting(rcf.HEALTH_URL_KEY, HEALTH_URL, store=store)
    _web(monkeypatch, resp=_ok_resp(None))
    ev = rcf.gather_evidence(store, REL, now=NOW)
    assert not ev.ok
    assert ev.problems == [f"web reports no sha, release is {SHA[:12]}"]


def test_web_non_200_fails_with_the_status_only(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    R.record_prod_head(store, SHA, [])
    _beat(store, "node-a", SHA)
    psettings.set_setting(rcf.HEALTH_URL_KEY, HEALTH_URL, store=store)
    _web(monkeypatch, resp=SimpleNamespace(status_code=503, headers={}))
    ev = rcf.gather_evidence(store, REL, now=NOW)
    assert not ev.ok and ev.problems == ["web HTTP 503"]


@pytest.mark.parametrize(
    "exc",
    [RuntimeError("cannot resolve health.example.invalid"), SsrfBlocked("blocked")],
    ids=["raise", "ssrf"],
)
def test_web_raise_fails_with_the_class_name_only(
    store: Store, monkeypatch: pytest.MonkeyPatch, exc: Exception
) -> None:
    R.record_prod_head(store, SHA, [])
    _beat(store, "node-a", SHA)
    psettings.set_setting(rcf.HEALTH_URL_KEY, HEALTH_URL, store=store)
    _web(monkeypatch, exc=exc)
    ev = rcf.gather_evidence(store, REL, now=NOW)
    assert not ev.ok
    assert ev.problems == [f"web unreachable ({type(exc).__name__})"]


def test_no_web_failure_names_the_url_or_host(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The URL and its host reach no problem, evidence text or failure
    reason — httpx errors carry them, so only the class name reports."""
    R.record_prod_head(store, SHA, [])
    _beat(store, "node-a", SHA)
    psettings.set_setting(rcf.HEALTH_URL_KEY, HEALTH_URL, store=store)
    _web(monkeypatch, exc=RuntimeError(f"dial tcp {HEALTH_URL}: refused"))
    ev = rcf.gather_evidence(store, REL, now=NOW)
    assert not ev.ok
    blob = "; ".join(ev.problems) + ev.text + rcf.failure_reason(ev)
    assert HEALTH_URL not in blob and HEALTH_HOST not in blob


def test_http_get_is_a_plain_bounded_get(monkeypatch: pytest.MonkeyPatch) -> None:
    """gr607271: the operator-set health URL is fetched by a bare bounded
    ``httpx`` GET — the SSRF pinning transport and ``polite_safe_get``
    must not be built (they refuse the fleet's own address)."""
    import httpx

    from precis.utils import http as _http
    from precis.utils import safe_fetch

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("the SSRF guard must not run for release.health_url")

    monkeypatch.setattr(safe_fetch, "pinning_transport", _boom)
    monkeypatch.setattr(_http, "polite_safe_get", _boom)
    seen: dict[str, Any] = {}

    class _Client:
        def __init__(self, **kw: Any) -> None:
            seen.update(kw)

        def __enter__(self) -> Any:
            return self

        def __exit__(self, *_a: Any) -> None:
            return None

        def get(self, url: str) -> Any:
            seen["url"] = url
            return SimpleNamespace(status_code=200, headers={})

    monkeypatch.setattr(httpx, "Client", _Client)
    resp = rcf._http_get(HEALTH_URL)
    assert resp.status_code == 200 and seen["url"] == HEALTH_URL
    assert seen["follow_redirects"] is False and seen["timeout"]


def test_a_web_only_failure_marks_the_evidence(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr607271 (c): every worker attesting and only the web probe red
    marks the evidence ``web_only`` — the job then files the ask itself;
    any second problem keeps it a plain failure."""
    R.record_prod_head(store, SHA, [])
    _beat(store, "node-a", SHA)
    psettings.set_setting(rcf.HEALTH_URL_KEY, HEALTH_URL, store=store)
    _web(monkeypatch, resp=SimpleNamespace(status_code=503, headers={}))
    ev = rcf.gather_evidence(store, REL, now=_past_window())
    assert not ev.ok and ev.web_only and ev.problems == ["web HTTP 503"]

    _beat(store, "node-b", SHA2)
    ev = rcf.gather_evidence(store, REL, now=_past_window())
    assert not ev.ok and not ev.web_only and len(ev.problems) == 2


def test_a_web_failure_while_a_worker_waits_is_not_web_only(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """td609348: inside the restart window a waiting worker has NOT
    attested — its heartbeat predates the deploy finish — so a failed
    web probe is a plain failure, never the ``workers all at`` ask."""
    R.record_prod_head(store, SHA, [])
    rollout = (datetime.now(UTC) - timedelta(minutes=2)).strftime(rcf._TS_FMT)
    psettings.set_setting(
        "release.round",
        _round_row(7, "deployed", {"sha": SHA, "rollout_at": rollout}),
        store=store,
    )
    _beat(store, "node-a", SHA2)
    _stale(store, "node-a", minutes=10)  # the beat predates the deploy finish
    _beat(store, "node-b", SHA)  # fresh beat at the sha: attested
    psettings.set_setting(rcf.HEALTH_URL_KEY, HEALTH_URL, store=store)
    _web(monkeypatch, resp=SimpleNamespace(status_code=503, headers={}))
    ev = rcf.gather_evidence(store, REL, now=datetime.now(UTC))
    assert not ev.ok and not ev.web_only
    assert ev.waiting == ("node-a",)
    assert ev.problems == ["web HTTP 503"]


# ── the worker roll and the unbounced range (gr645561) ──────────────


def test_a_worker_still_rolling_waits_then_passes(store: Store) -> None:
    """A worker heartbeating the old sha AFTER the deploy finished is
    still rolling: inside the window it parks the probe (no problem);
    the re-probe finds it at the release sha and passes."""
    R.record_prod_head(store, SHA, [])
    _beat(store, "node-a", SHA)
    _beat(store, "node-b", SHA2)
    ev = rcf.gather_evidence(store, REL, now=datetime.now(UTC))
    assert not ev.ok and ev.problems == []
    assert ev.waiting == ("node-b",) and ev.deadline is not None
    assert f"node-b at {SHA2[:12]}, still rolling" in ev.text

    _beat(store, "node-b", SHA)  # the restart landed
    ev = rcf.gather_evidence(store, REL, now=datetime.now(UTC))
    assert ev.ok and ev.waiting == () and ev.unbounced == ()


def test_a_runtime_range_with_a_stale_worker_fails_after_the_window(
    store: Store,
) -> None:
    """The window is bounded: past it a worker still at the old sha is a
    problem naming host and sha, a recorded ``full`` scope or none at all
    never excuses it, and the wait ends at the window's end."""
    R.record_prod_head(store, SHA, [])
    _beat(store, "node-a", SHA)
    _beat(store, "node-b", SHA2)
    rel = {**REL, "deploy_scope": {"scope": "full", "prev": SHA2}}
    for release in (REL, rel):
        inside = rcf.gather_evidence(store, release, now=datetime.now(UTC))
        assert inside.waiting == ("node-b",) and inside.problems == []
        ev = rcf.gather_evidence(store, release, now=_past_window())
        assert not ev.ok and ev.waiting == ()
        assert ev.problems == [
            f"worker node-b reports {SHA2[:12]}, release is {SHA[:12]}"
        ]


@pytest.mark.parametrize("scope", ["none", "web"])
def test_an_unbounced_range_accepts_the_previous_sha(store: Store, scope: str) -> None:
    """The bounce scope restarted no worker: a worker still at the
    previous prod sha is confirmed at once (no waiting) and the evidence
    line says so."""
    R.record_prod_head(store, SHA, [])
    _beat(store, "node-a", SHA2)  # never restarted, still on the previous sha
    _beat(store, "node-b", SHA)
    rel = {**REL, "deploy_scope": {"scope": scope, "prev": SHA2}}
    ev = rcf.gather_evidence(store, rel, now=datetime.now(UTC))
    assert ev.ok and ev.problems == [] and ev.waiting == ()
    assert ev.unbounced == ("node-a",)
    assert f"unbounced: node-a still at the previous prod sha {SHA2[:12]}" in ev.text
    assert f"node-a at {SHA2[:12]} (not bounced)" in ev.text
    assert "all at" not in ev.text


def test_an_unbounced_range_still_holds_other_shas_to_the_release(
    store: Store,
) -> None:
    """Only the previous prod sha is excused: a worker at a third sha
    waits out the window and then fails, and a short prev attests
    nothing."""
    R.record_prod_head(store, SHA, [])
    third = "dead" + "3" * 36
    _beat(store, "node-a", third)
    rel = {**REL, "deploy_scope": {"scope": "none", "prev": SHA2}}
    ev = rcf.gather_evidence(store, rel, now=datetime.now(UTC))
    assert not ev.ok and ev.waiting == ("node-a",) and ev.unbounced == ()
    ev = rcf.gather_evidence(store, rel, now=_past_window())
    assert ev.problems == [f"worker node-a reports {third[:12]}, release is {SHA[:12]}"]
    short = {**REL, "deploy_scope": {"scope": "none", "prev": "beef"}}
    assert rcf.unbounced_prev_sha(short) is None
    assert rcf.unbounced_prev_sha(REL) is None


def test_an_unbounced_worker_with_a_pre_deploy_heartbeat_is_accepted(
    store: Store,
) -> None:
    """A worker that never restarts also never refreshes past the deploy
    finish if its beat is old: the unbounced scope accepts it before the
    not-yet-restarted check."""
    R.record_prod_head(store, SHA, [])
    _beat(store, "node-a", SHA2)
    _stale(store, "node-a", minutes=10)  # beat predates the deploy finish
    rel = {**REL, "deploy_scope": {"scope": "none", "prev": SHA2}}
    ev = rcf.gather_evidence(store, rel, now=datetime.now(UTC))
    assert ev.ok and ev.unbounced == ("node-a",)


# ── pinned_deploy_sha ───────────────────────────────────────────────


def _round_row(n: int, state: str, deploy: dict[str, Any] | None = None) -> str:
    row: dict[str, Any] = {
        "n": n,
        "branch": f"release/r{n}",
        "base": "b" * 40,
        "state": state,
        "owner": {"peer": "main", "host": "node-a"},
    }
    if deploy:
        row["deploy"] = deploy
    return json.dumps(row)


def test_pinned_deploy_sha_prefers_the_round_row(store: Store) -> None:
    R.record_prod_head(store, SHA2, [])
    psettings.set_setting(
        "release.round", _round_row(7, "deployed", {"sha": SHA}), store=store
    )
    assert R.pinned_deploy_sha(store, "r7") == SHA


def test_pinned_deploy_sha_falls_back_to_the_prod_head(store: Store) -> None:
    R.record_prod_head(store, SHA, [])
    # A row for another release does not pin this one.
    psettings.set_setting(
        "release.round", _round_row(8, "deployed", {"sha": SHA2}), store=store
    )
    assert R.pinned_deploy_sha(store, "r7") == SHA
    assert R.pinned_deploy_sha(store, "r9") == SHA  # no row for r9: prod head
    psettings.clear_setting("release.round", store=store)
    assert R.pinned_deploy_sha(store, "r7") == SHA


def test_pinned_deploy_sha_none_with_nothing_recorded(store: Store) -> None:
    assert R.pinned_deploy_sha(store, "r7") is None


# ── record_confirm ──────────────────────────────────────────────────


def _cut(store: Store) -> T.PlanMint:
    mint = T.mint_release_pass(store)
    R.record_cut(store, mint.ids["cut"], "r7", SHA, RNG, repo="o/r")
    return mint


def _rel(store: Store, root: int) -> dict[str, Any]:
    ref = store.get_ref(kind="todo", id=root)
    assert ref is not None
    return dict(ref.meta["release"])


def test_record_confirm_fills_absent_keys_and_stamps_the_root(
    store: Store,
) -> None:
    mint = _cut(store)
    psettings.set_setting(
        "release.round",
        _round_row(7, "deployed", {"sha": SHA, "rollout_at": "2026-11-06T10:00:00Z"}),
        store=store,
    )
    notes = R.record_confirm(store, mint.ids["cut"], SHA, "all at sha", now=NOW)
    row = R.read_round(store)
    assert row is not None
    assert row["state"] == "confirmed"
    assert row["deploy"]["rollout_at"] == "2026-11-06T10:00:00Z"
    assert row["deploy"]["confirmed_at"] == "2026-11-06T12:00:00Z"
    assert row["deploy"]["evidence"] == "all at sha"
    rel = _rel(store, mint.root_id)
    assert rel["confirmed"] == {
        "sha": SHA,
        "at": "2026-11-06T12:00:00Z",
        "evidence": "all at sha",
    }
    assert rel["rN"] == "r7" and rel["range"] == RNG
    assert len(notes) == 2

    # Idempotent: a second call writes nothing and returns [].
    assert R.record_confirm(store, mint.ids["cut"], SHA, "all at sha", now=NOW) == []
    row2 = R.read_round(store)
    assert row2 == row


def test_record_confirm_never_overwrites_the_scripts_values(
    store: Store,
) -> None:
    mint = _cut(store)
    psettings.set_setting(
        "release.round",
        _round_row(
            7,
            "confirmed",
            {
                "sha": SHA,
                "confirmed_at": "2026-11-06T11:00:00Z",
                "evidence": "the script's evidence",
            },
        ),
        store=store,
    )
    notes = R.record_confirm(store, mint.ids["cut"], SHA, "the job's evidence", now=NOW)
    row = R.read_round(store)
    assert row is not None
    assert row["deploy"]["confirmed_at"] == "2026-11-06T11:00:00Z"
    assert row["deploy"]["evidence"] == "the script's evidence"
    assert row["state"] == "confirmed"
    # The root stamp still lands: the script never writes meta.release.
    assert _rel(store, mint.root_id)["confirmed"]["evidence"] == "the job's evidence"
    assert len(notes) == 1
    assert R.record_confirm(store, mint.ids["cut"], SHA, "again", now=NOW) == []


@pytest.mark.parametrize(
    ("row", "why"),
    [
        (_round_row(8, "deployed", {"sha": SHA}), "row is r8"),
        (_round_row(7, "deployed", {"sha": SHA2}), "deploy.sha"),
        (_round_row(7, "deployed"), "deploy.sha"),
    ],
    ids=["other-n", "other-sha", "no-deploy"],
)
def test_record_confirm_skips_a_row_that_is_not_this_deploy(
    store: Store, row: str, why: str
) -> None:
    mint = _cut(store)
    psettings.set_setting("release.round", row, store=store)
    notes = R.record_confirm(store, mint.ids["cut"], SHA, "ev", now=NOW)
    assert any(why in n for n in notes), notes
    assert R.read_round(store) == json.loads(row)
    # The root stamp is unaffected by the row skip.
    assert _rel(store, mint.root_id)["confirmed"]["sha"] == SHA


def test_record_confirm_never_creates_a_row(store: Store) -> None:
    mint = _cut(store)
    notes = R.record_confirm(store, mint.ids["cut"], SHA, "ev", now=NOW)
    assert R.read_round(store) is None
    assert any("no row" in n for n in notes)
    assert _rel(store, mint.root_id)["confirmed"]["sha"] == SHA


def test_record_confirm_leaves_a_terminal_state_alone(store: Store) -> None:
    mint = _cut(store)
    psettings.set_setting(
        "release.round",
        _round_row(7, "superseded", {"sha": SHA}),
        store=store,
    )
    R.record_confirm(store, mint.ids["cut"], SHA, "ev", now=NOW)
    row = R.read_round(store)
    assert row is not None and row["state"] == "superseded"
    assert row["deploy"]["confirmed_at"] == "2026-11-06T12:00:00Z"


# ── literal pins and caps ───────────────────────────────────────────


def test_boot_sha_header_matches_the_web_literal() -> None:
    assert rcf.BOOT_SHA_HEADER == BOOT_SHA_HEADER == "X-Precis-Boot-Sha"


def test_health_url_is_a_registered_setting() -> None:
    spec = psettings.REGISTRY["release.health_url"]
    assert spec.env_var == "PRECIS_RELEASE_HEALTH_URL" and spec.default is None


def test_text_is_masked_and_capped(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    R.record_prod_head(store, SHA, [])
    for i in range(40):
        _beat(store, f"node-{i:02d}x", SHA)
    token = "ghp_" + "A1b2C3d4" * 4 + "E5f6"  # 36 alnum after ghp_
    psettings.set_setting(rcf.HEALTH_URL_KEY, HEALTH_URL, store=store)

    def get(url: str) -> Any:
        return SimpleNamespace(status_code=200, headers={BOOT_SHA_HEADER: token})

    monkeypatch.setattr(rcf, "_http_get", get)
    ev = rcf.gather_evidence(store, REL, now=NOW)
    assert not ev.ok
    assert len(ev.text) <= 900
    assert token not in ev.text and token not in rcf.failure_reason(ev)


def test_failure_reason_is_capped_at_560(store: Store) -> None:
    R.record_prod_head(store, SHA, [])
    for i in range(40):
        _beat(store, f"node-{i:02d}x", SHA2)
    ev = rcf.gather_evidence(store, REL, now=_past_window())
    assert not ev.ok
    assert len(rcf.failure_reason(ev)) <= 560
    assert "node-00x" in rcf.failure_reason(ev)
