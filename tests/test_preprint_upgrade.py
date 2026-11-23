"""gr690851 — the preprint → published upgrade.

Covers ``src/precis/ingest/preprint_upgrade.py`` (detection, both
discovery lanes, the soft-retire write set, idempotence, sweep cohort),
the cite-resolution half in ``src/precis/taproot/cite.py`` (a hub citing
a superseded preprint resolves to the published cite_key) and the
retraction-walk half in ``src/precis/export/retraction.py`` (the walk
returns the published ref, so the watch checks the real Crossref DOI).

DB-backed (real ``refs``/``links``/``ref_identifiers``/``ref_tags`` via
the ``store`` fixture), mirroring ``tests/test_taproot_cite.py``'s setup
style; the network lanes are injected fns, no real S2/Crossref calls.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from precis.export.retraction import cited_paper_refs, draft_retraction_report
from precis.export.walker import resolve_cite_handle, resolve_si_slug
from precis.ingest.preprint_upgrade import (
    SUPERSEDED_TAG,
    is_preprint,
    sweep_preprint_upgrades,
    upgrade_preprint,
)
from precis.taproot.canon import CanonicalClaim
from precis.taproot.cite import finding_cite_keys, resolve_pin_handle
from precis.taproot.hub import attach_evidence, mint_hub

_CLAIM = CanonicalClaim(
    sentence="Pd/C catalyzes Suzuki coupling at room temperature with a mild base.",
    scope={"material": "Pd/C", "method": "Suzuki coupling", "regime": "RT"},
)

_PUBLISHED_DOI = "10.1038/s41586-024-07122-7"

#: The S2 batch answer for the arXiv-id lane: the same work, now with the
#: journal DOI in externalIds.
_S2_BATCH_META = {
    "title": "Room-temperature Suzuki coupling over a Pd/C packed bed",
    "authors": [{"name": "Doe, Jane"}],
    "year": 2024,
    "doi": _PUBLISHED_DOI,
    "arxiv_id": "2401.12345",
    "journal": "Nature",
    "external_ids": {"DOI": _PUBLISHED_DOI, "ArXiv": "2401.12345"},
}


def _crossref(doi: str) -> dict[str, Any]:
    return {
        "DOI": doi,
        "type": "journal-article",
        "title": [_S2_BATCH_META["title"]],
        "author": [{"family": "Doe", "given": "Jane"}],
        "published-online": {"date-parts": [[2024]]},
        "container-title": ["Nature"],
    }


def _batch_hits(
    metas: list[dict[str, Any] | None],
) -> Callable[[list[str], str], list[dict[str, Any] | None]]:
    """A BatchFn standing in for get_papers_batch: answer any request id
    from ``metas`` positionally."""

    def _fn(ids: list[str], api_key: str) -> list[dict[str, Any] | None]:
        return list(metas)

    return _fn


def _s2_returns(
    meta: dict[str, Any] | None,
) -> Callable[[str, str], dict[str, Any] | None]:
    """An S2Fn standing in for lookup_s2 (the title-search lane)."""

    def _fn(title: str, api_key: str) -> dict[str, Any] | None:
        return meta

    return _fn


def _preprint(
    store: Any,
    *,
    key: str,
    title: str = "A preprint result",
    year: int = 2023,
    arxiv: str | None = None,
) -> int:
    ref = store.insert_ref(kind="paper", slug=key, title=title, year=year, meta={})
    # An arXiv id is globally owned — derive a stable per-key id so two
    # preprints in one test don't collide on the identifier.
    store.set_ref_identifier(
        ref.id,
        "arxiv",
        arxiv or f"2401.{10000 + sum(map(ord, key)) % 80000}",
        source="test",
    )
    return ref.id


def _supersedes_links(store: Any, ref_id: int) -> list[Any]:
    return store.links_for(ref_id, direction="in", relation="supersedes")


# ── detection ────────────────────────────────────────────────────────────


def test_is_preprint_detection_rules() -> None:
    assert is_preprint({"arxiv": "2401.12345"}) is True  # arXiv id
    assert is_preprint({"doi": "10.48550/arXiv.2401.12345"}) is True  # mint only
    assert is_preprint({}) is True  # no DOI at all
    assert is_preprint({"doi": "10.1038/s41586-024-07122-7"}) is False  # real DOI
    # A mint alongside a real DOI is not a hole — the watch can check it.
    assert (
        is_preprint({"doi": "10.48550/arxiv.2401.12345", "pubmed": "123"})
        is True  # mint still the only DOI
    )


# ── the upgrade write set (arXiv-id / externalIds lane) ──────────────────


def test_upgrade_via_s2_external_ids(store: Any) -> None:
    pre = _preprint(store, key="pua01a")
    outcome = upgrade_preprint(
        store,
        pre,
        batch_fn=_batch_hits([_S2_BATCH_META]),
        crossref_fn=_crossref,
        s2_fn=_s2_returns(None),
    )

    assert outcome.verdict == "upgraded"
    pub = outcome.published_ref_id
    assert pub is not None and pub != pre
    # First-class published ref carrying the real DOI.
    assert store.identifier_owner("doi", _PUBLISHED_DOI) == pub
    assert store.ref_cite_keys(pub)  # minted with a real cite key
    # Soft-retire: supersedes link + STATUS tag, ref kept live.
    links = _supersedes_links(store, pre)
    assert len(links) == 1
    assert links[0].src_ref_id == pub
    assert SUPERSEDED_TAG in store.tags_for(pre)
    kept = store.fetch_refs_by_ids([pre], include_deleted=False)
    assert pre in kept  # not retired
    meta = kept[pre].meta
    assert meta.get("preprint_upgraded_to") == pub
    assert meta.get("preprint_upgrade_checked_at")


def test_upgrade_never_fires_on_weak_title_match(store: Any) -> None:
    pre = store.insert_ref(
        kind="paper",
        slug="puw01a",
        title="Fröhlich polarons in two-dimensional semiconductors",
        year=2022,
        meta={},
    ).id
    # S2 answers an unrelated paper with a real DOI — sim far below the
    # 0.85 auto gate (and below the 0.6 review floor).
    far = {
        "title": "Baking sourdough at high altitude: a community study",
        "authors": [{"name": "Baker, Bob"}],
        "year": 2022,
        "doi": "10.1000/xyz123",
        "external_ids": {"DOI": "10.1000/xyz123"},
    }
    outcome = upgrade_preprint(
        store,
        pre,
        batch_fn=_batch_hits([None]),
        s2_fn=_s2_returns(far),
    )

    assert outcome.verdict == "no-published-version"
    assert _supersedes_links(store, pre) == []
    assert SUPERSEDED_TAG not in store.tags_for(pre)
    # The attempt is stamped so the sweep doesn't re-probe every pass.
    meta = store.fetch_refs_by_ids([pre])[pre].meta
    assert meta.get("preprint_upgrade_checked_at")


def test_upgrade_via_high_confidence_title_match(store: Any) -> None:
    pre = store.insert_ref(
        kind="paper",
        slug="put01a",
        title="Room-temperature Suzuki coupling over a Pd/C packed bed",
        year=2023,
        meta={},
    ).id
    hit = dict(_S2_BATCH_META)
    hit["year"] = 2023  # year-compatible with the preprint
    outcome = upgrade_preprint(
        store,
        pre,
        batch_fn=_batch_hits([None]),  # no id- lane record
        s2_fn=_s2_returns(hit),
        crossref_fn=_crossref,
    )

    assert outcome.verdict == "upgraded"
    assert outcome.doi == _PUBLISHED_DOI
    assert outcome.reason == "s2-title-match"


def test_upgrade_skips_human_verified(store: Any) -> None:
    pre = _preprint(store, key="puh01a")
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET human_verified_at = now() WHERE ref_id = %s", (pre,)
        )
        conn.commit()

    outcome = upgrade_preprint(
        store,
        pre,
        batch_fn=_batch_hits([_S2_BATCH_META]),
        crossref_fn=_crossref,
        s2_fn=_s2_returns(None),
    )

    assert outcome.verdict == "skipped"
    assert _supersedes_links(store, pre) == []


# ── idempotence ──────────────────────────────────────────────────────────


def test_rerun_is_idempotent(store: Any) -> None:
    pre = _preprint(store, key="pui01a")
    batch = _batch_hits([_S2_BATCH_META])
    first = upgrade_preprint(store, pre, batch_fn=batch, crossref_fn=_crossref)

    def already_done(doi: str) -> dict[str, Any]:
        raise AssertionError("completed upgrades must skip discovery")

    second = upgrade_preprint(store, pre, batch_fn=batch, crossref_fn=already_done)

    assert first.published_ref_id is not None
    assert second.published_ref_id == first.published_ref_id
    assert second.verdict == "skipped"
    assert len(_supersedes_links(store, pre)) == 1
    assert len(store.events_for(pre, event="superseded_by_publication")) == 1
    assert store.tags_for(pre).count(SUPERSEDED_TAG) == 1

    # And the sweep no longer claims the upgraded ref at all.
    swept = sweep_preprint_upgrades(
        store, limit=10, delay=0, batch_fn=batch, crossref_fn=_crossref
    )
    assert all(o.ref_id != pre for o in swept.outcomes)


# ── sweep cohort: only cited preprints ───────────────────────────────────


def test_sweep_only_claims_used_preprints(store: Any) -> None:
    unused = _preprint(store, key="pus01a")
    cited = _preprint(store, key="pus02a")
    citer = store.insert_ref(kind="paper", slug="pus03a", title="Citer", meta={})
    store.add_link(src_ref_id=citer.id, dst_ref_id=cited, relation="cites")

    probes: list[str] = []

    def _probe(ids: list[str], api_key: str) -> list[dict[str, Any] | None]:
        probes.extend(ids)
        return [None]

    swept = sweep_preprint_upgrades(
        store, limit=10, delay=0, batch_fn=_probe, s2_fn=_s2_returns(None)
    )

    assert len(probes) == 1
    assert swept.outcomes[0].verdict == "no-published-version"
    assert [o.ref_id for o in swept.outcomes] == [cited]
    assert unused not in [o.ref_id for o in swept.outcomes]


def test_sweep_deferral_on_rate_limit_stops_pass(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from precis.ingest import semantic_scholar
    from precis.utils.rate_limit import RateLimited

    one = _preprint(store, key="pud01a")
    two = _preprint(store, key="pud02a")
    for rid in (one, two):
        citer = store.insert_ref(kind="paper", slug=f"pudc{rid}a", title="C", meta={})
        store.add_link(src_ref_id=citer.id, dst_ref_id=rid, relation="cites")

    def _limited(*args: Any, **kwargs: Any) -> Any:
        raise RateLimited("s2")

    monkeypatch.setattr(semantic_scholar, "_client", lambda key: object())
    monkeypatch.setattr(semantic_scholar, "_get_papers_with_retry", _limited)
    swept = sweep_preprint_upgrades(store, limit=10, delay=0, s2_fn=_s2_returns(None))

    assert any(o.verdict == "deferred" for o in swept.outcomes)
    assert len(swept.outcomes) == 1  # the pass stopped at the refusal
    assert all(
        "preprint_upgrade_checked_at" not in ref.meta
        for ref in store.fetch_refs_by_ids([one, two]).values()
    )


# ── cite resolution honors the upgrade ───────────────────────────────────


def _hub_citing_preprint(store: Any, pre_key: str, pre_id: int) -> int:
    """A claim hub whose derived originator is the preprint ``pre_id``."""
    hub = mint_hub(store, _CLAIM)
    follow = store.insert_ref(
        kind="paper", slug=f"{pre_key}f", title="Follow-up", year=2005, meta={}
    )
    attach_evidence(store, hub_ref_id=hub, paper_ref_id=pre_id, role="corroborates")
    attach_evidence(store, hub_ref_id=hub, paper_ref_id=follow.id, role="corroborates")
    store.add_link(src_ref_id=follow.id, dst_ref_id=pre_id, relation="cites")
    return hub


def test_hub_cite_resolves_to_published_key(store: Any) -> None:
    pre = _preprint(store, key="pur01a")
    outcome = upgrade_preprint(
        store,
        pre,
        batch_fn=_batch_hits([_S2_BATCH_META]),
        crossref_fn=_crossref,
        s2_fn=_s2_returns(None),
    )
    pub_key = store.ref_cite_keys(outcome.published_ref_id)[0]
    hub = _hub_citing_preprint(store, "pur01a", pre)

    result = finding_cite_keys(store, hub)

    assert result.is_hub is True
    assert result.cite_keys == [pub_key]


def test_bulk_supersede_follows_chains(store: Any) -> None:
    a = store.insert_ref(kind="paper", slug="puc01a", title="A", meta={}).id
    b = store.insert_ref(kind="paper", slug="puc02a", title="B", meta={}).id
    c = store.insert_ref(kind="paper", slug="puc03a", title="C", meta={}).id
    store.add_link(src_ref_id=b, dst_ref_id=a, relation="supersedes")
    store.add_link(src_ref_id=c, dst_ref_id=b, relation="supersedes")

    assert store.supersede_targets_bulk([a]) == {a: c}
    assert store.supersede_targets_bulk([a, c]) == {a: c}
    assert store.supersede_targets_bulk([c]) == {}


@pytest.mark.parametrize("work_type", [None, "posted-content", "dataset"])
def test_requires_a_published_crossref_work(store: Any, work_type: str | None) -> None:
    pre = _preprint(store, key="not-published")
    outcome = upgrade_preprint(
        store,
        pre,
        batch_fn=_batch_hits([_S2_BATCH_META]),
        s2_fn=_s2_returns(None),
        crossref_fn=lambda doi: (
            {**_crossref(doi), "type": work_type} if work_type else None
        ),
    )
    assert outcome.verdict == "no-published-version"
    assert _supersedes_links(store, pre) == []
    assert store.identifier_owner("doi", _PUBLISHED_DOI) is None


def test_transaction_rechecks_after_discovery(store: Any) -> None:
    pre = _preprint(store, key="racing-preprint")
    alternate_doi = "10.1234/alternate"
    batch = _batch_hits([_S2_BATCH_META])

    def competing_discovery(doi: str) -> dict[str, Any]:
        winner = upgrade_preprint(store, pre, batch_fn=batch, crossref_fn=_crossref)
        assert winner.verdict == "upgraded"
        return _crossref(doi)

    outcome = upgrade_preprint(
        store,
        pre,
        batch_fn=_batch_hits([{**_S2_BATCH_META, "doi": alternate_doi}]),
        crossref_fn=competing_discovery,
    )
    assert outcome.verdict == "skipped"
    assert store.identifier_owner("doi", alternate_doi) is None
    assert len(_supersedes_links(store, pre)) == 1
    assert len(store.events_for(pre, event="superseded_by_publication")) == 1


def test_sweep_advances_past_verified_and_self_owned_doi(store: Any) -> None:
    verified = _preprint(store, key="verified-first")
    self_owned = _preprint(store, key="self-owned-second")
    later = _preprint(store, key="eligible-last")
    citer = store.insert_ref(
        kind="paper", slug="preprint-citer", title="Citer", meta={}
    )
    for ref_id in (verified, self_owned, later):
        store.add_link(src_ref_id=citer.id, dst_ref_id=ref_id, relation="cites")
    with store.tx() as conn:
        conn.execute(
            "UPDATE refs SET human_verified_at = now() WHERE ref_id = %s", (verified,)
        )
    store.set_ref_identifier(self_owned, "doi", _PUBLISHED_DOI, source="test")

    first = sweep_preprint_upgrades(
        store,
        limit=1,
        delay=0,
        batch_fn=_batch_hits([_S2_BATCH_META]),
        crossref_fn=_crossref,
    )
    assert [(o.ref_id, o.reason) for o in first.outcomes] == [
        (self_owned, "preprint-owns-published-doi")
    ]
    second = sweep_preprint_upgrades(
        store, limit=1, delay=0, batch_fn=_batch_hits([None]), s2_fn=_s2_returns(None)
    )
    assert [o.ref_id for o in second.outcomes] == [later]


def test_draft_citation_keeps_passage_and_uses_published_key(store: Any) -> None:
    pre = _preprint(store, key="held-preprint")
    with store.tx() as conn:
        row = conn.execute(
            "INSERT INTO chunks (ref_id, ord, chunk_kind, text) "
            "VALUES (%s, 0, 'paragraph', 'Held preprint passage') RETURNING chunk_id",
            (pre,),
        ).fetchone()
    assert row is not None
    chunk_id = int(row[0])
    outcome = upgrade_preprint(
        store, pre, batch_fn=_batch_hits([_S2_BATCH_META]), crossref_fn=_crossref
    )
    pub_key = store.ref_cite_keys(outcome.published_ref_id)[0]
    assert resolve_cite_handle(store, f"pc{chunk_id}") == (pub_key, chunk_id, False)
    assert resolve_pin_handle(store, f"pc{chunk_id}") == (pre, pub_key)
    assert resolve_si_slug(
        "held-preprint", store, si_cache={}, target_cache={}, warnings=[]
    ) == (pub_key, False)
    assert store.resolve_handle(f"pc{chunk_id}").ref_id == pre


def test_report_tracks_original_preprint_and_deduplicates_publication(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from precis.ingest import provenance
    from precis_web.routes.drafts import _retraction_report_json

    pre = _preprint(store, key="puw02a")
    outcome = upgrade_preprint(
        store, pre, batch_fn=_batch_hits([_S2_BATCH_META]), crossref_fn=_crossref
    )
    pub = outcome.published_ref_id
    pub_key = store.ref_cite_keys(pub)[0]
    keys = ["puw02a", pub_key]
    refs, unresolved = cited_paper_refs(store, object(), cited_slugs=keys)
    assert unresolved == []
    assert [r.id for r in refs] == [pub]

    draft, _ = store.drafts.create_draft(name="upgrade-report", title="Report")
    for ref_id in (pre, pub):
        store.drafts.add_chunks(
            ref_id=draft.id,
            chunk_kind="paragraph",
            text=f"A report cites [pa{ref_id}].",
            at={"last": True},
        )
    checked: list[str] = []

    def check(doi: str, **kwargs: Any) -> provenance.ProvenanceResult:
        checked.append(doi)
        return provenance.ProvenanceResult(doi=doi, status="ok")

    monkeypatch.setattr(provenance, "check_doi", check)
    report = draft_retraction_report(store, draft, check=True)
    assert checked == [_PUBLISHED_DOI]
    assert [p.ref_id for p in report.papers] == [pub]
    stored = store.fetch_refs_by_ids([pre, pub])
    assert stored[pub].retraction_checked_at is not None
    assert stored[pre].retraction_checked_at is None
    assert [
        p["ref_id"] for p in _retraction_report_json(report)["upgraded_preprints"]
    ] == [pub]

    direct = draft_retraction_report(store, object(), cited_slugs=[pub_key])
    assert direct.upgraded_preprints == []
