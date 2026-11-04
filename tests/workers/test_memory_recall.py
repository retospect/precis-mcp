"""``memory_recall`` — the weekly P4 recall check (workers/memory_recall.py).

DB-backed: native and mirror-stamped ``SPACE:repo-dev`` memories through
:class:`precis.handlers.memory.MemoryHandler`, then the pass with injected
entries + the mock embedder. Scores land in ``app_settings``
(:data:`LAST_SCORE_KEY`); a drop raises a WARN ``memory_recall`` alert.
"""

from __future__ import annotations

import json

import pytest

from precis.budget import settings as app_settings
from precis.cli.memory import SPACE_TAG
from precis.cli.memory_recall import CURVE_KS, measure_recall
from precis.dispatch import Hub
from precis.handlers.memory import MemoryHandler
from precis.protocol import tolerates_extra_kwargs
from precis.runtime import PrecisRuntime
from precis.store import Store
from precis.workers.memory_recall import (
    ALERT_SOURCE,
    LAST_SCORE_KEY,
    memory_recall_summary,
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


def test_run_records_score_native_and_mirror(
    hub: Hub, store: Store, monkeypatch
) -> None:
    native = _put(hub, title="native-node", hook="zebra quorum native")
    mirror = _put(hub, title="mirror-node", hook="xenon lattice mirror", mirror=True)
    neighbor = _put(hub, title="linked evidence", hook="unrelated details")
    store.add_link(src_ref_id=native, dst_ref_id=neighbor, relation="related-to")
    entries = _entries(("zebra quorum", native), ("xenon lattice", mirror))
    rendered = []
    dispatch = PrecisRuntime.dispatch_with_status

    def capture(self, verb, args, **kwargs):
        assert kwargs["fold"] is True
        result = dispatch(self, verb, args, **kwargs)
        rendered.append((verb, dict(args), result))
        return result

    monkeypatch.setattr(PrecisRuntime, "dispatch_with_status", capture)

    r = run_memory_recall_pass(store, entries=entries, embedder=hub.embedder)

    assert (r.claimed, r.ok, r.failed) == (2, 2, 0)
    doc = _last(store)
    assert doc["k"] == 5
    assert doc["hits"] == 2 and doc["total"] == 2
    assert doc["native_hits"] == 1 and doc["native_total"] == 1
    assert doc["mirror_hits"] == 1 and doc["mirror_total"] == 1
    assert doc["misses"] == []
    assert doc["computed_at"].endswith("+00:00")
    assert doc["token_estimator"] == "chars/4"
    assert doc["rank_k"] == 12
    assert [row["k"] for row in doc["curve"]] == list(CURVE_KS)
    assert len(rendered) == 12  # four index sizes and both get views per query
    assert "linked evidence" in rendered[4][2][0]
    for row, calls in zip(doc["queries"], (rendered[:6], rendered[6:]), strict=True):
        assert row["rank"] == 1
        assert all(row["recall_at_k"].values())
        for cutoff, (_, args, (text, error)) in zip(CURVE_KS, calls[:4], strict=True):
            assert args["view"] == "index" and args["page_size"] == cutoff
            assert not error
            assert row["index"][str(cutoff)] == {
                "chars": len(text),
                "tokens": len(text) / 4,
            }
        for key, view, (_, args, (text, error)) in zip(
            ("fisheye_1hop", "fisheye_1hop_recall"),
            ("fisheye+1hop", "fisheye+1hop+recall"),
            calls[4:],
            strict=True,
        ):
            assert args["id"] == row["want"] and args["view"] == view
            assert not error
            assert row[key] == {"chars": len(text), "tokens": len(text) / 4}
    costs = [
        row["index"]["5"]["tokens"] + row["fisheye_1hop"]["tokens"]
        for row in doc["queries"]
    ]
    assert doc["median_tokens_k5"] == sum(costs) / 2
    assert all(row["measured"] == 2 for row in doc["curve"])
    assert _open_alerts(store) == []  # first run only records a baseline

    run_memory_recall_pass(store, entries=entries, embedder=hub.embedder)
    prior = _last(store)["history"][-1]
    for key in ("queries", "curve", "median_tokens_k5", "token_estimator", "rank_k"):
        assert prior[key] == doc[key]


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
    assert doc["queries"][1]["fisheye_1hop"] is None
    assert doc["queries"][1]["fisheye_1hop_recall"] is None
    assert all(row["measured"] == 1 for row in doc["curve"])
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


@pytest.mark.parametrize(
    ("factor", "estimator", "alerted"),
    [(1.30, "chars/4", True), (1.25, "chars/4", False), (1.30, "other", False)],
)
def test_token_regression_threshold_and_recovery(
    hub: Hub, store: Store, monkeypatch, factor: float, estimator: str, alerted: bool
) -> None:
    rid = _put(hub, title="native-node", hook="zebra quorum native")
    entries = _entries(("zebra quorum", rid))
    run_memory_recall_pass(store, entries=entries, embedder=hub.embedder)
    prior = _last(store)
    # Synthetic next measurement isolates the exact threshold from live
    # render metadata (for example age labels) changing between calls.
    measured = {
        key: prior[key]
        for key in ("token_estimator", "rank_k", "queries", "curve", "median_tokens_k5")
    }
    measured["median_tokens_k5"] *= factor
    monkeypatch.setattr(
        "precis.cli.memory_recall.measure_recall", lambda *a, **k: measured
    )
    prior["token_estimator"] = estimator
    app_settings.set_setting(store, LAST_SCORE_KEY, json.dumps(prior))
    run_memory_recall_pass(store, entries=entries, embedder=hub.embedder)
    assert bool(_open_alerts(store)) is alerted
    if alerted:
        with store.tx() as conn:
            alert = conn.execute(
                "SELECT kind, title FROM refs WHERE alert_source=%s "
                "AND fingerprint='recall-dropped' AND resolved_at IS NULL",
                (ALERT_SOURCE,),
            ).fetchone()
        assert alert is not None
        assert alert[0] == "alert"
        assert "median tokens" in alert[1] and "+30.0%" in alert[1]
    run_memory_recall_pass(store, entries=entries, embedder=hub.embedder)
    assert _open_alerts(store) == []


def test_curve_cutoffs_count_hits_not_hint_lines(hub: Hub, store: Store, monkeypatch):
    from precis.response import Response

    rid = _put(hub, title="wanted", hook="zebra quorum")

    # Pin rank eight independently of lexical/vector scoring and hint trailers.
    # The dispatcher's extra-kwarg gate reads the *stub's* signature (and
    # caches it per class), so a bare ``**kwargs`` would refuse q/tags/view
    # when no earlier test warmed the cache: opt out as a real handler would.
    @tolerates_extra_kwargs
    def ranked(self, *, page_size=10, **kwargs):
        handles = [f"me{rid + 100 + i}" for i in range(12)]
        handles[7] = f"me{rid}"
        return Response(
            body="heading\n"
            + "\n".join(f"- result ({h})" for h in handles[:page_size]),
            cost="[cost: included in the measured render]",
        )

    monkeypatch.setattr(MemoryHandler, "search", ranked)
    doc = measure_recall(store, _entries(("zebra quorum", rid)), embedder=hub.embedder)
    row = doc["queries"][0]
    assert row["rank"] == 8
    assert row["recall_at_k"] == {"3": False, "5": False, "8": True, "12": True}
    assert [r["hits"] for r in doc["curve"]] == [0, 0, 1, 1]
    assert row["index"]["5"]["chars"] > len("\n".join(["- result"] * 5))


def test_empty_set_and_missing_summary(store: Store, hub: Hub) -> None:
    assert memory_recall_summary(store) == ""
    run_memory_recall_pass(store, entries=[], embedder=hub.embedder)
    doc = _last(store)
    assert doc["median_tokens_k5"] is None and doc["queries"] == []
    assert all(row["measured"] == 0 and row["recall"] == 0 for row in doc["curve"])
    assert memory_recall_summary(store) == ""
    app_settings.set_setting(store, LAST_SCORE_KEY, "not json")
    assert memory_recall_summary(store) == ""
