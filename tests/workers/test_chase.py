"""Scenario tests for ``precis.workers.chase.run_finding_chase_pass``.

Each test seeds a finding + its frontier target in a specific
shape, runs one pass, and asserts the resulting (outcome, tag,
meta, links) tuple. Mocks ``_load_s2_references`` for any test that
needs S2 references — the chase worker itself is deterministic
otherwise.

Scenarios per the C5 design (`docs/design/finding-chase.md`):

  terminal       — no inline cites on the chunk → snapshot pass,
                   STATUS:established, card_combined re-emitted.
  stub_waiting   — frontier ref has zero chunks (still being
                   ingested) → "waiting" no-op, status unchanged.
  hop            — chunk has `[1]` + mocked S2 refs → chain grows
                   by one, derived-from link added.
  cycle          — next-hop target is already in the chain →
                   STATUS:cycle, no link added.
  dead_no_cite   — `_pick_next_hop` returns None because s2 refs
                   are absent → STATUS:dead_chain
                   reason=no_resolvable_cite.
  dead_no_extid  — next-hop target has no usable external ID →
                   STATUS:dead_chain reason=no_external_id.
  dead_deleted   — frontier ref soft-deleted →
                   STATUS:dead_chain reason=target_deleted.
  dead_empty     — finding's meta.chain is empty →
                   STATUS:dead_chain reason=empty_chain.
  multi          — two inline cites resolve to distinct targets →
                   STATUS:multi_candidate; candidate links recorded.
"""

from __future__ import annotations

import re
from typing import Any
from unittest.mock import patch

from precis.dispatch import Hub
from precis.handlers.finding import FindingHandler
from precis.store.types import BlockInsert
from precis.workers.chase import run_finding_chase_pass

# ── plumbing ────────────────────────────────────────────────────────


def _make_handler(store):
    return FindingHandler(hub=Hub(store=store))


def _seed_paper(
    store,
    *,
    cite_key: str,
    blocks: list[str] | None = None,
    identifiers: list[tuple[str, str]] | None = None,
) -> int:
    """Insert a minimal paper ref with optional chunks + external IDs."""
    ref = store.insert_ref(
        kind="paper",
        slug=cite_key,
        title=f"Test paper {cite_key}",
        meta={},
    )
    if blocks:
        store.insert_blocks(
            ref.id,
            [BlockInsert(pos=i, text=t, meta={}) for i, t in enumerate(blocks)],
        )
    if identifiers:
        with store.pool.connection() as conn:
            for id_kind, id_value in identifiers:
                conn.execute(
                    "INSERT INTO ref_identifiers "
                    "(id_kind, id_value, ref_id, source) "
                    "VALUES (%s, %s, %s, %s)",
                    (id_kind, id_value, ref.id, "test"),
                )
    return ref.id


def _seed_finding(
    store,
    *,
    cite_key: str = "miller23a",
    body: str = "claim body",
) -> int:
    """Create a finding pointing at a paper's frontier chunk."""
    h = _make_handler(store)
    resp = h.put(
        title="t",
        body=body,
        scope={"electrode": "Cu"},
        cited_in=cite_key,
    )
    return int(re.search(r"id=(\d+)", resp.body).group(1))


def _status_tag(store, ref_id: int) -> str | None:
    """Return the current STATUS value on a ref (or None)."""
    for t in store.tags_for(ref_id):
        if getattr(t, "namespace", None) == "closed" and t.prefix == "STATUS":
            return t.value
    return None


def _chain(store, ref_id: int) -> list[dict[str, Any]]:
    ref = store.get_ref_by_id(ref_id) if hasattr(store, "get_ref_by_id") else None
    if ref is None:
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT meta FROM refs WHERE ref_id = %s", (ref_id,)
            ).fetchone()
        return list((row[0] or {}).get("chain") or [])
    return list((ref.meta or {}).get("chain") or [])


# ── terminal: no inline cites → snapshot ─────────────────────────────


def test_terminal_no_inline_cites_establishes_chain(store) -> None:
    """A frontier chunk with no inline cites is the primary source;
    the chase snapshots the chain, sets ``primary_cite_key``, flips
    the status to ``established``, and re-emits ``card_combined``."""
    _seed_paper(
        store,
        cite_key="primary",
        blocks=["A direct measurement statement with no citations."],
    )
    fid = _seed_finding(store, cite_key="primary")

    result = run_finding_chase_pass(store, limit=10)
    assert result == {"claimed": 1, "ok": 1, "failed": 0}

    assert _status_tag(store, fid) == "established"

    with store.pool.connection() as conn:
        meta_row = conn.execute(
            "SELECT meta FROM refs WHERE ref_id = %s", (fid,)
        ).fetchone()
    meta = meta_row[0] or {}
    assert meta.get("primary_cite_key") == "primary"
    assert meta.get("via_cite_keys") == []

    # card_combined re-emitted at ord=-1.
    with store.pool.connection() as conn:
        card = conn.execute(
            "SELECT text FROM chunks WHERE ref_id = %s AND ord = -1",
            (fid,),
        ).fetchone()
    assert card is not None
    assert "primary=primary" in card[0]


# ── stub_waiting: frontier has no chunks ────────────────────────────


def test_stub_waiting_when_frontier_has_no_chunks(store) -> None:
    """A frontier ref with zero chunks (chase-minted stub waiting
    for its PDF) is a no-op pass — status stays tracing, no chain
    growth."""
    _seed_paper(store, cite_key="stubpaper", blocks=[])
    fid = _seed_finding(store, cite_key="stubpaper")

    result = run_finding_chase_pass(store, limit=10)
    assert result == {"claimed": 1, "ok": 1, "failed": 0}

    assert _status_tag(store, fid) == "tracing"
    assert len(_chain(store, fid)) == 1  # unchanged


# ── hop: inline cite + S2 reference → chain grows ──────────────────


def test_hop_advances_chain_by_one_and_adds_link(store) -> None:
    """A chunk with ``[1]`` inline cite + an S2 reference resolves
    to the next-hop ref; the chase appends to ``meta.chain`` and
    writes a ``derived-from`` link from the finding to the new ref."""
    _seed_paper(
        store,
        cite_key="frontier",
        blocks=["The device was held at 2.4 kV [1]."],
        identifiers=[("doi", "10.1/frontier")],
    )
    fid = _seed_finding(store, cite_key="frontier")

    # Mock S2 to return a single reference resolving to a target
    # we'll mint as a new stub.
    s2_refs = [{"doi": "10.1/primary", "title": "Primary measurement", "year": 2010}]

    with patch(
        "precis.workers.chase._load_s2_references",
        return_value=s2_refs,
    ):
        result = run_finding_chase_pass(store, limit=10)
    assert result == {"claimed": 1, "ok": 1, "failed": 0}

    assert _status_tag(store, fid) == "tracing"  # still in flight
    chain = _chain(store, fid)
    assert len(chain) == 2

    # New stub ref with the primary DOI registered.
    with store.pool.connection() as conn:
        new_id = conn.execute(
            "SELECT ref_id FROM ref_identifiers "
            "WHERE id_kind = 'doi' AND id_value = %s",
            ("10.1/primary",),
        ).fetchone()
    assert new_id is not None
    assert int(chain[-1]["ref_id"]) == int(new_id[0])

    # derived-from link from finding → new ref.
    links = store.links_for(fid, direction="out", relation="derived-from")
    assert any(l.dst_ref_id == int(new_id[0]) for l in links)


# ── cycle: next-hop would revisit an earlier chain entry ────────────


def test_cycle_protection_flags_status(store) -> None:
    """When the next hop resolves to a ref already in the chain,
    the chase tags ``STATUS:cycle`` and does not add a new link."""
    # Seed the cycle target up front with the doi so it appears in
    # the chain from put time.
    cycle_paper = _seed_paper(
        store,
        cite_key="frontier",
        blocks=["Held at 2.4 kV [1]."],
        identifiers=[("doi", "10.1/frontier")],
    )
    # The finding's initial chain is [frontier]. We mock S2 so the
    # next hop resolves *back* to the frontier ref by doi.
    fid = _seed_finding(store, cite_key="frontier")

    s2_refs = [{"doi": "10.1/frontier", "title": "Cycle target", "year": 2020}]
    with patch(
        "precis.workers.chase._load_s2_references",
        return_value=s2_refs,
    ):
        result = run_finding_chase_pass(store, limit=10)
    assert result == {"claimed": 1, "ok": 1, "failed": 0}

    assert _status_tag(store, fid) == "cycle"
    # Chain stayed at one entry.
    assert len(_chain(store, fid)) == 1
    # No new derived-from link to a non-cycle target.
    links = store.links_for(fid, direction="out", relation="derived-from")
    assert all(l.dst_ref_id == cycle_paper for l in links)


# ── dead_chain variants ─────────────────────────────────────────────


def test_dead_chain_when_no_resolvable_cite(store) -> None:
    """Inline cites present but no S2 references → can't resolve →
    dead_chain reason=no_resolvable_cite."""
    _seed_paper(
        store,
        cite_key="frontier",
        blocks=["Some claim [42]."],
        identifiers=[("doi", "10.1/frontier")],
    )
    fid = _seed_finding(store, cite_key="frontier")

    with patch(
        "precis.workers.chase._load_s2_references",
        return_value=None,
    ):
        result = run_finding_chase_pass(store, limit=10)
    assert result == {"claimed": 1, "ok": 1, "failed": 0}
    assert _status_tag(store, fid) == "dead_chain"


def test_dead_chain_when_target_soft_deleted(store) -> None:
    """A soft-deleted frontier ref is treated as dead_chain
    reason=target_deleted."""
    pid = _seed_paper(
        store,
        cite_key="frontier",
        blocks=["body"],
    )
    fid = _seed_finding(store, cite_key="frontier")
    store.soft_delete_ref(pid)

    result = run_finding_chase_pass(store, limit=10)
    assert result == {"claimed": 1, "ok": 1, "failed": 0}
    assert _status_tag(store, fid) == "dead_chain"


def test_dead_chain_when_meta_chain_empty(store) -> None:
    """An empty meta.chain (couldn't happen via put, but defensive
    against manual rows) terminates as dead_chain reason=empty_chain."""
    _seed_paper(store, cite_key="frontier", blocks=["body"])
    fid = _seed_finding(store, cite_key="frontier")
    # Stomp the chain meta.
    store.update_ref(fid, meta_patch={"chain": []})

    result = run_finding_chase_pass(store, limit=10)
    assert result == {"claimed": 1, "ok": 1, "failed": 0}
    assert _status_tag(store, fid) == "dead_chain"


# ── multi_candidate ─────────────────────────────────────────────────


def test_multi_candidate_tags_status_and_records_candidates(store) -> None:
    """Two inline cites resolving to distinct refs (no LLM, no
    automatic pick) → STATUS:multi_candidate plus a
    ``derived-from candidate=true`` link for each candidate."""
    _seed_paper(
        store,
        cite_key="frontier",
        blocks=["Held at 2.4 kV [1, 2]."],
        identifiers=[("doi", "10.1/frontier")],
    )
    fid = _seed_finding(store, cite_key="frontier")

    s2_refs = [
        {"doi": "10.1/cand-a", "title": "Candidate A", "year": 2018},
        {"doi": "10.1/cand-b", "title": "Candidate B", "year": 2019},
    ]
    with patch(
        "precis.workers.chase._load_s2_references",
        return_value=s2_refs,
    ):
        result = run_finding_chase_pass(store, limit=10)
    assert result == {"claimed": 1, "ok": 1, "failed": 0}

    assert _status_tag(store, fid) == "multi_candidate"

    # Both candidates linked with the candidate=true marker.
    links = store.links_for(fid, direction="out", relation="derived-from")
    candidate_links = [l for l in links if (l.meta or {}).get("candidate") is True]
    assert len(candidate_links) == 2


# ── card re-emit at chain termination ──────────────────────────────


def test_card_combined_reemits_at_chain_termination(store) -> None:
    """Termination DELETEs any prior ``card_combined`` row and
    INSERTs a fresh one carrying the primary cite_key. Exercised
    against an existing card so we see the swap."""
    _seed_paper(
        store,
        cite_key="primary",
        blocks=["A direct measurement."],
    )
    fid = _seed_finding(store, cite_key="primary")

    # Plant a stale card_combined as if from a prior pass — chase
    # must replace it.
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO chunks (ref_id, ord, chunk_kind, text, meta) "
            "VALUES (%s, -1, 'card_combined', %s, '{}'::jsonb)",
            (fid, "STALE CARD"),
        )

    run_finding_chase_pass(store, limit=10)

    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT text FROM chunks WHERE ref_id = %s AND ord = -1",
            (fid,),
        ).fetchall()
    assert len(rows) == 1
    assert "STALE CARD" not in rows[0][0]
    assert "primary=primary" in rows[0][0]
