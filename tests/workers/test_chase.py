"""Scenario tests for ``precis.workers.chase.run_finding_chase_pass``.

Each test seeds a finding + its frontier target in a specific
shape, runs one pass, and asserts the resulting (outcome, tag,
meta, links) tuple. Mocks ``_load_s2_references`` for any test that
needs S2 references — the chase worker itself is deterministic
otherwise.

Scenarios per the C5 design (`docs/backlog/finding-chase.md`):

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
from precis.store.types import BlockInsert, Tag
from precis.workers.chase import (
    _fetch_ref,
    _waiting_run_stats,
    claim_tracing_findings,
    run_finding_chase_pass,
)

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
        store.blocks.insert_blocks(
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
    id_m = re.search(r"id=(\d+)", resp.body)
    assert id_m is not None, f"create-ack missing id=; got {resp.body!r}"
    return int(id_m.group(1))


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


def test_fetch_ref_survives_multiple_cite_keys(store) -> None:
    """A ref with >1 cite_key (a dedup-merge — the PK is (id_kind,id_value),
    not (ref_id,id_kind)) must not raise CardinalityViolation in _fetch_ref's
    scalar subquery. Regression for the prod fetch_oa/chase/papers bug class."""
    ref_id = _seed_paper(
        store, cite_key="dup2024", identifiers=[("cite_key", "dup2024b")]
    )
    with store.pool.connection() as conn:
        row = _fetch_ref(conn, ref_id)  # must not raise
    assert row is not None
    assert row["ref_id"] == ref_id
    assert row["slug"] == "dup2024"  # min() of the two cite_keys


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


# ── waiting backoff: claim skips recently-waiting findings ──────────


def _insert_chase_event(store, ref_id: int, event: str, *, minutes_ago: float) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO ref_events (ref_id, source, event, payload, ts) "
            "VALUES (%s, 'chase', %s, '{}'::jsonb, "
            "now() - (%s || ' minutes')::interval)",
            (ref_id, event, str(minutes_ago)),
        )
        conn.commit()


def test_claim_skips_recently_waiting_finding(store) -> None:
    """A finding whose latest chase event is a recent ``waiting`` is
    excluded from the claim — this is what stops the per-pass spin
    loop on a chunk-less frontier stub."""
    _seed_paper(store, cite_key="stub_backoff", blocks=[])
    fid = _seed_finding(store, cite_key="stub_backoff")
    _insert_chase_event(store, fid, "waiting", minutes_ago=5)

    with store.pool.connection() as conn:
        claimed = claim_tracing_findings(conn, limit=10)
        conn.commit()
    assert fid not in [f.ref_id for f in claimed]


def _add_tag(store, ref_id: int, namespace: str, value: str) -> None:
    with store.pool.connection() as conn:
        store.add_tag(ref_id, Tag.closed(namespace, value), set_by="agent", conn=conn)
        conn.commit()


def test_claim_includes_axis_classified_claim_finding(store) -> None:
    """The ``axis:taproot`` classifier stamps ``TAPROOT:claim`` +
    ``TAPROOTCASCADE:1`` onto a live ``STATUS:tracing`` finding. Unlike a
    real ``mint_hub`` hub, that finding owns a real chase chain and MUST
    stay claimable — excluding it froze the Malthus-draft claims (neither
    chased nor canonical; OPEN-ITEMS §axis:taproot promote-and-freeze).
    The marker's presence is what distinguishes it from a mint_hub hub."""
    _seed_paper(store, cite_key="axisclaim", blocks=["a sourced statement."])
    fid = _seed_finding(store, cite_key="axisclaim")
    _add_tag(store, fid, "TAPROOT", "claim")
    _add_tag(store, fid, "TAPROOTCASCADE", "1")

    with store.pool.connection() as conn:
        claimed = claim_tracing_findings(conn, limit=10)
        conn.commit()
    assert fid in [f.ref_id for f in claimed]


def test_claim_excludes_real_mint_hub_claim(store) -> None:
    """A real ``mint_hub`` claim hub carries ``TAPROOT:claim`` with NO
    ``TAPROOTCASCADE`` marker. Even mis-statused ``STATUS:tracing`` (gripe
    175806) it must stay OUT of the claim so it can't re-claim + die as an
    empty-chain ``dead_chain`` every pass. Guards the narrowed exclusion
    from over-correcting."""
    _seed_paper(store, cite_key="realhub", blocks=["x."])
    fid = _seed_finding(store, cite_key="realhub")
    _add_tag(store, fid, "TAPROOT", "claim")  # no TAPROOTCASCADE marker

    with store.pool.connection() as conn:
        claimed = claim_tracing_findings(conn, limit=10)
        conn.commit()
    assert fid not in [f.ref_id for f in claimed]


def test_claim_reclaims_after_backoff_window(store) -> None:
    """Once the waiting event ages past the backoff window the finding
    is eligible again — the backoff is a throttle, not a kill."""
    _seed_paper(store, cite_key="stub_aged", blocks=[])
    fid = _seed_finding(store, cite_key="stub_aged")
    _insert_chase_event(store, fid, "waiting", minutes_ago=90)

    with store.pool.connection() as conn:
        claimed = claim_tracing_findings(conn, limit=10)
        conn.commit()
    assert fid in [f.ref_id for f in claimed]


def test_claim_not_suppressed_when_last_event_advanced(store) -> None:
    """A recent ``waiting`` doesn't suppress a finding that has since
    advanced — only the *most recent* outcome being ``waiting`` backs
    it off, so real progress keeps moving."""
    _seed_paper(store, cite_key="stub_moved", blocks=[])
    fid = _seed_finding(store, cite_key="stub_moved")
    _insert_chase_event(store, fid, "waiting", minutes_ago=10)
    _insert_chase_event(store, fid, "advanced", minutes_ago=1)

    with store.pool.connection() as conn:
        claimed = claim_tracing_findings(conn, limit=10)
        conn.commit()
    assert fid in [f.ref_id for f in claimed]


def test_claim_backoff_window_widens_exponentially(store) -> None:
    """Consecutive ``waiting`` outcomes widen the window: three waits in
    a row mean the base 60-min window has grown to 60*2^2 = 240 min, so
    a finding last-waiting 90 min ago — eligible under a flat window —
    is still suppressed."""
    _seed_paper(store, cite_key="stub_exp", blocks=[])
    fid = _seed_finding(store, cite_key="stub_exp")
    _insert_chase_event(store, fid, "waiting", minutes_ago=300)
    _insert_chase_event(store, fid, "waiting", minutes_ago=180)
    _insert_chase_event(store, fid, "waiting", minutes_ago=90)

    with store.pool.connection() as conn:
        claimed = claim_tracing_findings(conn, limit=10)
        conn.commit()
    assert fid not in [f.ref_id for f in claimed]


def test_claim_reclaims_after_widened_window(store) -> None:
    """Past the widened window the finding is eligible again: three
    waits give a 240-min window, and a most-recent wait 300 min ago is
    aged out."""
    _seed_paper(store, cite_key="stub_exp_aged", blocks=[])
    fid = _seed_finding(store, cite_key="stub_exp_aged")
    _insert_chase_event(store, fid, "waiting", minutes_ago=600)
    _insert_chase_event(store, fid, "waiting", minutes_ago=450)
    _insert_chase_event(store, fid, "waiting", minutes_ago=300)

    with store.pool.connection() as conn:
        claimed = claim_tracing_findings(conn, limit=10)
        conn.commit()
    assert fid in [f.ref_id for f in claimed]


def test_claim_backoff_count_resets_after_progress(store) -> None:
    """A non-waiting outcome resets the run: a single fresh ``waiting``
    after an ``advanced`` is back to the base 60-min window, not the
    widened one — so the prior waits don't keep a moving chain
    suppressed longer than base."""
    _seed_paper(store, cite_key="stub_reset", blocks=[])
    fid = _seed_finding(store, cite_key="stub_reset")
    # Old run of waits, then progress, then one fresh wait aged past base.
    _insert_chase_event(store, fid, "waiting", minutes_ago=500)
    _insert_chase_event(store, fid, "waiting", minutes_ago=400)
    _insert_chase_event(store, fid, "advanced", minutes_ago=300)
    _insert_chase_event(store, fid, "waiting", minutes_ago=90)

    with store.pool.connection() as conn:
        claimed = claim_tracing_findings(conn, limit=10)
        conn.commit()
    # waits-since-progress == 1 → base 60-min window → 90 min ago is eligible.
    assert fid in [f.ref_id for f in claimed]


def test_claim_survives_huge_waiting_run(store) -> None:
    """A finding stuck in ``waiting`` for thousands of cycles must not
    crash the claim. ``2^(waits-1)`` overflows double precision once
    ``waits`` passes ~1024, and Postgres raises ``value out of range``,
    which previously killed the whole chase pass every loop on every
    node (and blocked the give-up path that would have abandoned the
    finding). The exponent is clamped so POWER can never overflow."""
    _seed_paper(store, cite_key="stub_overflow", blocks=[])
    fid = _seed_finding(store, cite_key="stub_overflow")
    # 2000 consecutive waiting events — exponent 1999 overflows double
    # without the clamp. Most recent is recent, so it stays suppressed;
    # the point of the test is that the query returns instead of raising.
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO ref_events (ref_id, source, event, payload, ts) "
            "SELECT %s, 'chase', 'waiting', '{}'::jsonb, "
            "now() - (g || ' minutes')::interval "
            "FROM generate_series(1, 2000) AS g",
            (fid,),
        )
        conn.commit()

    with store.pool.connection() as conn:
        claimed = claim_tracing_findings(conn, limit=10)
        conn.commit()
    # No NumericValueOutOfRange raised; window is pinned at cap so the
    # most-recent (1-min-ago) wait keeps the finding suppressed.
    assert fid not in [f.ref_id for f in claimed]


# ── claim excludes taproot hubs (gripe 175806) ───────────────────────


def test_claim_excludes_taproot_claim_findings(store) -> None:
    """A minted taproot hub is itself a `finding` carrying STATUS:tracing
    (``mint_hub``'s "no resolved originators yet" tag) but is not a
    chase-owned chain -- the claim query must not re-select it, or it just
    dies as an empty-chain ``dead_chain`` every pass, wasting a claim slot
    and polluting dead_chain telemetry (gripe 175806)."""
    from precis.store.types import Tag
    from precis.taproot.canon import TAPROOT_CLAIM, TAPROOT_NAMESPACE

    _seed_paper(store, cite_key="hubpaper", blocks=["text"])
    fid = _seed_finding(store, cite_key="hubpaper")
    store.add_tag(
        fid,
        Tag.closed(TAPROOT_NAMESPACE, TAPROOT_CLAIM),
        set_by="chase",
        replace_prefix=True,
    )

    with store.pool.connection() as conn:
        claimed = claim_tracing_findings(conn, limit=10)
        conn.commit()
    assert fid not in [f.ref_id for f in claimed]


# ── terminal give-up: starve past WAITING_ABANDON_AFTER_DAYS ────────


def test_waiting_abandoned_after_long_starvation(store) -> None:
    """A finding that has been *continuously* waiting on a chunk-less
    frontier for longer than ``WAITING_ABANDON_AFTER_DAYS`` is given up:
    the pass flips STATUS:tracing → dead_chain so it leaves the pool
    instead of re-polling ~once a day forever."""
    _seed_paper(store, cite_key="stub_starved", blocks=[])
    fid = _seed_finding(store, cite_key="stub_starved")
    # Consecutive waiting run that began > 14 days ago; the most recent
    # wait is old enough (> 24h cap) to be claimable again.
    _insert_chase_event(store, fid, "waiting", minutes_ago=15 * 1440)
    _insert_chase_event(store, fid, "waiting", minutes_ago=2 * 1440)

    result = run_finding_chase_pass(store, limit=10)
    assert result == {"claimed": 1, "ok": 1, "failed": 0}
    assert _status_tag(store, fid) == "dead_chain"


def test_waiting_not_abandoned_before_threshold(store) -> None:
    """A finding only a few days into waiting is still re-polled, not
    abandoned — the give-up is for genuine multi-week starvation only."""
    _seed_paper(store, cite_key="stub_young", blocks=[])
    fid = _seed_finding(store, cite_key="stub_young")
    # 3-day-old run, most recent wait past the cap so it's claimable.
    _insert_chase_event(store, fid, "waiting", minutes_ago=3 * 1440)
    _insert_chase_event(store, fid, "waiting", minutes_ago=int(1.5 * 1440))

    result = run_finding_chase_pass(store, limit=10)
    assert result == {"claimed": 1, "ok": 1, "failed": 0}
    assert _status_tag(store, fid) == "tracing"


def test_waiting_run_stats_age_not_count(store) -> None:
    """A dense burst of waits in a short window (the pre-fix spin-loop
    shape) has a high count but low age — age is what gates give-up, so
    such a burst is *not* abandoned."""
    _seed_paper(store, cite_key="stub_burst", blocks=[])
    fid = _seed_finding(store, cite_key="stub_burst")
    # 200 waits all within the last ~3 hours: count is huge, age tiny.
    for i in range(200):
        _insert_chase_event(store, fid, "waiting", minutes_ago=180 - i * 0.5)

    with store.pool.connection() as conn:
        waits, age_days = _waiting_run_stats(conn, fid)
    assert waits == 200
    assert age_days < 1.0


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


def test_hop_mint_carries_s2_meta_when_reference_dict_has_it(store) -> None:
    """When the S2 reference dict carries abstract/fields/citation_count
    (beyond ``_load_s2_references``'s usual doi/title/year shape), the
    freshly-minted next-hop stub's ``refs.meta`` gets the mint-time S2
    patch (``s2_enriched_at`` + abstract) — so ``stub_rank`` skips the
    redundant enrich round-trip for it."""
    _seed_paper(
        store,
        cite_key="frontier2",
        blocks=["The device was held at 2.4 kV [1]."],
        identifiers=[("doi", "10.1/frontier2")],
    )
    fid = _seed_finding(store, cite_key="frontier2")

    s2_refs = [
        {
            "doi": "10.1/primary-rich",
            "title": "Primary measurement, richly described",
            "year": 2010,
            "abstract": "the primary's abstract",
            "fields": ["Physics"],
            "citation_count": 12,
        }
    ]
    with patch(
        "precis.workers.chase._load_s2_references",
        return_value=s2_refs,
    ):
        result = run_finding_chase_pass(store, limit=10)
    assert result == {"claimed": 1, "ok": 1, "failed": 0}

    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT r.meta FROM refs r JOIN ref_identifiers ri "
            "ON ri.ref_id = r.ref_id "
            "WHERE ri.id_kind = 'doi' AND ri.id_value = %s",
            ("10.1/primary-rich",),
        ).fetchone()
    assert row is not None
    meta = dict(row[0] or {})
    assert meta.get("s2_enriched_at") is not None
    assert meta.get("abstract") == "the primary's abstract"
    assert meta.get("s2_fields") == ["Physics"]
    assert meta.get("s2_citation_count") == 12
    assert meta.get("set_by") == "chase"


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


# ── acquisition mode (the acquiring-finding chase arm) ───────────────
#
# All of these go through run_finding_chase_pass (the worker-loop entry
# point), NOT a hand-built call to advance_finding -- the readiness
# review's explicit trap: without claim_tracing_findings widened to
# also claim STATUS:acquiring, this arm is unreachable in the real
# worker loop even if a hand-built FindingRow test would pass.


def _seed_memory(store, *, text: str = "a research note") -> int:
    ref = store.insert_ref(kind="memory", slug=None, title=text[:80], meta={})
    store.blocks.insert_blocks(ref.id, [BlockInsert(pos=0, text=text, meta={})])
    return ref.id


def _seed_acquiring_finding(
    store,
    *,
    wants: list[dict[str, Any]],
    body: str = "a claim awaiting corpus evidence",
) -> tuple[int, int]:
    """Mint an acquisition-mode finding; returns ``(finding_ref_id,
    stub_ref_id)`` — the single linked stub (every test here uses one
    ``wants=`` descriptor)."""
    mem_id = _seed_memory(store)
    h = _make_handler(store)
    resp = h.put(
        title="acquisition-mode claim",
        body=body,
        wants=wants,
        provenance=f"memory:{mem_id}",
    )
    id_m = re.search(r"id=(\d+)", resp.body)
    assert id_m is not None, f"create-ack missing id=; got {resp.body!r}"
    fid = int(id_m.group(1))
    awaits = store.links_for(fid, direction="out", relation="awaits-evidence")
    assert len(awaits) == 1
    return fid, awaits[0].dst_ref_id


def test_acquiring_finding_claimed_but_stays_acquiring_while_stub_bare(store) -> None:
    """AC #3 (part 1): an acquiring finding IS claimed by
    run_finding_chase_pass (the widened claim query), but with a
    chunk-less stub it stays STATUS:acquiring -- NOT dead_chain, unlike
    the tracing arm's own empty-chain check."""
    fid, _stub_id = _seed_acquiring_finding(store, wants=[{"doi": "10.1/acq-a"}])

    result = run_finding_chase_pass(store, limit=10)
    assert result == {"claimed": 1, "ok": 1, "failed": 0}
    assert _status_tag(store, fid) == "acquiring"
    assert _chain(store, fid) == []


def test_acquiring_finding_grounds_once_stub_gains_chunks(store) -> None:
    """AC #3 (part 2): once the linked stub is ingested with chunks, the
    NEXT pass grounds the finding -- chain populated, cited_in-equivalent
    derived-from link set, status flips to tracing -- and the pre-existing
    lifecycle proceeds unchanged from there (a further pass on the now-
    tracing, no-inline-cite frontier establishes it)."""
    claim_text = "a claim awaiting corpus evidence"
    fid, stub_id = _seed_acquiring_finding(
        store, wants=[{"doi": "10.1/acq-b"}], body=claim_text
    )

    # First pass: stub still bare -- stays acquiring (already covered
    # above; re-asserted here as the pre-condition for this scenario).
    run_finding_chase_pass(store, limit=10)
    assert _status_tag(store, fid) == "acquiring"

    # That first pass wrote a "waiting" chase event, which the claim
    # query's exponential backoff would otherwise suppress for up to an
    # hour (the same throttle a tracing finding's frontier-stub wait
    # gets -- see test_claim_skips_recently_waiting_finding). Age it out
    # so the next pass below can reclaim promptly, same as
    # test_claim_reclaims_after_backoff_window does for the tracing arm.
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE ref_events SET ts = ts - INTERVAL '2 hours' "
            "WHERE ref_id = %s AND source = 'chase'",
            (fid,),
        )
        conn.commit()

    # The stub "lands a PDF": give it a body chunk (what fetch_oa +
    # ingest would have done).
    store.blocks.insert_blocks(
        stub_id,
        [BlockInsert(pos=0, text=f"{claim_text}, stated directly.", meta={})],
    )

    result = run_finding_chase_pass(store, limit=10)
    assert result == {"claimed": 1, "ok": 1, "failed": 0}
    assert _status_tag(store, fid) == "tracing"
    chain = _chain(store, fid)
    assert len(chain) == 1
    assert int(chain[0]["ref_id"]) == stub_id

    links = store.links_for(fid, direction="out", relation="derived-from")
    assert any(link.dst_ref_id == stub_id for link in links)

    # The pre-existing lifecycle proceeds unchanged: no inline cites on
    # the grounded chunk -> the next pass establishes it, same as an
    # ordinary chase finding.
    result2 = run_finding_chase_pass(store, limit=10)
    assert result2 == {"claimed": 1, "ok": 1, "failed": 0}
    assert _status_tag(store, fid) == "established"


def test_acquiring_give_up_after_grace_window_when_stubs_exhausted(store) -> None:
    """AC #5: once every linked stub is fetch-exhausted (>=1 fetcher:%
    attempt, still no PDF) AND the finding has been waiting past the
    acquisition-mode grace window, the pass gives up exactly once --
    dead_chain(reason=unacquirable) -- and the stub still surfaces in
    the hand-download queue (stub_backlog), unaffected."""
    fid, stub_id = _seed_acquiring_finding(store, wants=[{"doi": "10.1/acq-c"}])

    # fetch_oa already tried this stub at least once and came up empty.
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO ref_events (ref_id, source, event, payload) "
            "VALUES (%s, 'fetcher:unpaywall', 'no_oa_version', '{}'::jsonb)",
            (stub_id,),
        )
        conn.commit()

    # The finding has been "waiting" (chase's own backoff bookkeeping)
    # for 10 days -- past the default 7-day acquisition grace window,
    # and well past the exponential backoff's 24h cap, so the claim
    # query still reclaims it (mirrors test_claim_reclaims_after_widened_window).
    _insert_chase_event(store, fid, "waiting", minutes_ago=14400)

    result = run_finding_chase_pass(store, limit=10)
    assert result == {"claimed": 1, "ok": 1, "failed": 0}
    assert _status_tag(store, fid) == "dead_chain"

    with store.pool.connection() as conn:
        meta_row = conn.execute(
            "SELECT meta FROM refs WHERE ref_id = %s", (fid,)
        ).fetchone()
    assert (meta_row[0] or {}).get("dead_reason") == "unacquirable"

    # A second pass doesn't re-claim a dead_chain finding at all (the
    # claim query only selects tracing/acquiring) -- "exactly once".
    with store.pool.connection() as conn:
        reclaimed = claim_tracing_findings(conn, limit=10)
        conn.commit()
    assert fid not in [f.ref_id for f in reclaimed]

    # The stub is untouched by the finding's give-up -- it still
    # surfaces in the hand-download queue.
    backlog_ids = {row["ref_id"] for row in store.stub_backlog(limit=50)}
    assert stub_id in backlog_ids


def test_acquiring_waits_within_grace_window_even_when_exhausted(store) -> None:
    """A stub that's already fetch-exhausted does NOT trigger give-up
    before the grace window has elapsed -- age is required, not just
    exhaustion (the "honest give-up" requires both)."""
    fid, stub_id = _seed_acquiring_finding(store, wants=[{"doi": "10.1/acq-d"}])
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO ref_events (ref_id, source, event, payload) "
            "VALUES (%s, 'fetcher:unpaywall', 'no_oa_version', '{}'::jsonb)",
            (stub_id,),
        )
        conn.commit()

    result = run_finding_chase_pass(store, limit=10)
    assert result == {"claimed": 1, "ok": 1, "failed": 0}
    assert _status_tag(store, fid) == "acquiring"


def test_acquiring_stays_acquiring_when_fetch_ok_pending_ingest_past_grace(
    store,
) -> None:
    """A ``fetch_ok`` success event means a PDF is inbound but not yet
    ingested -- NOT exhausted, regardless of grace-window age or an
    earlier failed leg on the same stub. Without excluding fetch_ok from
    ``_stub_exhausted``, give-up could fire the instant before evidence
    actually arrives (reviewer-flagged bug). Once chunks land on a later
    pass, the finding grounds normally."""
    claim_text = "a claim awaiting corpus evidence"
    fid, stub_id = _seed_acquiring_finding(
        store, wants=[{"doi": "10.1/acq-e"}], body=claim_text
    )
    with store.pool.connection() as conn:
        # An earlier leg failed, then a later one succeeded -- fetch_ok
        # must win regardless of event order.
        conn.execute(
            "INSERT INTO ref_events (ref_id, source, event, payload) "
            "VALUES (%s, 'fetcher:unpaywall', 'no_oa_version', '{}'::jsonb)",
            (stub_id,),
        )
        conn.execute(
            "INSERT INTO ref_events (ref_id, source, event, payload) "
            "VALUES (%s, 'fetcher:arxiv', 'fetch_ok', '{}'::jsonb)",
            (stub_id,),
        )
        conn.commit()

    # Age the finding's own "waiting" bookkeeping well past the grace
    # window -- if fetch_ok weren't excluded from exhaustion, this alone
    # (combined with the failed unpaywall leg above) would be enough to
    # trigger give-up.
    _insert_chase_event(store, fid, "waiting", minutes_ago=14400)

    result = run_finding_chase_pass(store, limit=10)
    assert result == {"claimed": 1, "ok": 1, "failed": 0}
    assert _status_tag(store, fid) == "acquiring"  # NOT dead_chain

    # Age out the "waiting" event this pass itself just wrote (else the
    # claim-query backoff suppresses the finding on the next pass below
    # -- same throttle test_acquiring_finding_grounds_once_stub_gains_chunks
    # works around).
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE ref_events SET ts = ts - INTERVAL '2 hours' "
            "WHERE ref_id = %s AND source = 'chase'",
            (fid,),
        )
        conn.commit()

    # Ingest lands the chunks on a later pass -- grounds normally.
    store.blocks.insert_blocks(
        stub_id,
        [BlockInsert(pos=0, text=f"{claim_text}, stated directly.", meta={})],
    )
    result2 = run_finding_chase_pass(store, limit=10)
    assert result2 == {"claimed": 1, "ok": 1, "failed": 0}
    assert _status_tag(store, fid) == "tracing"
    chain = _chain(store, fid)
    assert len(chain) == 1
    assert int(chain[0]["ref_id"]) == stub_id
