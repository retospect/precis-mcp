"""Taproot Phase 1 — ``src/precis/taproot/cite.py``: the ONE hub
cite-key resolution policy shared by ``precis resolve`` (`cli/resolve.py`)
and both draft exporters (`export/latex.py` / `export/docx.py`).

DB-backed (real ``refs``/``chunks``/``ref_tags``/``links`` via the
``store`` fixture), mirroring ``tests/test_taproot_seniority.py``'s setup
style: mint a hub via ``hub.mint_hub``, attach evidence via
``hub.attach_evidence``, and write the intra-supporter ``cites`` edge
that makes ``derive_evidence`` split originators from corroborators.
"""

from __future__ import annotations

import re
from typing import Any

from precis.dispatch import Hub
from precis.handlers.finding import FindingHandler
from precis.taproot.canon import CanonicalClaim
from precis.taproot.cite import (
    OUTCOME_CAP,
    OUTCOME_CONFIRMATION,
    OUTCOME_FIRST,
    OUTCOME_REVIEW_HELD,
    OUTCOME_REVIEW_LATER,
    OUTCOME_REVIEW_PRINTED,
    OUTCOME_SHARED_AUTHOR,
    apply_pin,
    finding_cite_keys,
    hub_cite_keys,
    resolve_hub_print,
    resolve_pin_handle,
)
from precis.taproot.hub import attach_evidence, mint_hub
from precis.taproot.seniority import derive_evidence
from precis.utils import handle_registry

_CLAIM = CanonicalClaim(
    sentence="Pd/C catalyzes Suzuki coupling at room temperature with a mild base.",
    scope={"material": "Pd/C", "method": "Suzuki coupling", "regime": "RT"},
)


def _search(pattern: str, text: str) -> re.Match[str]:
    """``re.search`` narrowed for tests — asserts the pattern actually hit."""
    m = re.search(pattern, text)
    assert m is not None, f"pattern {pattern!r} not found in {text!r}"
    return m


def _make_handler(store: Any) -> FindingHandler:
    return FindingHandler(hub=Hub(store=store))


def _paper(store: Any, *, cite_key: str, title: str, year: int | None = None) -> int:
    ref = store.insert_ref(kind="paper", slug=cite_key, title=title, year=year, meta={})
    return ref.id


# ── hub → derived originator(s) ─────────────────────────────────────────


def test_finding_cite_keys_hub_resolves_to_originator(store: Any) -> None:
    hub = mint_hub(store, _CLAIM)
    origin = _paper(store, cite_key="ftco01a", title="Original report", year=2001)
    follow = _paper(store, cite_key="ftcf05a", title="Follow-up", year=2005)
    attach_evidence(store, hub_ref_id=hub, paper_ref_id=origin, role="corroborates")
    attach_evidence(store, hub_ref_id=hub, paper_ref_id=follow, role="corroborates")
    store.add_link(src_ref_id=follow, dst_ref_id=origin, relation="cites")

    result = finding_cite_keys(store, hub)

    assert result.is_hub is True
    assert result.inflight is False
    assert result.cite_keys == ["ftco01a"]


def test_finding_cite_keys_hub_empty_evidence_is_inflight(store: Any) -> None:
    hub = mint_hub(store, _CLAIM)

    result = finding_cite_keys(store, hub)

    assert result.is_hub is True
    assert result.inflight is True
    assert result.cite_keys == []


# ── non-hub finding → its own meta ──────────────────────────────────────


def test_finding_cite_keys_non_hub_established(store: Any) -> None:
    _paper(store, cite_key="mlr23a", title="paper mlr23a")
    handler = _make_handler(store)
    resp = handler.put(title="t", body="b", scope={}, cited_in="mlr23a")
    ref_id = int(_search(r"id=(\d+)", resp.body).group(1))
    store.update_ref(ref_id, meta_patch={"primary_cite_key": "mlr23a"})

    result = finding_cite_keys(store, ref_id)

    assert result.is_hub is False
    assert result.inflight is False
    assert result.cite_keys == ["mlr23a"]


def test_finding_cite_keys_non_hub_pub_id_only(store: Any) -> None:
    _paper(store, cite_key="pnd01a", title="paper pnd01a")
    handler = _make_handler(store)
    resp = handler.put(title="t", body="b", scope={}, cited_in="pnd01a")
    ref_id = int(_search(r"id=(\d+)", resp.body).group(1))
    pub_id = _search(r"pub_id=(\w+)", resp.body).group(1)

    result = finding_cite_keys(store, ref_id)

    assert result.is_hub is False
    assert result.inflight is False
    assert result.cite_keys == [pub_id]


# ── Taproot Phase 2 — authorial pins (shared `apply_pin` / `resolve_pin_handle`)
#
# The ONE pin-application policy `precis resolve` and the draft `mentions`
# grammar both call — mirrors tests/cli/test_resolve.py's pin coverage
# (ported off `_apply_pin`/`_resolve_pin_handle`) at the shared-module level.


def _paper_chunk(store: Any, ref_id: int, *, ord: int = 0) -> int:
    """Insert a minimal body chunk directly so a `pc<chunk_id>` passage
    handle has something real to resolve to."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO chunks (ref_id, ord, chunk_kind, text) "
            "VALUES (%s, %s, 'paragraph', %s) RETURNING chunk_id",
            (ref_id, ord, "a grounded passage"),
        ).fetchone()
        conn.commit()
    assert row is not None
    return int(row[0])


def _hub_with_derived_originator(
    store: Any, *, origin_key: str, follow_key: str
) -> tuple[int, int]:
    """A hub whose derived `establishes` originator is the paper
    `origin_key`. Returns ``(hub_ref_id, origin_ref_id)``."""
    hub = mint_hub(store, _CLAIM)
    origin = _paper(store, cite_key=origin_key, title="Original report", year=2001)
    follow = _paper(store, cite_key=follow_key, title="Follow-up", year=2005)
    attach_evidence(store, hub_ref_id=hub, paper_ref_id=origin, role="corroborates")
    attach_evidence(store, hub_ref_id=hub, paper_ref_id=follow, role="corroborates")
    store.add_link(src_ref_id=follow, dst_ref_id=origin, relation="cites")
    return hub, origin


def test_resolve_pin_handle_paper_handle(store: Any) -> None:
    ref_id = _paper(store, cite_key="rph01a", title="A paper")
    handle = handle_registry.format_handle("paper", ref_id)

    resolved = resolve_pin_handle(store, handle)

    assert resolved == (ref_id, "rph01a")


def test_resolve_pin_handle_passage_resolves_to_parent_paper(store: Any) -> None:
    ref_id = _paper(store, cite_key="rph02a", title="A paper")
    chunk_id = _paper_chunk(store, ref_id)

    resolved = resolve_pin_handle(store, f"pc{chunk_id}")

    assert resolved == (ref_id, "rph02a")


def test_resolve_pin_handle_unresolvable_returns_none(store: Any) -> None:
    assert resolve_pin_handle(store, "pa999999999") is None
    assert resolve_pin_handle(store, "not-a-handle") is None


def test_apply_pin_replace_uses_pinned_not_derived(store: Any) -> None:
    hub, _origin = _hub_with_derived_originator(
        store, origin_key="apc01a", follow_key="apf01a"
    )
    pinned = _paper(store, cite_key="apn01a", title="Author's pick")
    handle = handle_registry.format_handle("paper", pinned)
    evidence = derive_evidence(store, hub)

    result = apply_pin(
        store,
        label="fi1",
        op=">",
        handles=[handle],
        derived_cite_keys=["apc01a"],
        evidence=evidence,
    )

    assert result.cite_keys == ["apn01a"]
    assert result.diverged is True
    assert result.divergence is not None
    assert handle in result.divergence
    assert handle_registry.format_handle("paper", _origin) in result.divergence
    assert result.warnings == []


def test_apply_pin_replace_matching_derived_no_divergence(store: Any) -> None:
    hub, origin = _hub_with_derived_originator(
        store, origin_key="apc02a", follow_key="apf02a"
    )
    handle = handle_registry.format_handle("paper", origin)
    evidence = derive_evidence(store, hub)

    result = apply_pin(
        store,
        label="fi2",
        op=">",
        handles=[handle],
        derived_cite_keys=["apc02a"],
        evidence=evidence,
    )

    assert result.cite_keys == ["apc02a"]
    assert result.diverged is False
    assert result.divergence is None


def test_apply_pin_replace_empty_resolved_falls_back_with_warning(store: Any) -> None:
    hub, _origin = _hub_with_derived_originator(
        store, origin_key="apc03a", follow_key="apf03a"
    )
    evidence = derive_evidence(store, hub)

    result = apply_pin(
        store,
        label="fi3",
        op=">",
        handles=["pa999999999"],  # unresolvable
        derived_cite_keys=["apc03a"],
        evidence=evidence,
    )

    assert result.cite_keys == ["apc03a"]  # fell back to derived
    assert result.diverged is False
    # one warning for the unresolvable handle, one for the empty-replace
    # fallback itself.
    assert len(result.warnings) == 2
    assert all(status == "pin" for status, _detail in result.warnings)


def test_apply_pin_supplement_dedups_and_appends(store: Any) -> None:
    hub, origin = _hub_with_derived_originator(
        store, origin_key="apc04a", follow_key="apf04a"
    )
    extra = _paper(store, cite_key="apn04a", title="Extra evidence")
    dup_handle = handle_registry.format_handle("paper", origin)
    extra_handle = handle_registry.format_handle("paper", extra)
    evidence = derive_evidence(store, hub)

    result = apply_pin(
        store,
        label="fi4",
        op="+",
        handles=[dup_handle, extra_handle],
        derived_cite_keys=["apc04a"],
        evidence=evidence,
    )

    assert result.cite_keys == ["apc04a", "apn04a"]  # dedup + append, order kept
    assert result.diverged is False
    assert result.divergence is None
    assert result.warnings == []


def test_apply_pin_supplement_never_diverges_even_when_differing(store: Any) -> None:
    hub, _origin = _hub_with_derived_originator(
        store, origin_key="apc05a", follow_key="apf05a"
    )
    extra = _paper(store, cite_key="apn05a", title="Extra evidence")
    handle = handle_registry.format_handle("paper", extra)
    evidence = derive_evidence(store, hub)

    result = apply_pin(
        store,
        label="fi5",
        op="+",
        handles=[handle],
        derived_cite_keys=["apc05a"],
        evidence=evidence,
    )

    assert result.diverged is False
    assert result.divergence is None


# ── hub fallback: the citation standard over corroborators ──────────────
#
# No derived originator (no intra-set `cites` edge) → `hub_cite_keys` picks
# the earliest grounded+verified non-review corroborator + up to 2 independent
# confirmations; reviews only when no primary; one unverified corroborator
# when nothing is grounded+verified. Never the whole list.

_GROUNDED = {"support": "yes", "source_handle": "pc1"}


def _supporter(
    store: Any,
    hub: int,
    key: str,
    *,
    year: int | None,
    title: str = "A research result",
    authors: list[dict[str, Any]] | None = None,
    journal: str | None = None,
    meta: dict[str, Any] | None = None,
) -> int:
    """A paper attached to ``hub`` as a corroborator. ``meta`` is the edge meta
    (default: grounded + verified); pass ``{}`` for a born-withheld edge."""
    ref = store.insert_ref(
        kind="paper",
        slug=key,
        title=title,
        year=year,
        meta={"journal": journal} if journal else {},
        authors=authors,
    )
    attach_evidence(
        store,
        hub_ref_id=hub,
        paper_ref_id=ref.id,
        role="corroborates",
        meta=dict(_GROUNDED if meta is None else meta),
        check_retraction=False,
    )
    return int(ref.id)


def _au(*families: str) -> list[dict[str, Any]]:
    return [{"given": "X.", "family": f} for f in families]


def _fallback_keys(store: Any, hub: int) -> tuple[list[str], list[tuple[str, str]]]:
    return hub_cite_keys(store, derive_evidence(store, hub))


def test_fallback_only_grounded_verified_count(store: Any) -> None:
    hub = mint_hub(store, _CLAIM)
    _supporter(store, hub, "fbg01a", year=2001, meta={})  # withheld (no verdict)
    _supporter(store, hub, "fbg02a", year=2002, meta={"support": "yes"})  # ungrounded
    _supporter(
        store, hub, "fbg03a", year=2003, meta={"support": "no", "source_handle": "pc1"}
    )  # grounded but unsupported
    _supporter(store, hub, "fbg04a", year=2004)  # grounded + verified

    keys, notes = _fallback_keys(store, hub)

    assert keys == ["fbg04a"]
    assert any("primary + 0 independent confirmations" in d for _, d in notes)


def test_fallback_grounding_entry_without_edge_source_handle_counts(
    store: Any,
) -> None:
    """A paper→hub edge pinning ``src_chunk_id`` (no ``meta.source_handle``)
    surfaces in ``HubEvidence.grounding`` and still counts as grounded."""
    hub = mint_hub(store, _CLAIM)
    ref = store.insert_ref(
        kind="paper", slug="fbgr01a", title="Result", year=2001, meta={}
    )
    _paper_chunk(store, ref.id, ord=0)
    store.add_link(
        src_ref_id=ref.id,
        dst_ref_id=hub,
        relation="corroborates",
        src_pos=0,
        meta={"support": "yes"},
    )
    evidence = derive_evidence(store, hub)
    assert evidence.corroborators and evidence.corroborators[0].source_handle is None
    assert evidence.grounding

    keys, _notes = hub_cite_keys(store, evidence)

    assert keys == ["fbgr01a"]


def test_fallback_earliest_primary_beats_older_review(store: Any) -> None:
    hub = mint_hub(store, _CLAIM)
    _supporter(
        store, hub, "fbr01a", year=1999, title="Nanobuds: a review", authors=_au("Aa")
    )
    _supporter(
        store, hub, "fbr02a", year=2004, journal="Chemical Reviews", authors=_au("Bb")
    )
    _supporter(store, hub, "fbp03a", year=2006, authors=_au("Cc"))
    _supporter(store, hub, "fbp04a", year=2008, authors=_au("Dd"))

    keys, _notes = _fallback_keys(store, hub)

    assert keys == ["fbp03a", "fbp04a"]  # no review, primary first


def test_fallback_skips_shared_author_confirmation(store: Any) -> None:
    hub = mint_hub(store, _CLAIM)
    _supporter(store, hub, "fba01a", year=2001, authors=_au("Smith", "Jones"))
    _supporter(store, hub, "fba02a", year=2002, authors=_au("Müller", "JONES"))
    _supporter(store, hub, "fba03a", year=2003, authors=_au("Nguyen"))

    keys, notes = _fallback_keys(store, hub)

    assert keys == ["fba01a", "fba03a"]  # fba02a shares "Jones" (case-folded)
    assert any("primary + 1 independent confirmation)" in d for _, d in notes)


def test_fallback_author_overlap_handles_name_only_authors(store: Any) -> None:
    hub = mint_hub(store, _CLAIM)
    _supporter(store, hub, "fbn01a", year=2001, authors=[{"name": "Jane Q. Doe"}])
    _supporter(store, hub, "fbn02a", year=2002, authors=[{"name": "Doe, Janet"}])
    _supporter(store, hub, "fbn03a", year=2003, authors=[{"name": "Bob Roe"}])

    keys, _notes = _fallback_keys(store, hub)

    assert keys == ["fbn01a", "fbn03a"]


def test_fallback_caps_at_three(store: Any) -> None:
    hub = mint_hub(store, _CLAIM)
    for i, fam in enumerate(["Aa", "Bb", "Cc", "Dd", "Ee"]):
        _supporter(store, hub, f"fbc0{i}a", year=2001 + i, authors=_au(fam))

    keys, _notes = _fallback_keys(store, hub)

    assert keys == ["fbc00a", "fbc01a", "fbc02a"]


def test_fallback_missing_authors_counted_independent_with_note(store: Any) -> None:
    hub = mint_hub(store, _CLAIM)
    _supporter(store, hub, "fbm01a", year=2001, authors=_au("Aa"))
    _supporter(store, hub, "fbm02a", year=2002)  # no authors

    keys, notes = _fallback_keys(store, hub)

    assert keys == ["fbm01a", "fbm02a"]
    assert any("authors missing" in d for _, d in notes)


def test_fallback_year_none_sorts_last_and_ties_by_ref_id(store: Any) -> None:
    hub = mint_hub(store, _CLAIM)
    _supporter(store, hub, "fby01a", year=None, authors=_au("Aa"))
    _supporter(store, hub, "fby02a", year=2005, authors=_au("Bb"))
    _supporter(store, hub, "fby03a", year=2005, authors=_au("Cc"))

    keys, _notes = _fallback_keys(store, hub)

    assert keys == ["fby02a", "fby03a", "fby01a"]


def test_fallback_all_review_prints_one_review(store: Any) -> None:
    hub = mint_hub(store, _CLAIM)
    _supporter(store, hub, "fbv01a", year=2010, journal="Nature Reviews Materials")
    _supporter(store, hub, "fbv02a", year=2005, title="An overview of nanobuds")
    _supporter(store, hub, "fbv03a", year=2008, title="Progress in nanobuds")

    keys, notes = _fallback_keys(store, hub)

    assert keys == ["fbv02a"]
    assert any("earliest grounded verified review" in d for _, d in notes)


def test_fallback_nothing_verified_prints_one_unverified_corroborator(
    store: Any,
) -> None:
    hub = mint_hub(store, _CLAIM)
    _supporter(store, hub, "fbu01a", year=2004, meta={})
    _supporter(store, hub, "fbu02a", year=2002, meta={})
    _supporter(store, hub, "fbu03a", year=2003, meta={"support": "yes"})  # ungrounded

    keys, notes = _fallback_keys(store, hub)

    assert keys == ["fbu02a"]
    assert (
        "established",
        "resolved via 1 unverified corroborator — no grounded verified supporter",
    ) in notes


def test_fallback_excludes_retracted_candidate(store: Any) -> None:
    hub = mint_hub(store, _CLAIM)
    bad = _supporter(store, hub, "fbx01a", year=2001, authors=_au("Aa"))
    _supporter(store, hub, "fbx02a", year=2002, authors=_au("Bb"))
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET retraction_status = 'retracted' WHERE ref_id = %s", (bad,)
        )
        conn.commit()

    keys, _notes = _fallback_keys(store, hub)

    assert keys == ["fbx02a"]


def test_fallback_originator_still_wins(store: Any) -> None:
    hub, _origin = _hub_with_derived_originator(
        store, origin_key="fbo01a", follow_key="fbo02a"
    )
    # An extra grounded+verified corroborator must not leak in beside the
    # derived originator.
    _supporter(store, hub, "fbo03a", year=2003)

    keys, notes = _fallback_keys(store, hub)

    assert keys == ["fbo01a"]
    assert not any("resolved via" in d for _, d in notes)


def test_fallback_pin_still_overrides(store: Any) -> None:
    hub = mint_hub(store, _CLAIM)
    _supporter(store, hub, "fbq01a", year=2001, authors=_au("Aa"))
    pinned = _paper(store, cite_key="fbq09a", title="Pinned paper")
    evidence = derive_evidence(store, hub)
    keys, _notes = hub_cite_keys(store, evidence)

    result = apply_pin(
        store,
        label="fi1",
        op=">",
        handles=[handle_registry.format_handle("paper", pinned)],
        derived_cite_keys=keys,
        evidence=evidence,
    )

    assert keys == ["fbq01a"]
    assert result.cite_keys == ["fbq09a"]


def test_fallback_fetches_candidate_refs_in_one_call(store: Any) -> None:
    """A bulk caller's contract: one ``fetch_refs_by_ids`` for all candidates
    (none when ``paper_refs`` already covers them), never one per supporter."""
    hub = mint_hub(store, _CLAIM)
    for i in range(4):
        _supporter(store, hub, f"fbq1{i}a", year=2001 + i, authors=_au("Abcd"[i] * 3))
    evidence = derive_evidence(store, hub)
    cite_key_map = store.ref_cite_keys_bulk(
        [e.paper_ref_id for e in evidence.corroborators]
    )
    calls: list[list[int]] = []
    real = store.fetch_refs_by_ids

    class _Spy:
        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

        def fetch_refs_by_ids(self, ref_ids: Any, **kw: Any) -> Any:
            ids = list(ref_ids)
            calls.append(ids)
            return real(ids, **kw)

    spy: Any = _Spy(store)
    keys, _ = hub_cite_keys(spy, evidence, cite_key_map=cite_key_map)
    assert len(keys) == 3
    assert len(calls) == 1 and len(calls[0]) == 4

    calls.clear()
    prefetched = real([e.paper_ref_id for e in evidence.corroborators])
    hub_cite_keys(spy, evidence, cite_key_map=cite_key_map, paper_refs=prefetched)
    assert calls == []


def test_decision_record_primary_tier(store: Any) -> None:
    hub = mint_hub(store, _CLAIM)
    _supporter(
        store, hub, "fbd01a", year=1999, title="Nanobuds: a review", authors=_au("Aa")
    )
    _supporter(store, hub, "fbd02a", year=2001, authors=_au("Smith"))
    _supporter(store, hub, "fbd03a", year=2002, authors=_au("Smith"))
    _supporter(store, hub, "fbd04a", year=2003, authors=_au("Cc"))
    _supporter(store, hub, "fbd05a", year=2004, authors=_au("Dd"))
    _supporter(store, hub, "fbd06a", year=2005, authors=_au("Ee"))

    printed = resolve_hub_print(store, derive_evidence(store, hub))

    assert printed.tier == "primary" and printed.decision is not None
    d = printed.decision
    assert d.tier == "primary" and d.n_corroborators == 6
    by_key = {c.cite_key: c for c in d.candidates}
    assert by_key["fbd01a"].review_reason == "title ~ 'review'"
    assert by_key["fbd01a"].outcome == OUTCOME_REVIEW_HELD
    assert by_key["fbd02a"].review_reason is None
    assert by_key["fbd02a"].outcome == OUTCOME_FIRST
    assert by_key["fbd03a"].outcome == OUTCOME_SHARED_AUTHOR
    assert by_key["fbd04a"].outcome == OUTCOME_CONFIRMATION
    assert by_key["fbd05a"].outcome == OUTCOME_CONFIRMATION
    assert by_key["fbd06a"].outcome == OUTCOME_CAP
    # The record rides the notes too (one line, status `cite-fallback`).
    line = next(d_ for st, d_ in printed.notes if st == "cite-fallback")
    assert "fbd02a" in line and "REVIEW[title ~ 'review']" in line


def test_decision_record_review_tier(store: Any) -> None:
    hub = mint_hub(store, _CLAIM)
    _supporter(store, hub, "fbe01a", year=2010, journal="Chemical Reviews")
    _supporter(store, hub, "fbe02a", year=2005, title="An overview of nanobuds")

    printed = resolve_hub_print(store, derive_evidence(store, hub))

    assert printed.tier == "review" and printed.cite_keys == ["fbe02a"]
    assert printed.decision is not None
    by_key = {c.cite_key: c for c in printed.decision.candidates}
    assert by_key["fbe02a"].outcome == OUTCOME_REVIEW_PRINTED
    assert by_key["fbe01a"].outcome == OUTCOME_REVIEW_LATER
    assert by_key["fbe01a"].review_reason == "journal ~ 'Reviews'"


def test_decision_record_absent_for_originators(store: Any) -> None:
    hub, _origin = _hub_with_derived_originator(
        store, origin_key="fbz01a", follow_key="fbz02a"
    )

    printed = resolve_hub_print(store, derive_evidence(store, hub))

    assert printed.tier == "originator" and printed.decision is None
    assert not any(st == "cite-fallback" for st, _ in printed.notes)


def test_evidence_view_renders_citation_fallback_block(store: Any) -> None:
    hub = mint_hub(store, _CLAIM)
    _supporter(
        store, hub, "fbw01a", year=1999, title="Nanobuds: a review", authors=_au("Aa")
    )
    _supporter(store, hub, "fbw02a", year=2001, authors=_au("Bb"))

    body = _make_handler(store).get(id=hub, view="evidence").body

    assert "## citation fallback" in body
    assert "fbw02a" in body
    assert "review-like: title ~ 'review'" in body
    assert OUTCOME_FIRST in body and OUTCOME_REVIEW_HELD in body


def test_evidence_view_no_fallback_block_with_originator(store: Any) -> None:
    hub, _origin = _hub_with_derived_originator(
        store, origin_key="fbw11a", follow_key="fbw12a"
    )

    body = _make_handler(store).get(id=hub, view="evidence").body

    assert "## citation fallback" not in body
