"""DOI backfill (id-batch + title-match, S2/Crossref clients injected —
no network hit).
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.ingest.doi_backfill import _is_real_doi, backfill_dois
from precis.store import Store


def _paper(
    store: Store,
    *,
    slug: str,
    title: str = "",
    year: int | None = None,
    doi: str | None = None,
    s2: str | None = None,
    arxiv: str | None = None,
) -> int:
    ref = store.insert_ref(kind="paper", slug=slug, title=title, year=year)
    with store.tx() as conn:
        if doi:
            store.set_ref_identifier(ref.id, "doi", doi, source="system", conn=conn)
        if s2:
            store.set_ref_identifier(ref.id, "s2", s2, source="system", conn=conn)
        if arxiv:
            store.set_ref_identifier(ref.id, "arxiv", arxiv, source="system", conn=conn)
    return ref.id


def _ref(store: Store, ref_id: int):
    return store.fetch_refs_by_ids([ref_id], include_deleted=True)[ref_id]


def _doi_of(store: Store, ref_id: int) -> str | None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT id_value FROM ref_identifiers WHERE ref_id=%s AND id_kind='doi'",
            (ref_id,),
        ).fetchone()
    return str(row[0]) if row else None


def _no_batch(
    _ids: list[str], _key: str
) -> list[dict[str, Any] | None]:  # pragma: no cover
    raise AssertionError("batch_fn should not be called")


def _no_crossref(_doi: str, _mailto: str) -> dict[str, Any] | None:  # pragma: no cover
    raise AssertionError("crossref_fn should not be called")


def _no_s2(_title: str, _key: str) -> dict[str, Any] | None:  # pragma: no cover
    raise AssertionError("s2_fn should not be called")


# ── _is_real_doi ─────────────────────────────────────────────────


def test_is_real_doi_rejects_arxiv_datacite() -> None:
    assert not _is_real_doi("10.48550/arXiv.2401.12345")
    assert not _is_real_doi("10.48550/ARXIV.2401.12345")  # case-insensitive
    assert not _is_real_doi(None)
    assert not _is_real_doi("")


def test_is_real_doi_accepts_publisher_doi() -> None:
    assert _is_real_doi("10.1038/s41567-024-1234-5")
    assert _is_real_doi("10.1234/real")


# ── phase A: deterministic id path ───────────────────────────────


def test_phase_a_writes_real_doi_and_skips_arxiv_datacite(store: Store) -> None:
    rid_s2 = _paper(store, slug="ida", title="Has S2 Id", s2="s2abc")
    rid_arxiv = _paper(store, slug="idb", title="Has ArXiv Id", arxiv="2401.99999")

    def fake_batch(req_ids: list[str], api_key: str) -> list[dict[str, Any] | None]:
        table = {
            "s2abc": {"doi": "10.1234/real"},
            "ARXIV:2401.99999": {"doi": "10.48550/arXiv.2401.99999"},
        }
        return [table.get(r) for r in req_ids]

    result = backfill_dois(
        store,
        apply=True,
        ids=[rid_s2, rid_arxiv],
        do_title_phase=False,
        batch_fn=fake_batch,
    )

    assert result.recovered_id == {rid_s2: "10.1234/real"}
    assert _doi_of(store, rid_s2) == "10.1234/real"
    # arXiv DataCite DOI is not real — never written, never counted recovered.
    assert rid_arxiv not in result.recovered_id
    assert _doi_of(store, rid_arxiv) is None


def test_dry_run_writes_nothing(store: Store) -> None:
    rid = _paper(store, slug="dry", title="Dry Run Paper", s2="s2dry")

    def fake_batch(req_ids: list[str], api_key: str) -> list[dict[str, Any] | None]:
        return [{"doi": "10.1234/dryrun"} for _ in req_ids]

    result = backfill_dois(
        store, apply=False, ids=[rid], do_title_phase=False, batch_fn=fake_batch
    )

    # Reports what it WOULD write...
    assert result.recovered_id == {rid: "10.1234/dryrun"}
    # ...but writes nothing.
    assert _doi_of(store, rid) is None


def test_phase_a_skips_doi_owned_by_another_ref(store: Store) -> None:
    owner = _paper(store, slug="owner", title="Owner Paper", doi="10.9/dup")
    rid = _paper(store, slug="claimant", title="Claimant Paper", s2="s2dup")

    def fake_batch(req_ids: list[str], api_key: str) -> list[dict[str, Any] | None]:
        return [{"doi": "10.9/dup"} for _ in req_ids]

    result = backfill_dois(
        store, apply=True, ids=[rid], do_title_phase=False, batch_fn=fake_batch
    )

    assert rid not in result.recovered_id
    assert rid in result.id_owned_elsewhere
    assert _doi_of(store, rid) is None
    assert _doi_of(store, owner) == "10.9/dup"  # untouched


def test_phase_a_write_failure_is_not_owned_elsewhere(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient write error (not a live-owner conflict) must land in the
    distinct ``id_write_failed`` bucket, never get miscounted as a
    duplicate-DOI conflict in ``id_owned_elsewhere``."""
    rid = _paper(store, slug="write-fail", title="Write Fail Paper", s2="s2wf")

    def fake_batch(req_ids: list[str], api_key: str) -> list[dict[str, Any] | None]:
        return [{"doi": "10.1234/wf"} for _ in req_ids]

    def boom(*_a: Any, **_k: Any) -> bool:
        raise RuntimeError("transient db hiccup")

    monkeypatch.setattr(store, "set_ref_identifier", boom)

    result = backfill_dois(
        store, apply=True, ids=[rid], do_title_phase=False, batch_fn=fake_batch
    )

    assert rid in result.id_write_failed
    assert rid not in result.id_owned_elsewhere
    assert rid not in result.recovered_id
    assert _doi_of(store, rid) is None


# ── phase B: title match ─────────────────────────────────────────


def test_phase_b_auto_recovers_writes_only_doi(store: Store) -> None:
    rid = _paper(
        store, slug="ttl-auto", title="A Very Specific Widget Study", year=2010
    )

    def fake_s2(title: str, api_key: str) -> dict[str, Any] | None:
        return {
            "title": "A Very Specific Widget Study",
            "authors": [{"name": "Someone Else"}],
            "year": 2010,
            "doi": "10.1234/ttl-hit",
        }

    result = backfill_dois(
        store,
        apply=True,
        ids=[rid],
        do_id_phase=False,
        crossref_fn=_no_crossref,
        s2_fn=fake_s2,
    )

    assert result.recovered_title == {rid: "10.1234/ttl-hit"}
    assert _doi_of(store, rid) == "10.1234/ttl-hit"
    # Surgical write — title/authors untouched (unlike apply_resolution).
    ref = _ref(store, rid)
    assert ref.title == "A Very Specific Widget Study"
    assert not ref.authors


def test_phase_b_arxiv_only_classified_not_written(store: Store) -> None:
    rid = _paper(
        store, slug="ttl-arxiv", title="A Preprint With No Publisher DOI", year=2022
    )

    def fake_s2(title: str, api_key: str) -> dict[str, Any] | None:
        return {
            "title": "A Preprint With No Publisher DOI",
            "authors": [],
            "year": 2022,
            "doi": "10.48550/arXiv.2201.00001",
        }

    result = backfill_dois(
        store,
        apply=True,
        ids=[rid],
        do_id_phase=False,
        crossref_fn=_no_crossref,
        s2_fn=fake_s2,
    )

    assert rid in result.arxiv_only
    assert rid not in result.recovered_title
    assert _doi_of(store, rid) is None


def test_phase_b_review_verdict_not_written(store: Store) -> None:
    rid = _paper(
        store, slug="ttl-review", title="A Study Of Widget Dynamics", year=2005
    )

    def fake_s2(title: str, api_key: str) -> dict[str, Any] | None:
        return {
            "title": "A Study Of Widget Dynamics",  # identical -> high sim
            "authors": [{"name": "X"}],
            "year": 2020,  # incompatible year -> review, not auto
            "doi": "10.1/mismatch",
        }

    result = backfill_dois(
        store,
        apply=True,
        ids=[rid],
        do_id_phase=False,
        crossref_fn=_no_crossref,
        s2_fn=fake_s2,
    )

    assert any(r == rid for r, _reason in result.review)
    assert rid not in result.recovered_title
    assert _doi_of(store, rid) is None


# ── cohort selection ──────────────────────────────────────────────


def test_ids_cohort_selects_exact_set(store: Store) -> None:
    rid_a = _paper(store, slug="cohort-a", title="Cohort A")
    rid_b = _paper(store, slug="cohort-b", title="Cohort B")
    _paper(store, slug="cohort-c", title="Cohort C")  # DOI-less too, but not requested

    result = backfill_dois(
        store,
        apply=False,
        ids=[rid_a, rid_b],
        do_id_phase=False,
        do_title_phase=False,
    )

    assert result.cohort == [rid_a, rid_b]


def test_ids_cohort_excludes_soft_deleted_ref(store: Store) -> None:
    """``--ids`` must not resurrect a tombstoned ref into the write cohort
    — ``fetch_refs_by_ids`` defaults to ``include_deleted=True`` (built for
    link-rendering), which would otherwise let ``--apply --ids
    <tombstoned-ref>`` write a DOI onto a dead row."""
    live = _paper(store, slug="live-ref", title="Live Paper", s2="s2live")
    gone = _paper(store, slug="gone-ref", title="Gone Paper", s2="s2gone")
    store.soft_delete_ref(gone)

    def fake_batch(req_ids: list[str], api_key: str) -> list[dict[str, Any] | None]:
        return [{"doi": "10.1234/x"} for _ in req_ids]

    result = backfill_dois(
        store,
        apply=True,
        ids=[live, gone],
        do_title_phase=False,
        batch_fn=fake_batch,
    )

    assert result.cohort == [live]
    assert gone not in result.cohort
    assert gone not in result.recovered_id
    assert _doi_of(store, gone) is None
