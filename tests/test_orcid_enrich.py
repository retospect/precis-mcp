"""The ORCID identity tier — background fetch + cross-check pass
(precis.utils.authors module docstring).

DB-backed via the ``store`` fixture (skips without a reachable test
Postgres). ``fetch_record`` is always stubbed (``fetch_fn=``) so these
stay offline; ``has_credentials`` is monkeypatched per test.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from precis.health_checks import paper_authors_drift_check
from precis.ingest import orcid as orcid_api
from precis.store import Store
from precis.workers import orcid_enrich
from precis.workers.orcid_enrich import run_once

_VALID_ID = "0000-0002-1825-0097"
_SLUG = f"orcid:{_VALID_ID}"


def _paper(
    store: Store,
    *,
    slug: str,
    doi: str,
    authors: list[dict[str, Any]],
    source: str,
) -> int:
    ref = store.insert_ref(
        kind="paper", slug=slug, title="T", authors=authors, authors_source=source
    )
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO ref_identifiers (ref_id, id_kind, id_value, source) "
            "VALUES (%s, 'doi', %s, 'manual')",
            (ref.id, doi),
        )
        conn.commit()
    return ref.id


def _orcid_node(store: Store, *, slug: str = _SLUG, orcid_id: str = _VALID_ID) -> int:
    ref = store.insert_ref(
        kind="orcid",
        slug=slug,
        title=slug,
        provider="crossref",
        meta={"orcid_id": orcid_id},
    )
    return ref.id


def _record(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "orcid_id": _VALID_ID,
        "name": "Jane Doe",
        "given": "Jane",
        "family": "Doe",
        "credit_name": "",
        "biography": "",
        "keywords": [],
        "researcher_urls": [],
        "country": "",
        "employments": [],
        "works": [],
        "work_count": 0,
    }
    base.update(overrides)
    return base


@pytest.fixture(autouse=True)
def _reset_credentials_alert_flag() -> Iterator[None]:
    # The missing-credentials alert is guarded by a module-level flag so
    # it fires at most once per process; reset it around every test so
    # one test's trip doesn't silence another's.
    orcid_enrich._CREDENTIALS_ALERT_RAISED = False
    yield
    orcid_enrich._CREDENTIALS_ALERT_RAISED = False


def _stub_credentials(monkeypatch: pytest.MonkeyPatch, *, present: bool) -> None:
    monkeypatch.setattr(orcid_api, "has_credentials", lambda: present)


def _alert_count(store: Store) -> int:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM refs WHERE kind = 'alert' AND alert_source = %s",
            ("orcid_enrich",),
        ).fetchone()
    assert row is not None
    return int(row[0])


class TestFetchAndStore:
    def test_claimed_node_gets_fetched_at(
        self, store: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_credentials(monkeypatch, present=True)
        _orcid_node(store)
        record = _record()
        result = run_once(store, fetch_fn=lambda _oid: record, sleep_fn=lambda _s: None)
        assert result.claimed == 1
        assert result.ok == 1
        assert result.failed == 0

        ref = store.get_ref(kind="orcid", id=_SLUG)
        assert ref is not None
        assert ref.meta is not None
        assert ref.meta.get("fetched_at")

    def test_fetch_failure_counts_as_failed_and_leaves_node_unfetched(
        self, store: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_credentials(monkeypatch, present=True)
        _orcid_node(store)

        def _boom(_oid: str) -> dict[str, Any]:
            raise RuntimeError("upstream down")

        result = run_once(store, fetch_fn=_boom, sleep_fn=lambda _s: None)
        assert result.claimed == 1
        assert result.ok == 0
        assert result.failed == 1

        ref = store.get_ref(kind="orcid", id=_SLUG)
        assert ref is not None
        assert not (ref.meta or {}).get("fetched_at")
        # ...and the failure is remembered, so the next pass holds it back
        assert (ref.meta or {}).get("fetch_failed_at")
        assert (ref.meta or {}).get("fetch_fail_count") == 1


class TestFailureBackoff:
    """A node whose fetch fails never gets ``meta.fetched_at``, so without a
    failure memory the claim query re-picked it every pass forever — prod
    2026-09-26: 18 nodes × 28 failures in 24h = 501 of the fleet's 516 ERROR
    rows, and 18 of every 100-node batch burned on rows already known to
    fail (against a 93k-node claimable backlog).

    These drive :func:`orcid_enrich._claim_batch` directly rather than
    ``run_once``: the pass is throttled to one run per refresh window
    (``_due``), so a second ``run_once`` in one test claims nothing no
    matter what the backoff does — it would pass for the wrong reason.
    """

    STEP = orcid_enrich._BACKOFF_STEP_HOURS

    @staticmethod
    def _stamp_failure(
        store: Store, ref_id: int, *, hours_ago: float, count: int
    ) -> None:
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE refs SET meta = meta || jsonb_build_object("
                "  'fetch_failed_at', (now() - (%s || ' hours')::interval)::text,"
                "  'fetch_fail_count', %s::int) "
                "WHERE ref_id = %s",
                (hours_ago, count, ref_id),
            )
            conn.commit()

    def _claimed(self, store: Store) -> list[int]:
        return orcid_enrich._claim_batch(store, limit=10)

    def test_never_failed_node_is_claimable(self, store: Store) -> None:
        ref_id = _orcid_node(store)
        assert self._claimed(store) == [ref_id]

    def test_just_failed_node_is_held_back(self, store: Store) -> None:
        ref_id = _orcid_node(store)
        self._stamp_failure(store, ref_id, hours_ago=0.1, count=1)
        assert self._claimed(store) == []

    def test_node_is_claimable_once_its_window_elapses(self, store: Store) -> None:
        ref_id = _orcid_node(store)
        self._stamp_failure(store, ref_id, hours_ago=self.STEP + 1, count=1)
        assert self._claimed(store) == [ref_id]

    def test_window_widens_with_the_failure_count(self, store: Store) -> None:
        """The same gap that was long enough after one failure is not after
        two — a permanently-dead iD costs one attempt per widening window,
        not one per pass."""
        ref_id = _orcid_node(store)
        self._stamp_failure(store, ref_id, hours_ago=self.STEP + 1, count=2)
        assert self._claimed(store) == []
        self._stamp_failure(store, ref_id, hours_ago=2 * self.STEP + 1, count=2)
        assert self._claimed(store) == [ref_id]

    def test_window_stops_widening_at_the_cap(self, store: Store) -> None:
        """A huge failure count must not push the retry out to never."""
        ref_id = _orcid_node(store)
        capped_h = orcid_enrich._MAX_BACKOFF_STEPS * self.STEP
        self._stamp_failure(store, ref_id, hours_ago=capped_h + 1, count=9999)
        assert self._claimed(store) == [ref_id]

    def test_healthy_node_is_unaffected_by_a_sibling_in_backoff(
        self, store: Store
    ) -> None:
        """The throughput half of the bug: the held-back node frees its slot
        instead of consuming one every pass."""
        failing = _orcid_node(store)
        self._stamp_failure(store, failing, hours_ago=0.1, count=1)
        other_id = "0000-0002-1825-0098"
        healthy = _orcid_node(store, slug=f"orcid:{other_id}", orcid_id=other_id)
        assert self._claimed(store) == [healthy]

    def test_a_fetched_node_is_never_reclaimed_even_with_a_stale_count(
        self, store: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``fetched_at`` still wins outright, so a node that failed a few
        times and then succeeded needs no counter reset."""
        _stub_credentials(monkeypatch, present=True)
        ref_id = _orcid_node(store)
        # 3 failures ⇒ a 3-step window; sit just past it so it is claimable
        self._stamp_failure(store, ref_id, hours_ago=3 * self.STEP + 1, count=3)
        record = _record()
        result = run_once(store, fetch_fn=lambda _oid: record, sleep_fn=lambda _s: None)
        assert (result.claimed, result.ok) == (1, 1)
        assert self._claimed(store) == []


class TestCrossCheck:
    def test_matching_doi_verifies_and_overwrites_names(
        self, store: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_credentials(monkeypatch, present=True)
        paper_id = _paper(
            store,
            slug="p1",
            doi="10.1/a",
            authors=[{"given": "B.", "family": "Old", "orcid": _VALID_ID}],
            source="crossref",
        )
        node_id = _orcid_node(store)
        store.add_link(
            src_ref_id=node_id,
            dst_ref_id=paper_id,
            relation="authored",
            set_by="system",
            meta={"set_by": "paper-meta-enrich"},
        )
        record = _record(
            works=[{"doi": "10.1/a", "title": "T", "year": 2020, "url": ""}]
        )
        run_once(store, fetch_fn=lambda _oid: record, sleep_fn=lambda _s: None)

        rows = store.get_paper_authors(paper_id)
        assert len(rows) == 1
        row = rows[0]
        assert row["verified_at"] is not None
        assert row["given"] == "Jane"
        assert row["family"] == "Doe"
        assert row["source"] == "orcid"

        links = store.links_for(node_id, direction="out", relation="authored")
        edge = next(link for link in links if link.dst_ref_id == paper_id)
        assert edge.meta.get("verified") is True

    def test_human_row_keeps_names_but_gets_verified(
        self, store: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_credentials(monkeypatch, present=True)
        paper_id = _paper(
            store,
            slug="p2",
            doi="10.1/b",
            authors=[{"given": "Human", "family": "Edited", "orcid": _VALID_ID}],
            source="human",
        )
        node_id = _orcid_node(store)
        store.add_link(
            src_ref_id=node_id,
            dst_ref_id=paper_id,
            relation="authored",
            set_by="system",
            meta={},
        )
        record = _record(
            works=[{"doi": "10.1/b", "title": "T", "year": 2021, "url": ""}]
        )
        run_once(store, fetch_fn=lambda _oid: record, sleep_fn=lambda _s: None)

        row = store.get_paper_authors(paper_id)[0]
        assert row["verified_at"] is not None
        assert row["given"] == "Human"
        assert row["family"] == "Edited"
        assert row["source"] == "human"

    def test_doi_not_in_works_leaves_row_untouched_and_flags_edge(
        self, store: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_credentials(monkeypatch, present=True)
        paper_id = _paper(
            store,
            slug="p3",
            doi="10.1/c",
            authors=[{"given": "B.", "family": "Old", "orcid": _VALID_ID}],
            source="crossref",
        )
        node_id = _orcid_node(store)
        store.add_link(
            src_ref_id=node_id,
            dst_ref_id=paper_id,
            relation="authored",
            set_by="system",
            meta={},
        )
        record = _record(
            works=[{"doi": "10.9/other", "title": "T", "year": 2021, "url": ""}]
        )
        run_once(store, fetch_fn=lambda _oid: record, sleep_fn=lambda _s: None)

        row = store.get_paper_authors(paper_id)[0]
        assert row["verified_at"] is None
        assert row["given"] == "B."
        assert row["family"] == "Old"

        links = store.links_for(node_id, direction="out", relation="authored")
        edge = next(link for link in links if link.dst_ref_id == paper_id)
        assert edge.meta.get("orcid_unconfirmed") is True

    def test_drift_check_zero_after_cross_check(
        self, store: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_credentials(monkeypatch, present=True)
        paper_id = _paper(
            store,
            slug="p4",
            doi="10.1/d",
            authors=[{"given": "B.", "family": "Old", "orcid": _VALID_ID}],
            source="crossref",
        )
        node_id = _orcid_node(store)
        store.add_link(
            src_ref_id=node_id,
            dst_ref_id=paper_id,
            relation="authored",
            set_by="system",
            meta={},
        )
        record = _record(
            works=[{"doi": "10.1/d", "title": "T", "year": 2020, "url": ""}]
        )
        run_once(store, fetch_fn=lambda _oid: record, sleep_fn=lambda _s: None)

        assert paper_authors_drift_check(store)["count"] == 0


class TestMissingCredentials:
    def test_missing_credentials_raises_one_alert_and_claims_nothing(
        self, store: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_credentials(monkeypatch, present=False)
        _orcid_node(store)

        result = run_once(store, fetch_fn=lambda _oid: _record())
        assert result.claimed == 0
        assert result.ok == 0
        assert result.failed == 0
        assert _alert_count(store) == 1

        # A second call (credentials still missing) raises no second alert.
        result2 = run_once(store, fetch_fn=lambda _oid: _record())
        assert result2.claimed == 0
        assert _alert_count(store) == 1

        # Nothing was claimed either time — the node stays unfetched.
        ref = store.get_ref(kind="orcid", id=_SLUG)
        assert ref is not None
        assert not (ref.meta or {}).get("fetched_at")
