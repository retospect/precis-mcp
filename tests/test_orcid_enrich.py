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
