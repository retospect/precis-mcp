"""``memory_recall`` — the weekly P4 recall check (workers/memory_recall.py).

DB-backed: native and mirror-stamped ``SPACE:repo-dev`` memories through
:class:`precis.handlers.memory.MemoryHandler`, then the pass with injected
entries + the mock embedder. Scores land in ``app_settings``
(:data:`LAST_SCORE_KEY`); a drop raises a WARN ``memory_recall`` alert.
"""

from __future__ import annotations

import json

from precis.budget import settings as app_settings
from precis.cli.memory import SPACE_TAG
from precis.dispatch import Hub
from precis.handlers.memory import MemoryHandler
from precis.store import Store
from precis.workers.memory_recall import (
    ALERT_SOURCE,
    LAST_SCORE_KEY,
    run_memory_recall_pass,
)
from precis.workers.scheduler import CADENCES
from tests.conftest import id_of


def _put(hub: Hub, *, title: str, hook: str, mirror: bool = False) -> int:
    """One memory node whose hook carries the query's terms (the lexical
    leg + hash embedder hit on snapshot wording only); ``mirror`` stamps
    the ``file_mirror`` meta the origin split reads."""
    rid = id_of(
        MemoryHandler(hub=hub)
        .put(text=hook, title=title, tags=[SPACE_TAG], meta={"hook": hook})
        .body
    )
    if mirror:
        hub.live_store.update_ref(
            rid, meta_patch={"file_mirror": {"filename": f"{title}.md"}}
        )
    return rid


def _entries(*queries_wants: tuple[str, int]) -> list[dict[str, str]]:
    return [{"query": q, "want": f"me{w}"} for q, w in queries_wants]


def _open_alerts(store: Store) -> list[tuple[int, str]]:
    with store.pool.connection() as conn:
        return conn.execute(
            "SELECT ref_id, meta->>'severity' FROM refs "
            "WHERE alert_source = %s AND fingerprint = 'recall-dropped' "
            "AND resolved_at IS NULL",
            (ALERT_SOURCE,),
        ).fetchall()


def _last(store: Store) -> dict:
    raw = app_settings.get_setting(store, LAST_SCORE_KEY)
    assert raw is not None
    return json.loads(raw)


def test_cadence_registered_weekly() -> None:
    cad = next(c for c in CADENCES if c.name == "memory_recall")
    assert 7 * 24 * 3600 <= cad.interval_s < 8 * 24 * 3600
    assert cad.eligible is not None  # embedder-less hosts skip the claim
    assert not cad.spends


def test_eligible_gate_matches_embedder_config(monkeypatch) -> None:
    from types import SimpleNamespace

    import precis.config
    from precis.workers.scheduler import _memory_recall_eligible

    # Patch load_config, not env: PrecisConfig also reads .env, which a
    # dev checkout may carry — env alone can't make this deterministic.
    cfg = SimpleNamespace(embedder="mock", embedder_url=None)
    monkeypatch.setattr(precis.config, "load_config", lambda: cfg)
    assert _memory_recall_eligible()
    cfg.embedder = "remote"
    assert not _memory_recall_eligible()  # remote with no URL can't score
    cfg.embedder_url = "http://embedder.invalid"
    assert _memory_recall_eligible()


def test_load_recall_set_packaged() -> None:
    # The cadence's default: the same packaged P4 set bare `--set` scores.
    from precis.cli.memory import load_recall_set

    entries = load_recall_set()
    assert len(entries) >= 36
    assert all(e["query"] and e["want"].startswith("me") for e in entries)


def test_run_records_score_native_and_mirror(hub: Hub, store: Store) -> None:
    native = _put(hub, title="native-node", hook="zebra quorum native")
    mirror = _put(hub, title="mirror-node", hook="xenon lattice mirror", mirror=True)
    entries = _entries(("zebra quorum", native), ("xenon lattice", mirror))

    r = run_memory_recall_pass(store, entries=entries, embedder=hub.embedder)

    assert (r.claimed, r.ok, r.failed) == (2, 2, 0)
    doc = _last(store)
    assert doc["k"] == 5
    assert doc["hits"] == 2 and doc["total"] == 2
    assert doc["native_hits"] == 1 and doc["native_total"] == 1
    assert doc["mirror_hits"] == 1 and doc["mirror_total"] == 1
    assert doc["misses"] == []
    assert doc["computed_at"].endswith("+00:00")
    assert _open_alerts(store) == []  # first run only records a baseline


def test_score_drop_raises_warn_alert(hub: Hub, store: Store) -> None:
    native = _put(hub, title="native-node", hook="zebra quorum native")
    mirror = _put(hub, title="mirror-node", hook="xenon lattice mirror", mirror=True)
    entries = _entries(("zebra quorum", native), ("xenon lattice", mirror))
    run_memory_recall_pass(store, entries=entries, embedder=hub.embedder)

    store.retire_ref(mirror)  # the mirror answer node is gone -> mirror drops

    r = run_memory_recall_pass(store, entries=entries, embedder=hub.embedder)
    assert (r.ok, r.failed) == (1, 1)
    alerts = _open_alerts(store)
    assert len(alerts) == 1 and alerts[0][1] == "warn"

    doc = _last(store)
    assert doc["mirror_hits"] == 0 and doc["hits"] == 1
    assert doc["history"][-1]["hits"] == 2  # the pre-drop score is kept


def test_equal_or_higher_score_no_alert(hub: Hub, store: Store) -> None:
    native = _put(hub, title="native-node", hook="zebra quorum native")
    mirror = _put(hub, title="mirror-node", hook="xenon lattice mirror", mirror=True)
    entries = _entries(("zebra quorum", native), ("xenon lattice", mirror))

    run_memory_recall_pass(store, entries=entries, embedder=hub.embedder)
    run_memory_recall_pass(store, entries=entries, embedder=hub.embedder)
    assert _open_alerts(store) == []  # equal

    # Higher than the last recorded: seed a worse prior, rerun the same
    # score — no alert (and an open one would resolve).
    prior = _last(store)
    app_settings.set_setting(
        store,
        LAST_SCORE_KEY,
        json.dumps(
            {
                "computed_at": prior["computed_at"],
                "hits": 1,
                "total": 2,
                "native_hits": 1,
                "native_total": 1,
                "mirror_hits": 0,
                "mirror_total": 1,
            }
        ),
    )
    run_memory_recall_pass(store, entries=entries, embedder=hub.embedder)
    assert _open_alerts(store) == []


def test_recovery_resolves_the_drop_alert(hub: Hub, store: Store) -> None:
    native = _put(hub, title="native-node", hook="zebra quorum native")
    mirror = _put(hub, title="mirror-node", hook="xenon lattice mirror", mirror=True)
    entries = _entries(("zebra quorum", native), ("xenon lattice", mirror))
    run_memory_recall_pass(store, entries=entries, embedder=hub.embedder)

    store.retire_ref(mirror)
    run_memory_recall_pass(store, entries=entries, embedder=hub.embedder)
    assert len(_open_alerts(store)) == 1

    # The node is still gone but the last recorded score is the lower one,
    # so an unchanged run resolves the drop alert (its condition cleared).
    run_memory_recall_pass(store, entries=entries, embedder=hub.embedder)
    assert _open_alerts(store) == []
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM refs WHERE alert_source = %s "
            "AND resolved_at IS NOT NULL",
            (ALERT_SOURCE,),
        ).fetchone()
    assert row is not None and row[0] == 1
