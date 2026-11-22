"""Identifier collapse must not hide the title corruption in gr710296."""

import pytest

from precis.errors import Conflict
from precis.store import Store

_WANTED = "Catalytic reactions within the cavity of coordination cages"
_STORED = (
    "Origin of the electrocatalytic activity in carbon nanotube fiber "
    "counter-electrodes for solar-energy conversion"
)


@pytest.mark.parametrize(
    ("kind", "stored_id", "requested_id"),
    [
        ("doi", "10.1039/c9cs00091g", "10.1039/c9cs00091g"),
        ("s2", "identity-collapse-test", "identity-collapse-test"),
        ("doi", "10.1002/anie.202000001", "10.1002/ange.202000001"),
    ],
)
def test_identifier_title_conflict_preserves_row(
    store: Store, kind: str, stored_id: str, requested_id: str
) -> None:
    ref_id, _ = store.upsert_stub_paper(identifiers=[(kind, stored_id)], title=_STORED)
    with pytest.raises(Conflict, match=f"pa{ref_id}") as caught:
        store.upsert_stub_paper(identifiers=[(kind, requested_id)], title=_WANTED)
    assert "repair" in str(caught.value)
    ref = store.get_ref(kind="paper", id=ref_id)
    assert ref is not None
    assert ref.title == _STORED
    assert store.find_ref_by_identifier(kind, stored_id, kind="paper") == ref_id
    if requested_id != stored_id:
        assert store.find_ref_by_identifier(kind, requested_id, kind="paper") is None


@pytest.mark.parametrize("title", [None, "(no title)", _WANTED, "Coordination cages"])
def test_identifier_match_with_insufficient_or_agreeing_title(
    store: Store, title: str | None
) -> None:
    ref_id, _ = store.upsert_stub_paper(
        identifiers=[("doi", "10.1/title-agreement")], title=_WANTED
    )
    assert store.upsert_stub_paper(
        identifiers=[("doi", "10.1/title-agreement")], title=title
    ) == (ref_id, False)


# ── gr711568: arxiv:<id> ↔ 10.48550/arXiv.<id> — one preprint, two spellings


def test_datacite_doi_ask_collapses_onto_arxiv_stub(store: Store) -> None:
    """The pa711567/pa159949 split: a ``put(doi='10.48550/arXiv.<id>')``
    minted a duplicate next to the existing arXiv-row stub. The derived
    alias probe must find the held stub and leave the DOI spelling
    registered on it."""
    ref_id, _ = store.upsert_stub_paper(identifiers=[("arxiv", "2607.03433")])
    again_id, created = store.upsert_stub_paper(
        identifiers=[("doi", "10.48550/arXiv.2607.03433")]
    )
    assert (again_id, created) == (ref_id, False)
    assert (
        store.find_ref_by_identifier("doi", "10.48550/arxiv.2607.03433", kind="paper")
        == ref_id
    )


def test_arxiv_ask_collapses_onto_datacite_doi_stub(store: Store) -> None:
    """The reverse spelling pair: a stub minted under the DataCite DOI
    carries the derived arXiv row from birth (the mint registers the
    expansion), so the bare-arXiv ask collapses directly."""
    ref_id, _ = store.upsert_stub_paper(
        identifiers=[("doi", "10.48550/arXiv.2401.00077")]
    )
    again_id, created = store.upsert_stub_paper(identifiers=[("arxiv", "2401.00077v2")])
    assert (again_id, created) == (ref_id, False)
    assert store.find_ref_by_identifier("arxiv", "2401.00077", kind="paper") == ref_id


def test_arxiv_mint_registers_no_datacite_doi_row(store: Store) -> None:
    """The derived DOI is a probe/fold device, not a row of its own:
    a fresh arXiv mint does not acquire a ``doi`` identifier — the DOI
    lands on the row when a DOI-spelled ask actually converges."""
    ref_id, _ = store.upsert_stub_paper(identifiers=[("arxiv", "2401.00077")])
    assert (
        store.find_ref_by_identifier("doi", "10.48550/arxiv.2401.00077", kind="paper")
        is None
    )
