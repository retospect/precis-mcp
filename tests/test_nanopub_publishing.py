"""Durable ``publishing`` state: real PG, mocked registry POST, no keys/network.

The POST must run with no pooled connection or open transaction held; a
concurrent second publish loses a CAS; a definite failure reverts; an
ambiguous one leaves ``publishing`` for ``reconcile_publishing``.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from typing import Any

import psycopg
import pytest

from precis.errors import BadInput
from precis.nanopub import registry
from precis.nanopub.demote import ACTION_SUPERSEDE_REQUIRED, plan_demotion
from precis.nanopub.supersede import supersede
from tests.test_nanopub_supersede import _old


class _Resp:
    def __init__(self, status_code: int, content: bytes = b"") -> None:
        self.status_code = status_code
        self.content = content


class _HttpError(Exception):
    """Stands in for httpx.HTTPStatusError (carries ``.response``)."""

    def __init__(self, status_code: int, content: bytes = b"nope") -> None:
        super().__init__(f"HTTP {status_code}")
        self.response = _Resp(status_code, content)


@pytest.fixture(autouse=True)
def _no_preflight(monkeypatch: Any) -> None:
    # Preflight (trust allowlist, drift) is covered in test_nanopub_preflight;
    # these rows carry synthetic artifacts and no allowlisted key.
    monkeypatch.setattr(registry, "publish_preflight", lambda *a, **k: [])


def _live(store: Any, hub: int, post: Any) -> Any:
    return registry.publish(store, hub, live=True, interactive=True, post=post)


def _fresh_state(store: Any, row_id: int) -> tuple[Any, ...]:
    with psycopg.connect(store.pool.conninfo) as conn:
        row = conn.execute(
            "SELECT state, publishing_since, registry_url, published_at "
            "FROM nanopub_publish WHERE id = %s",
            (row_id,),
        ).fetchone()
    assert row is not None
    return row


def _held_connections(store: Any) -> int:
    stats = store.pool.get_stats()
    return stats["pool_size"] - stats["pool_available"]


def _idle_in_txn(store: Any) -> int:
    with psycopg.connect(store.pool.conninfo) as conn:
        row = conn.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = "
            "current_database() AND state LIKE 'idle in transaction%%' "
            "AND pid <> pg_backend_pid()"
        ).fetchone()
    assert row is not None
    return row[0]


def test_success_claims_then_publishes_without_holding_a_connection(
    store: Any,
) -> None:
    old = _old(store)
    seen: dict[str, Any] = {}

    def post(url: str, body: bytes) -> None:
        # Visible to an independent connection => the claim is committed.
        seen["row"] = _fresh_state(store, old.id)
        seen["held"] = _held_connections(store)
        seen["idle_txn"] = _idle_in_txn(store)
        seen["body"] = body

    result = _live(store, old.claim_ref_id, post)
    assert result.live
    state, since, url, published_at = seen["row"]
    assert state == "publishing" and since is not None
    assert url == registry.DEFAULT_REGISTRY_URL and published_at is None
    assert seen["held"] == 0, "a pooled connection was held across the POST"
    assert seen["idle_txn"] == 0, "a transaction idled across the POST"
    state, since, url, published_at = _fresh_state(store, old.id)
    assert state == "published" and since is None and published_at is not None


def test_concurrent_second_publish_loses_cleanly(store: Any) -> None:
    old = _old(store)
    posts: list[bytes] = []
    inner: dict[str, Any] = {}

    def post(url: str, body: bytes) -> None:
        posts.append(body)
        # Second publisher arrives while the first POST is in flight.
        try:
            _live(store, old.claim_ref_id, lambda u, b: posts.append(b))
        except BadInput as exc:
            inner["exc"] = exc

    _live(store, old.claim_ref_id, post)
    assert len(posts) == 1, "the loser must not POST"
    assert "publishing" in str(inner["exc"])
    assert _fresh_state(store, old.id)[0] == "published"


def test_claim_cas_has_exactly_one_winner(store: Any) -> None:
    old = _old(store)
    barrier = Barrier(2)

    def go() -> bool:
        barrier.wait()
        return store.nanopub_claim_publishing(
            old.id,
            artifact_id=old.artifact_id,
            trusty_uri=old.trusty_uri,
            registry_url="test:registry",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [f.result(timeout=15) for f in [pool.submit(go), pool.submit(go)]]
    assert sorted(results) == [False, True]


@pytest.mark.parametrize(
    "exc",
    [_HttpError(400), _HttpError(422, b"bad trig"), _HttpError(404, b"")],
)
def test_definite_failure_reverts_to_anchored(store: Any, exc: Exception) -> None:
    old = _old(store)

    def post(url: str, body: bytes) -> None:
        raise exc

    with pytest.raises(_HttpError):
        _live(store, old.claim_ref_id, post)
    state, since, url, published_at = _fresh_state(store, old.id)
    assert (state, since, url, published_at) == ("anchored", None, None, None)
    # Retryable: a later publish goes through.
    _live(store, old.claim_ref_id, lambda u, b: None)
    assert _fresh_state(store, old.id)[0] == "published"


@pytest.mark.parametrize(
    "exc",
    [
        TimeoutError("read timed out"),
        ConnectionError("reset"),
        _HttpError(502, b""),
        _HttpError(503, b"overloaded"),
        _HttpError(408, b""),
        _HttpError(429, b"slow down"),
    ],
)
def test_ambiguous_outcome_leaves_publishing(store: Any, exc: Exception) -> None:
    old = _old(store)

    def post(url: str, body: bytes) -> None:
        raise exc

    with pytest.raises(BadInput, match="reconcile"):
        _live(store, old.claim_ref_id, post)
    state, since, url, _ = _fresh_state(store, old.id)
    assert state == "publishing" and since is not None and url


def test_change_between_preflight_and_claim_is_caught(
    store: Any, monkeypatch: Any
) -> None:
    from precis.nanopub.preflight import PreflightIssue

    old = _old(store)
    calls: list[str] = []

    def preflight(store_: Any, hub: int, *, row: Any) -> list[Any]:
        calls.append(row.state)
        # First look is clean; by the post-claim look a signoff has changed.
        return [] if len(calls) == 1 else [PreflightIssue("signoff", "withdrawn")]

    monkeypatch.setattr(registry, "publish_preflight", preflight)

    def post(url: str, body: bytes) -> None:
        raise AssertionError("must not POST after a failed re-check")

    with pytest.raises(registry.PublishBlocked, match="withdrawn"):
        _live(store, old.claim_ref_id, post)
    assert calls == ["anchored", "anchored"]  # re-check judged as anchored
    assert _fresh_state(store, old.id) == ("anchored", None, None, None)


def _stuck(store: Any) -> Any:
    old = _old(store)

    def post(url: str, body: bytes) -> None:
        raise TimeoutError("read timed out")

    with pytest.raises(BadInput):
        _live(store, old.claim_ref_id, post)
    return old


def test_reconcile_present_resolves_to_published(store: Any) -> None:
    old = _stuck(store)
    urls: list[str] = []

    def fetch(url: str) -> bytes:
        urls.append(url)
        return b"@prefix np: <http://www.nanopub.org/nschema#> ."

    out = registry.reconcile_publishing(store, older_than=timedelta(0), fetch=fetch)
    assert [(r.row_id, r.outcome) for r in out] == [(old.id, "published")]
    assert urls == [
        f"{registry.DEFAULT_REGISTRY_URL}{old.trusty_uri.rsplit('/', 1)[-1]}.trig"
    ]
    state, since, _, published_at = _fresh_state(store, old.id)
    assert state == "published" and since is None and published_at is not None


def test_reconcile_absent_reverts_to_anchored(store: Any) -> None:
    old = _stuck(store)

    def fetch(url: str) -> bytes:
        raise _HttpError(404)

    out = registry.reconcile_publishing(store, older_than=timedelta(0), fetch=fetch)
    assert [r.outcome for r in out] == ["reverted"]
    assert _fresh_state(store, old.id) == ("anchored", None, None, None)


def test_reconcile_unreachable_or_young_rows_are_untouched(store: Any) -> None:
    old = _stuck(store)

    def down(url: str) -> bytes:
        raise TimeoutError("registry down")

    out = registry.reconcile_publishing(store, older_than=timedelta(0), fetch=down)
    assert [r.outcome for r in out] == ["unknown"]
    assert _fresh_state(store, old.id)[0] == "publishing"

    def boom(url: str) -> bytes:  # must not even be consulted
        raise AssertionError("row is younger than older_than")

    assert (
        registry.reconcile_publishing(store, older_than=timedelta(hours=1), fetch=boom)
        == []
    )
    html = lambda url: b"<!DOCTYPE html><html></html>"
    out = registry.reconcile_publishing(store, older_than=timedelta(0), fetch=html)
    assert [r.outcome for r in out] == ["unknown"]
    assert _fresh_state(store, old.id)[0] == "publishing"


def test_other_doors_refuse_a_publishing_row(store: Any) -> None:
    old = _stuck(store)
    before = _fresh_state(store, old.id)

    with pytest.raises(BadInput, match="publishing"):
        store.nanopub_supersede(old.id)
    with pytest.raises(BadInput, match="publishing"):
        supersede(store, old.claim_ref_id, interactive=True)
    with pytest.raises(BadInput, match="publishing"):
        store.nanopub_create_publish_row(old.claim_ref_id)
    assert store.nanopub_reopen(old.id) is False  # discard/restage/demote path
    assert store.nanopub_discard_candidate(old.id) is False
    assert store.nanopub_set_batch(old.id, 1) is False
    assert store.nanopub_reopen_stuck_batch(old.batch_id) == 0
    with pytest.raises(BadInput):
        store.nanopub_transition(old.id, to_state="anchored", expect=("publishing",))
    with pytest.raises(BadInput):
        store.nanopub_transition(old.id, to_state="publishing", expect=("anchored",))
    assert plan_demotion("publishing") == ACTION_SUPERSEDE_REQUIRED

    assert _fresh_state(store, old.id) == before
    # No successor row or obligation leaked.
    with psycopg.connect(store.pool.conninfo) as conn:
        row = conn.execute(
            "SELECT count(*) FROM nanopub_publish WHERE claim_ref_id = %s",
            (old.claim_ref_id,),
        ).fetchone()
    assert row is not None and row[0] == 1


def test_dry_run_claims_nothing(store: Any) -> None:
    old = _old(store)
    posted: list[Any] = []
    registry.publish(
        store, old.claim_ref_id, interactive=True, post=lambda u, b: posted.append(b)
    )
    assert posted == [] and _fresh_state(store, old.id)[0] == "anchored"
