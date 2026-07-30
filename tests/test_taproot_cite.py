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
from precis.taproot.cite import finding_cite_keys
from precis.taproot.hub import attach_evidence, mint_hub

_CLAIM = CanonicalClaim(
    sentence="Pd/C catalyzes Suzuki coupling at room temperature with a mild base.",
    scope={"material": "Pd/C", "method": "Suzuki coupling", "regime": "RT"},
)


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
    ref_id = int(re.search(r"id=(\d+)", resp.body).group(1))
    store.update_ref(ref_id, meta_patch={"primary_cite_key": "mlr23a"})

    result = finding_cite_keys(store, ref_id)

    assert result.is_hub is False
    assert result.inflight is False
    assert result.cite_keys == ["mlr23a"]


def test_finding_cite_keys_non_hub_pub_id_only(store: Any) -> None:
    _paper(store, cite_key="pnd01a", title="paper pnd01a")
    handler = _make_handler(store)
    resp = handler.put(title="t", body="b", scope={}, cited_in="pnd01a")
    ref_id = int(re.search(r"id=(\d+)", resp.body).group(1))
    pub_id = re.search(r"pub_id=(\w+)", resp.body).group(1)

    result = finding_cite_keys(store, ref_id)

    assert result.is_hub is False
    assert result.inflight is False
    assert result.cite_keys == [pub_id]
