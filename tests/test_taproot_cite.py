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
from precis.taproot.cite import apply_pin, finding_cite_keys, resolve_pin_handle
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
