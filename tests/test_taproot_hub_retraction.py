"""Trigger 1 of the demand-driven retraction model
(``docs/backlog/retraction-check-triggers.md``): ``attach_evidence``, the
single write door for a ``paper --role--> hub`` evidence edge, opportunistically
calls ``precis.ingest.provenance.check_ref_retraction`` after the edge lands.

DB-backed (real ``refs``/``chunks``/``links`` via the ``store`` fixture, the
same idiom as ``test_taproot_hub.py``); ``check_ref_retraction`` itself is
monkeypatched everywhere here so nothing in this file ever touches the
network.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.taproot.canon import CanonicalClaim
from precis.taproot.hub import attach_evidence, mint_hub
from tests.workers._helpers import seed_ref

_CLAIM = CanonicalClaim(
    sentence="Pd/C catalyzes Suzuki coupling at room temperature with a mild base.",
    scope={"material": "Pd/C", "method": "Suzuki coupling", "regime": "RT"},
)


def _edge(store: Any, src: int, dst: int) -> str | None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT relation FROM links WHERE src_ref_id = %s AND dst_ref_id = %s",
            (src, dst),
        ).fetchone()
    return row[0] if row else None


@pytest.fixture
def fake_check(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Monkeypatch ``check_ref_retraction`` as seen from ``taproot.hub``.

    Records every ``ref_id`` it was called with (no network — the fake
    just returns without doing anything) so tests can assert invocation.
    """
    calls: list[int] = []

    def _fake(store: Any, ref_id: int, **kwargs: Any) -> None:
        calls.append(ref_id)

    monkeypatch.setattr("precis.taproot.hub.check_ref_retraction", _fake)
    return calls


def test_attach_evidence_checks_retraction_for_paper_source(
    store: Any, fake_check: list[int]
) -> None:
    hub = mint_hub(store, _CLAIM)
    paper = seed_ref(store, title="Collins 2006", kind="paper")

    attach_evidence(store, hub_ref_id=hub, paper_ref_id=paper, role="establishes")

    assert fake_check == [paper]


def test_attach_evidence_skips_retraction_check_when_opted_out(
    store: Any, fake_check: list[int]
) -> None:
    hub = mint_hub(store, _CLAIM)
    paper = seed_ref(store, title="Collins 2006", kind="paper")

    attach_evidence(
        store,
        hub_ref_id=hub,
        paper_ref_id=paper,
        role="establishes",
        check_retraction=False,
    )

    assert fake_check == []
    # The edge itself is unaffected by the opt-out.
    assert _edge(store, paper, hub) == "establishes"


def test_attach_evidence_skips_retraction_check_for_patent_source(
    store: Any, fake_check: list[int]
) -> None:
    hub = mint_hub(store, _CLAIM)
    patent = seed_ref(store, title="EP1 probe", kind="patent")

    attach_evidence(store, hub_ref_id=hub, paper_ref_id=patent, role="corroborates")

    # A patent has no DOI to check against Crossref — never called.
    assert fake_check == []
    assert _edge(store, patent, hub) == "corroborates"


def test_attach_evidence_survives_a_retraction_check_exception(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed check must NEVER fail the attach — the edge is the durable
    thing, the check is opportunistic (docs/backlog/retraction-check-triggers.md)."""

    def _boom(store: Any, ref_id: int, **kwargs: Any) -> None:
        raise RuntimeError("Crossref is down")

    monkeypatch.setattr("precis.taproot.hub.check_ref_retraction", _boom)

    hub = mint_hub(store, _CLAIM)
    paper = seed_ref(store, title="Collins 2006", kind="paper")

    # No exception propagates out of attach_evidence.
    attach_evidence(store, hub_ref_id=hub, paper_ref_id=paper, role="establishes")

    # The edge landed regardless of the checker blowing up.
    assert _edge(store, paper, hub) == "establishes"
