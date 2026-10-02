"""``precis.nanopub.overview.hub_rows`` — the claim-hub definition it must
agree with ``taproot.canon.block``/``hub.mint_hub`` on
(docs/backlog/claim-hub-definition-divergence.md). DB-backed via the
``store`` fixture; no LLM."""

from __future__ import annotations

from typing import Any

from precis.nanopub.overview import hub_rows
from precis.store.types import Tag
from precis.taproot.canon import CanonicalClaim
from precis.taproot.hub import mint_hub
from tests.workers._helpers import seed_ref


def test_hub_rows_excludes_taproot_claim_without_status_canonical(store: Any) -> None:
    """A finding carrying ``TAPROOT:claim`` but ``STATUS:established`` (a
    chase-tree finding mid-lifecycle, never minted through ``mint_hub``) is
    not a hub and must not appear in the queue table with a publish
    posture it can never have."""
    chase_ref = seed_ref(store, title="chase finding", kind="finding")
    store.add_tag(chase_ref, Tag.closed("TAPROOT", "claim"), set_by="system")
    store.add_tag(chase_ref, Tag.closed("STATUS", "established"), set_by="system")

    rows = hub_rows(store)

    assert chase_ref not in [r.ref_id for r in rows]


def test_hub_rows_includes_a_properly_minted_hub(store: Any) -> None:
    """A properly minted hub (``TAPROOT:claim`` + ``STATUS:canonical``) is
    still returned, unminted (no publish row)."""
    hub = mint_hub(store, CanonicalClaim(sentence="a minted hub claim", scope={}))

    rows = hub_rows(store)

    ids = [r.ref_id for r in rows]
    assert hub in ids
    row = next(r for r in rows if r.ref_id == hub)
    assert row.state is None


# ── ``tagline`` (precis.workers.hub_tagline) — presentation metadata on
# ``refs.meta``, threaded onto the overview row for the /nanopub forest. ──


def test_hub_rows_tagline_none_when_unset(store: Any) -> None:
    hub = mint_hub(store, CanonicalClaim(sentence="a taglineless hub claim", scope={}))

    rows = hub_rows(store)

    row = next(r for r in rows if r.ref_id == hub)
    assert row.tagline is None


def test_hub_rows_carries_tagline_from_meta(store: Any) -> None:
    hub = mint_hub(store, CanonicalClaim(sentence="a tagline-bearing claim", scope={}))
    store.update_ref(hub, meta_patch={"tagline": "Pd/C is Suzuki catalyst"})

    rows = hub_rows(store)

    row = next(r for r in rows if r.ref_id == hub)
    assert row.tagline == "Pd/C is Suzuki catalyst"


# ── ``open_disputes_count`` (D1, docs/backlog/
# disputes-edge-nonblocking-disagreement.md) — the non-blocking
# `disputes` complement to `disputed`/`disputed_since` above. ──────────


def test_hub_rows_open_disputes_count_counts_both_directions(store: Any) -> None:
    hub = mint_hub(store, CanonicalClaim(sentence="a disputed-open claim", scope={}))
    inbound = seed_ref(store, title="questions this claim", kind="finding")
    outbound = seed_ref(store, title="this claim questions that one", kind="finding")
    store.add_link(src_ref_id=inbound, dst_ref_id=hub, relation="disputes")
    store.add_link(src_ref_id=hub, dst_ref_id=outbound, relation="disputes")

    rows = hub_rows(store)

    row = next(r for r in rows if r.ref_id == hub)
    assert row.open_disputes_count == 2


def test_hub_rows_open_disputes_never_sets_disputed(store: Any) -> None:
    """`disputes` is the non-blocking open question — a hub touched only
    by a live `disputes` edge must NOT read `disputed`/`disputed_since`,
    which key on the adjudicated, blocking `contradicts` shape alone."""
    hub = mint_hub(
        store, CanonicalClaim(sentence="another open-question claim", scope={})
    )
    other = seed_ref(store, title="an open question", kind="finding")
    store.add_link(src_ref_id=other, dst_ref_id=hub, relation="disputes")

    rows = hub_rows(store)

    row = next(r for r in rows if r.ref_id == hub)
    assert row.open_disputes_count == 1
    assert row.disputed is False
    assert row.disputed_since is None


# ── compound roll-up (docs/backlog/compound-hub-posture-ignores-conjunct-
# evidence.md) — a compound hub carries no evidence edges of its own; its
# atoms (``atom --conjunct-of--> compound``) do. ───────────────────────


def _compound(store: Any, n_atoms: int = 3) -> tuple[int, list[int]]:
    compound = mint_hub(store, CanonicalClaim(sentence="a compound claim", scope={}))
    atoms = [
        mint_hub(store, CanonicalClaim(sentence=f"atom claim number {i}", scope={}))
        for i in range(n_atoms)
    ]
    for a in atoms:
        store.add_link(src_ref_id=a, dst_ref_id=compound, relation="conjunct-of")
    return compound, atoms


def _support(store: Any, atom: int, *, support: str = "yes") -> None:
    paper = seed_ref(store, title=f"supporter of {atom}", kind="paper")
    store.add_link(
        src_ref_id=paper,
        dst_ref_id=atom,
        relation="corroborates",
        meta={"support": support},
    )


def _row(store: Any, ref_id: int) -> Any:
    return next(r for r in hub_rows(store) if r.ref_id == ref_id)


def test_compound_rolls_up_supported_atoms_and_passes_verified(store: Any) -> None:
    from precis.handlers.finding import _passes_trust

    compound, atoms = _compound(store)
    for a in atoms:
        _support(store, a)

    row = _row(store, compound)

    assert row.supported_count == 0  # no direct edges — unchanged meaning
    assert (row.conjunct_count, row.conjuncts_supported) == (3, 3)
    assert row.conjuncts_contradicted == 0
    assert row.atoms_all_supported
    assert _passes_trust(row, "verified")


def test_compound_with_an_unsupported_atom_fails_verified(store: Any) -> None:
    from precis.handlers.finding import _passes_trust

    compound, atoms = _compound(store)
    _support(store, atoms[0])
    _support(store, atoms[1])
    _support(store, atoms[2], support="no")  # judged, supports nothing

    row = _row(store, compound)

    assert (row.conjunct_count, row.conjuncts_supported) == (3, 2)
    assert not row.atoms_all_supported
    assert not _passes_trust(row, "verified")


def test_compound_with_a_contradicted_atom_fails_verified(store: Any) -> None:
    from precis.handlers.finding import _passes_trust

    compound, atoms = _compound(store)
    for a in atoms:
        _support(store, a)
    rival = seed_ref(store, title="a rival claim", kind="finding")
    store.add_link(src_ref_id=rival, dst_ref_id=atoms[0], relation="contradicts")

    row = _row(store, compound)

    assert row.conjuncts_supported == 3
    assert row.conjuncts_contradicted == 1
    assert not row.atoms_all_supported
    assert not _passes_trust(row, "verified")


def test_compound_roll_up_ignores_retired_atoms(store: Any) -> None:
    compound, atoms = _compound(store)
    _support(store, atoms[0])
    _support(store, atoms[1])
    store.retire_ref(atoms[2])  # the unsupported one is gone

    row = _row(store, compound)

    assert (row.conjunct_count, row.conjuncts_supported) == (2, 2)
    assert row.atoms_all_supported


def test_atomic_hub_has_no_roll_up_and_keeps_its_own_trust(store: Any) -> None:
    from precis.handlers.finding import _passes_trust

    atom = mint_hub(store, CanonicalClaim(sentence="a lone atomic claim", scope={}))
    bare = _row(store, atom)
    assert (bare.conjunct_count, bare.conjuncts_supported) == (0, 0)
    assert not bare.atoms_all_supported
    assert not _passes_trust(bare, "verified")

    _support(store, atom)
    row = _row(store, atom)
    assert row.supported_count == 1
    assert row.conjunct_count == 0
    assert _passes_trust(row, "verified")


def test_compound_posture_cells_show_the_atom_roll_up(store: Any) -> None:
    from precis.handlers.finding import _posture_cells, _search_hit_posture

    compound, atoms = _compound(store)
    for a in atoms:
        _support(store, a)

    row = _row(store, compound)

    assert _posture_cells(row)["support"] == "atoms 3/3✓"
    assert _posture_cells(row)["flags"] == ""
    assert _search_hit_posture(row) == "◆ atoms 3/3✓ unopposed"
