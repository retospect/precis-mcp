"""gr524022 follow-up: a merge records exactly which cite_key values it
re-homed on the loser (``meta.rehomed_cite_keys``, plus the adopt leg's
``adopted_cite_key``/``adopted_displaced_key``), and ``garden restore``
moves back exactly those — never a survivor-owned alias, never another
loser's key. Before the marker, restore chased ANY alias on the
survivor, could pin the wrong loser, and gave the loser a key that was
never its own.
"""

from __future__ import annotations

import pytest

from precis.cli import garden
from precis.cli.main import _build_parser
from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.paper import PaperHandler
from precis.ingest.add import _reconcile_orphan_stub
from precis.ingest.dedup import merge_duplicate
from precis.store import Ref, Store


def _paper(store: Store, slug: str, title: str | None = None) -> int:
    return store.insert_ref(
        kind="paper",
        slug=slug,
        title=title or f"Rehome {slug}",
        provider="manual",
    ).id


def _merge(store: Store, loser: int, winner: int) -> None:
    with store.tx() as conn:
        merge_duplicate(
            store,
            survivor_ref_id=winner,
            duplicate_ref_id=loser,
            source="test-rehome",
            reason="duplicate",
            conn=conn,
        )


def _run(store: Store, monkeypatch: pytest.MonkeyPatch, *args: str) -> None:
    parser = _build_parser(("garden",))
    monkeypatch.setattr(Store, "connect", lambda dsn: store)
    garden.run(parser.parse_args(["garden", "restore", *args, "--dsn", "test://"]))


def _ref(store: Store, ref_id: int) -> Ref:
    return store.fetch_refs_by_ids([ref_id], include_deleted=True)[ref_id]


def _meta(store: Store, ref_id: int) -> dict:
    return dict(_ref(store, ref_id).meta or {})


def _sql(store: Store, q: str, p: tuple) -> None:
    with store.pool.connection() as conn:
        conn.execute(q, p)
        conn.commit()


def _collapse_l1_to_survivor(store: Store, loser1: int, surv: int) -> None:
    """What paper_reconcile's collapse_superseded_chains does to a chain
    tombstone: repoint the upstream loser's superseded_by at the live
    terminal."""
    _sql(
        store,
        "UPDATE refs SET meta = jsonb_set(meta, %s, to_jsonb(%s::text)) "
        "WHERE ref_id = %s",
        (["superseded_by"], str(surv), loser1),
    )


def _strip_markers(store: Store, ref_id: int) -> None:
    """Make a marker-era merge look like a pre-marker one: the loser's
    key sits as an unrecorded cite_key_alias on the survivor."""
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET meta = meta - 'rehomed_cite_keys' "
            "- 'rehomed_cite_key_aliases' - 'adopted_cite_key' "
            "- 'adopted_displaced_key' WHERE ref_id = %s",
            (ref_id,),
        )
        conn.commit()


def test_merge_records_rehomed_keys_and_restore_by_id_moves_them_back(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    winner = _paper(store, "survrealkey31")  # non-junk → no adopt leg
    loser = _paper(store, "loserkey31")
    _merge(store, loser, winner)

    meta = _meta(store, loser)
    assert meta["rehomed_cite_keys"] == ["loserkey31"]
    assert store.find_ref_by_identifier("cite_key_alias", "loserkey31") == winner

    _run(store, monkeypatch, f"pa{loser}", "--reason", "not a duplicate")
    assert _ref(store, loser).retired_at is None
    assert store.find_ref_by_identifier("cite_key", "loserkey31") == loser
    assert store.find_ref_by_identifier("cite_key_alias", "loserkey31") is None
    assert store.find_ref_by_identifier("cite_key", "survrealkey31") == winner
    after = _meta(store, loser)
    assert "rehomed_cite_keys" not in after and "superseded_by" not in after


def test_adopt_case_restore_puts_each_side_back(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The exact reviewed bug: survivor ``anon88`` adopts the loser's key;
    ``restore anon88`` must not hand the survivor's own old key to the
    loser, and restoring the loser puts both sides back."""
    winner = _paper(store, "anon88")  # junk-shaped → adopt leg fires
    loser = _paper(store, "victimkey2034")
    _merge(store, loser, winner)

    meta = _meta(store, loser)
    assert meta["rehomed_cite_keys"] == ["victimkey2034"]
    assert meta["adopted_cite_key"] == "victimkey2034"
    assert meta["adopted_displaced_key"] == "anon88"
    assert store.find_ref_by_identifier("cite_key", "victimkey2034") == winner
    assert store.find_ref_by_identifier("cite_key_alias", "anon88") == winner

    # anon88 is the survivor's OWN displaced alias — the loser never
    # recorded it. Restore by that key must not touch the loser.
    _run(store, monkeypatch, "anon88", "--reason", "checking")
    assert f"no change: pa{winner} is already live" in capsys.readouterr().out
    assert _ref(store, loser).retired_at is not None

    _run(store, monkeypatch, f"pa{loser}", "--reason", "not a duplicate")
    assert store.find_ref_by_identifier("cite_key", "victimkey2034") == loser
    assert store.find_ref_by_identifier("cite_key", "anon88") == winner
    assert store.find_ref_by_identifier("cite_key_alias", "victimkey2034") is None
    assert store.find_ref_by_identifier("cite_key_alias", "anon88") is None


def test_two_losers_each_gets_own_key_back(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    winner = _paper(store, "survkey77")
    loser_a = _paper(store, "loserkey77a")
    loser_b = _paper(store, "loserkey77b")
    _merge(store, loser_a, winner)
    _merge(store, loser_b, winner)

    _run(store, monkeypatch, f"pa{loser_a}", "--reason", "first")
    assert store.find_ref_by_identifier("cite_key", "loserkey77a") == loser_a
    # The other loser's key is untouched — it stays an alias on the
    # survivor until ITS restore.
    assert store.find_ref_by_identifier("cite_key_alias", "loserkey77b") == winner

    _run(store, monkeypatch, f"pa{loser_b}", "--reason", "second")
    assert store.find_ref_by_identifier("cite_key", "loserkey77b") == loser_b
    assert store.find_ref_by_identifier("cite_key_alias", "loserkey77b") is None


def test_restore_by_rehomed_key_resolves_the_marked_loser(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """By-key restore follows the loser's own rehomed marker — not 'any
    alias on the survivor' — so a survivor with two losers restores the
    right one."""
    winner = _paper(store, "survkey88")
    loser_a = _paper(store, "loserkey88a")
    loser_b = _paper(store, "loserkey88b")
    _merge(store, loser_a, winner)
    _merge(store, loser_b, winner)

    _run(store, monkeypatch, "loserkey88b", "--reason", "checking")
    assert _ref(store, loser_b).retired_at is None
    assert _ref(store, loser_a).retired_at is not None
    assert store.find_ref_by_identifier("cite_key", "loserkey88b") == loser_b
    assert store.find_ref_by_identifier("cite_key_alias", "loserkey88a") == winner


def test_premarker_merge_restore_by_key_falls_back_to_alias_chase(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No marker matches (a pre-marker merge): the old behaviour —
    chase the alias to its holder, then the supersedes edge back to
    the loser — restores it instead of printing "already live"."""
    winner = _paper(store, "survkey66")
    loser = _paper(store, "loserkey66")
    _merge(store, loser, winner)
    _strip_markers(store, loser)

    _run(store, monkeypatch, "loserkey66", "--reason", "not a duplicate")
    assert f"restored pa{loser}" in capsys.readouterr().out
    assert _ref(store, loser).retired_at is None
    # Pre-marker scope: with nothing recorded, no keys move back.
    assert store.find_ref_by_identifier("cite_key_alias", "loserkey66") == winner


def test_premarker_merge_restore_by_key_ambiguous_refuses(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The alias's holder superseded two unmarked losers — the key
    alone can't pin which one. Refuse, naming each candidate's
    garden-restore-by-id line."""
    winner = _paper(store, "survkey77x")
    loser_a = _paper(store, "loserkey77a")
    loser_b = _paper(store, "loserkey77b")
    _merge(store, loser_a, winner)
    _merge(store, loser_b, winner)
    _strip_markers(store, loser_a)
    _strip_markers(store, loser_b)

    with pytest.raises(SystemExit) as exc:
        _run(store, monkeypatch, "loserkey77a", "--reason", "checking")
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "2 merged papers" in err
    assert f"precis garden restore pa{loser_a}" in err
    assert f"precis garden restore pa{loser_b}" in err
    assert _ref(store, loser_a).retired_at is not None
    assert _ref(store, loser_b).retired_at is not None


def test_losers_own_aliases_recorded_and_restored(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The loser's own cite_key_alias rows (an earlier rekey's demoted
    key) re-home to the survivor too — recorded under
    meta.rehomed_cite_key_aliases and put back on the loser AS aliases
    on restore."""
    winner = _paper(store, "survkey55")
    loser = _paper(store, "loserkey55")
    # The loser was rekeyed once before: its old key sits as an alias.
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO ref_identifiers (id_kind, id_value, ref_id, "
            "source) VALUES ('cite_key_alias', 'oldloserkey55', %s, 'test')",
            (loser,),
        )
        conn.commit()
    _merge(store, loser, winner)

    meta = _meta(store, loser)
    assert meta["rehomed_cite_keys"] == ["loserkey55"]
    assert meta["rehomed_cite_key_aliases"] == ["oldloserkey55"]
    assert store.find_ref_by_identifier("cite_key_alias", "oldloserkey55") == winner

    _run(store, monkeypatch, f"pa{loser}", "--reason", "not a duplicate")
    assert store.find_ref_by_identifier("cite_key", "loserkey55") == loser
    # The old redirect points at the loser again — as an alias, not a
    # live key.
    assert store.find_ref_by_identifier("cite_key_alias", "oldloserkey55") == loser
    assert store.find_ref_by_identifier("cite_key", "oldloserkey55") is None
    assert "rehomed_cite_key_aliases" not in _meta(store, loser)


def test_chained_remerge_deletes_the_alias_wherever_it_lives(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L1 merges into L2, then L2 into S: the second fold re-homes L2's
    cite_key_alias rows — L1's recorded key included — onto S. A
    survivor-scoped delete (``ref_id = L1's direct superseded_by``)
    misses it, leaving S and the restored L1 holding the same value.
    The recorded alias drops by (id_kind, id_value) wherever it lives."""
    surv = _paper(store, "survchain9")
    loser2 = _paper(store, "loser2chain9")
    loser1 = _paper(store, "loser1chain9")
    _merge(store, loser1, loser2)
    _merge(store, loser2, surv)
    assert store.find_ref_by_identifier("cite_key_alias", "loser1chain9") == surv

    _run(store, monkeypatch, f"pa{loser1}", "--reason", "first merge wrong")
    assert _ref(store, loser1).retired_at is None
    assert store.find_ref_by_identifier("cite_key", "loser1chain9") == loser1
    assert store.find_ref_by_identifier("cite_key_alias", "loser1chain9") is None
    # L2 stays a tombstone of S; only L1's own key came back.
    assert _ref(store, loser2).retired_at is not None


def test_restore_refuses_when_a_third_ref_holds_the_rehomed_key(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A post-merge re-mint claimed the loser's old key as a live
    cite_key on a third ref: the alias delete still fires but the
    re-INSERT hits ON CONFLICT DO NOTHING, leaving the loser with no
    identifiers. The restore must refuse up front, naming the holder,
    and roll back with nothing changed."""
    winner = _paper(store, "survkey31x")
    loser = _paper(store, "loserkey31x")
    _merge(store, loser, winner)
    squatter = _paper(store, "loserkey31x-dup")
    # A third ref re-claims the value as its live cite_key — the
    # (id_kind, id_value) PK allows it beside the winner's alias.
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE ref_identifiers SET id_value = 'loserkey31x' "
            "WHERE ref_id = %s AND id_kind = 'cite_key'",
            (squatter,),
        )
        conn.commit()

    with pytest.raises(BadInput, match=f"pa{squatter}.*live cite_key"):
        store.restore_superseded_ref(loser)
    # Rolled back: nothing moved, loser still retired and marked.
    assert _ref(store, loser).retired_at is not None
    assert _meta(store, loser)["rehomed_cite_keys"] == ["loserkey31x"]
    assert store.find_ref_by_identifier("cite_key_alias", "loserkey31x") == winner
    assert store.find_ref_by_identifier("cite_key", "loserkey31x") == squatter


def test_restore_refuses_when_an_outsider_holds_the_own_key_alias(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(review should-fix 4) The loser's OWN key recorded under
    ``rehomed_cite_keys`` is held as a cite_key_alias by a live ref
    outside the survivor chain — the same conflict as a live cite_key
    holder, so the restore refuses before any write instead of
    completing with the loser keyless. The ``next=`` names the holder
    and the detach door."""
    surv = _paper(store, "survoa")
    loser = _paper(store, "loseroa")
    _merge(store, loser, surv)
    assert store.find_ref_by_identifier("cite_key_alias", "loseroa") == surv
    outsider = _paper(store, "outsideroa")
    _sql(
        store,
        "UPDATE ref_identifiers SET ref_id = %s "
        "WHERE id_kind = 'cite_key_alias' AND id_value = 'loseroa'",
        (outsider,),
    )

    with pytest.raises(BadInput) as exc:
        store.restore_superseded_ref(loser)
    err = exc.value
    assert "loseroa" in str(err) and f"pa{outsider}" in str(err)
    nxt = str(getattr(err, "next", ""))
    assert "drop_identifier" in nxt and f"pa{outsider}" in nxt
    # Rolled back: nothing moved, loser still retired and marked.
    assert _ref(store, loser).retired_at is not None
    assert _meta(store, loser)["rehomed_cite_keys"] == ["loseroa"]
    assert store.find_ref_by_identifier("cite_key_alias", "loseroa") == outsider
    assert store.find_ref_by_identifier("cite_key", "loseroa") is None


def test_restore_refuses_when_an_outsider_holds_the_displaced_alias(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(review should-fix 5) Adopt leg: the survivor's own displaced
    key must go back on the survivor before the adopted value frees.
    A live ref outside the chain holding it as a cite_key_alias would
    leave a shadowing alias behind — refuse before any write, naming
    the holder and the detach door."""
    winner = _paper(store, "anon55x")  # junk-shaped → adopt leg fires
    loser = _paper(store, "victimkey2055x")
    _merge(store, loser, winner)
    assert _meta(store, loser)["adopted_displaced_key"] == "anon55x"
    assert store.find_ref_by_identifier("cite_key_alias", "anon55x") == winner
    outsider = _paper(store, "outsiderdx")
    _sql(
        store,
        "UPDATE ref_identifiers SET ref_id = %s "
        "WHERE id_kind = 'cite_key_alias' AND id_value = 'anon55x'",
        (outsider,),
    )

    with pytest.raises(BadInput) as exc:
        store.restore_superseded_ref(loser)
    err = exc.value
    assert "anon55x" in str(err) and f"pa{outsider}" in str(err)
    nxt = str(getattr(err, "next", ""))
    assert "drop_identifier" in nxt and f"pa{outsider}" in nxt
    # Rolled back: survivor still holds the adopted key, its displaced
    # alias still sits on the outsider, the loser is still retired.
    assert _ref(store, loser).retired_at is not None
    assert store.find_ref_by_identifier("cite_key", "victimkey2055x") == winner
    assert store.find_ref_by_identifier("cite_key_alias", "anon55x") == outsider


def test_restore_refuses_when_a_third_ref_holds_the_displaced_key(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Adopt leg: the survivor's own displaced key must go back on the
    survivor before the adopted value frees. A third ref holding that
    displaced key as cite_key would leave the survivor keyless — refuse
    before any DELETE and roll the whole restore back."""
    winner = _paper(store, "anon77")  # junk-shaped → adopt leg fires
    loser = _paper(store, "victimkey2077")
    _merge(store, loser, winner)
    assert _meta(store, loser)["adopted_displaced_key"] == "anon77"
    squatter = _paper(store, "squatter77")
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE ref_identifiers SET id_value = 'anon77' "
            "WHERE ref_id = %s AND id_kind = 'cite_key'",
            (squatter,),
        )
        conn.commit()

    with pytest.raises(BadInput, match=f"pa{squatter}.*live cite_key"):
        store.restore_superseded_ref(loser)
    # Rolled back: the survivor still holds the adopted key, its
    # displaced key is still its alias, the loser is still retired.
    assert _ref(store, loser).retired_at is not None
    assert store.find_ref_by_identifier("cite_key", "victimkey2077") == winner
    assert store.find_ref_by_identifier("cite_key_alias", "anon77") == winner
    assert store.find_ref_by_identifier("cite_key", "anon77") == squatter


def test_chain_restore_l1_then_l2_no_dual_claim(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(p1) Chain L1→L2→S: the second fold also records L1's key on
    L2's ``rehomed_cite_key_aliases``. Restoring L1 strips that stale
    claim, so L2's own restore moves only L2's key — no dual claim on
    L1's key survives."""
    surv = _paper(store, "survp1")
    loser2 = _paper(store, "loser2p1")
    loser1 = _paper(store, "loser1p1")
    _merge(store, loser1, loser2)
    _merge(store, loser2, surv)
    assert "loser1p1" in _meta(store, loser2)["rehomed_cite_key_aliases"]

    _run(store, monkeypatch, f"pa{loser1}", "--reason", "first")
    assert _ref(store, loser1).retired_at is None
    assert store.find_ref_by_identifier("cite_key", "loser1p1") == loser1
    # L2's stale claim on L1's key went with the same restore.
    assert "loser1p1" not in _meta(store, loser2).get("rehomed_cite_key_aliases", [])

    _run(store, monkeypatch, f"pa{loser2}", "--reason", "second")
    assert _ref(store, loser2).retired_at is None
    assert store.find_ref_by_identifier("cite_key", "loser2p1") == loser2
    # No dual claim: L1's key resolves only to L1, nothing aliases it.
    assert store.find_ref_by_identifier("cite_key", "loser1p1") == loser1
    assert store.find_ref_by_identifier("cite_key_alias", "loser1p1") is None
    assert store.find_ref_by_identifier("cite_key_alias", "loser2p1") is None


def test_chain_restore_rekey_l1_restore_l2_keeps_l1_alias(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(p3) Same chain, then a rekey on the restored L1 demotes its old
    key to L1's own ``cite_key_alias``. L2's later restore must not take
    the row — a live ref outside L2's survivor chain holds it."""
    surv = _paper(store, "survp3")
    loser2 = _paper(store, "loser2p3")
    loser1 = _paper(store, "loser1p3")
    _merge(store, loser1, loser2)
    _merge(store, loser2, surv)

    _run(store, monkeypatch, f"pa{loser1}", "--reason", "first")
    store.set_ref_identifier(loser1, "cite_key", "loser1newp3", source="test")
    assert store.find_ref_by_identifier("cite_key_alias", "loser1p3") == loser1

    _run(store, monkeypatch, f"pa{loser2}", "--reason", "second")
    assert _ref(store, loser2).retired_at is None
    assert store.find_ref_by_identifier("cite_key", "loser2p3") == loser2
    # L1 keeps its new key AND the demoted redirect for the old one.
    assert store.find_ref_by_identifier("cite_key", "loser1newp3") == loser1
    assert store.find_ref_by_identifier("cite_key_alias", "loser1p3") == loser1


def test_reverse_order_l2_then_l1(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(review must-fix 1) Chain L1→L2→S, restore the DOWNSTREAM loser
    first. L2's alias list records L1's key and L1 still claims it under
    ``rehomed_cite_keys`` — but L1 folded THROUGH L2, so the owner
    skip does not apply: L2's restore moves the row onto L2 as an
    alias instead of leaving it on S where L1's later chain cannot
    reach it. L1's own restore then completes with no refusal and no
    manual step, and no ref is left double-claiming the value."""
    surv = _paper(store, "survrv")
    loser2 = _paper(store, "loser2rv")
    loser1 = _paper(store, "loser1rv")
    _merge(store, loser1, loser2)
    _merge(store, loser2, surv)
    assert "loser1rv" in _meta(store, loser2)["rehomed_cite_key_aliases"]

    _run(store, monkeypatch, f"pa{loser2}", "--reason", "x")
    # The row moved onto L2 as an alias; L1's own-key claim survived —
    # an alias-list move never strips rehomed_cite_keys.
    assert "loser1rv" in _meta(store, loser1).get("rehomed_cite_keys", [])
    assert store.find_ref_by_identifier("cite_key_alias", "loser1rv") == loser2

    _run(store, monkeypatch, f"pa{loser1}", "--reason", "y")
    assert _ref(store, loser1).retired_at is None
    assert store.find_ref_by_identifier("cite_key", "loser1rv") == loser1
    # No double claim: the value lives only as L1's cite_key.
    assert store.find_ref_by_identifier("cite_key_alias", "loser1rv") is None
    assert store.find_ref_by_identifier("cite_key", "loser2rv") == loser2


def test_reverse_order_stub_chain_stub_then_l1(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(review must-fix 1) The same shape through a stub fold: L1
    merges into stub T, T folds into S via ``_reconcile_orphan_stub``
    — which stamps only ``rehomed_cite_key_aliases`` on T. Restoring T
    first moves L1's key onto T (L1 folded through T); restoring L1
    then completes with no refusal and no manual step."""
    surv = _paper(store, "stubsv", "Atomic evolution of hydrogen")
    stub = _paper(store, "stubtc24", "Atomic evolution (stub)")
    loser1 = _paper(store, "loser1stc")
    _merge(store, loser1, stub)
    with store.pool.connection() as conn:
        merged = _reconcile_orphan_stub(
            store, survivor_ref_id=surv, file_stem="stubtc24", conn=conn
        )
        conn.commit()
    assert merged == stub
    assert "loser1stc" in _meta(store, stub)["rehomed_cite_key_aliases"]
    assert "rehomed_cite_keys" not in _meta(store, stub)

    _run(store, monkeypatch, f"pa{stub}", "--reason", "x")
    assert _ref(store, stub).retired_at is None
    assert store.find_ref_by_identifier("cite_key_alias", "loser1stc") == stub
    assert "loser1stc" in _meta(store, loser1).get("rehomed_cite_keys", [])

    _run(store, monkeypatch, f"pa{loser1}", "--reason", "y")
    assert _ref(store, loser1).retired_at is None
    assert store.find_ref_by_identifier("cite_key", "loser1stc") == loser1
    assert store.find_ref_by_identifier("cite_key_alias", "loser1stc") is None


def test_reverse_order_after_collapse(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(review must-fix 1) Same chain, but collapse_superseded_chains
    repointed L1.superseded_by at S before the restores: L2 is then a
    live ref OUTSIDE L1's chain, so L2's alias claim on L1's key must
    still lose to L1's own-key claim."""
    surv = _paper(store, "survrc")
    loser2 = _paper(store, "loser2rc")
    loser1 = _paper(store, "loser1rc")
    _merge(store, loser1, loser2)
    _merge(store, loser2, surv)
    _collapse_l1_to_survivor(store, loser1, surv)

    _run(store, monkeypatch, f"pa{loser2}", "--reason", "x")
    assert "loser1rc" in _meta(store, loser1).get("rehomed_cite_keys", [])
    assert store.find_ref_by_identifier("cite_key_alias", "loser1rc") == surv

    _run(store, monkeypatch, f"pa{loser1}", "--reason", "y")
    assert _ref(store, loser1).retired_at is None
    assert store.find_ref_by_identifier("cite_key", "loser1rc") == loser1
    assert store.find_ref_by_identifier("cite_key_alias", "loser1rc") is None
    assert store.find_ref_by_identifier("cite_key", "loser2rc") == loser2


def test_collapse_then_l1_first(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    """The other order still works after a collapse: L1 restores first
    (taking its key off S and stripping L2's stale alias claim), then
    L2 restores its own key without touching L1's."""
    surv = _paper(store, "survcf")
    loser2 = _paper(store, "loser2cf")
    loser1 = _paper(store, "loser1cf")
    _merge(store, loser1, loser2)
    _merge(store, loser2, surv)
    _collapse_l1_to_survivor(store, loser1, surv)

    _run(store, monkeypatch, f"pa{loser1}", "--reason", "y")
    assert store.find_ref_by_identifier("cite_key", "loser1cf") == loser1
    _run(store, monkeypatch, f"pa{loser2}", "--reason", "x")
    assert store.find_ref_by_identifier("cite_key", "loser2cf") == loser2
    assert store.find_ref_by_identifier("cite_key", "loser1cf") == loser1
    assert store.find_ref_by_identifier("cite_key_alias", "loser1cf") is None


def test_null_keyed_rank_by_key(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    """(review must-fix 2) A retired claimant with NO
    ``rehomed_cite_keys`` key at all produces keyed=NULL, and plain
    ``ORDER BY keyed DESC`` puts NULLS FIRST — the alias-only claimant
    would outrank the true owner. L2's marker rows here look exactly
    like a stub fold's (only the alias list is stamped)."""
    surv = _paper(store, "survnk")
    loser2 = _paper(store, "loser2nk")
    loser1 = _paper(store, "loser1nk")
    _merge(store, loser1, loser2)
    _merge(store, loser2, surv)
    _sql(
        store,
        "UPDATE refs SET meta = meta - %s WHERE ref_id = %s",
        ("rehomed_cite_keys", loser2),
    )

    _run(store, monkeypatch, "loser1nk", "--reason", "k")
    assert _ref(store, loser1).retired_at is None
    assert _ref(store, loser2).retired_at is not None


def test_realistic_stub_chain_by_key(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(review must-fix 2) The realistic shape of a NULL-keyed
    claimant: L1 merges into stub T, then T folds into S via
    ``_reconcile_orphan_stub`` — which stamps only
    ``rehomed_cite_key_aliases`` on T. Restore by L1's key must pick
    L1 (keyed=TRUE), not T (keyed=NULL)."""
    surv = _paper(store, "lee24d", "Atomic evolution of hydrogen")
    stub = _paper(store, "atomic24c", "Atomic evolution (stub)")
    loser1 = _paper(store, "loser1stub")
    _merge(store, loser1, stub)
    with store.pool.connection() as conn:
        merged = _reconcile_orphan_stub(
            store, survivor_ref_id=surv, file_stem="atomic24c", conn=conn
        )
        conn.commit()
    assert merged == stub
    assert "loser1stub" in _meta(store, stub)["rehomed_cite_key_aliases"]
    assert "rehomed_cite_keys" not in _meta(store, stub)

    _run(store, monkeypatch, "loser1stub", "--reason", "k")
    assert _ref(store, loser1).retired_at is None
    assert _ref(store, stub).retired_at is not None


def test_null_keyed_door_next(
    store: Store, hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(review must-fix 2) The destructive-door guard's ``next=`` must
    name the true owner of the key: with a NULL-keyed claimant ranking
    first, the refusal pointed the operator at the wrong loser."""
    surv = _paper(store, "survnd")
    loser2 = _paper(store, "loser2nd")
    loser1 = _paper(store, "loser1nd")
    _merge(store, loser1, loser2)
    _merge(store, loser2, surv)
    _sql(
        store,
        "UPDATE refs SET meta = meta - %s WHERE ref_id = %s",
        ("rehomed_cite_keys", loser2),
    )

    handler = PaperHandler(hub=hub)
    with pytest.raises(BadInput) as exc:
        handler.edit(id="loser1nd", args={"retire": True, "reason": "r"})
    err = exc.value
    assert f"pa{loser1}" in str(getattr(err, "next", "")) + str(err)


def test_restore_leaves_alias_claim_a_live_outsider_holds(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """(review should-fix 3) A value recorded under the loser's own
    ``rehomed_cite_key_aliases`` whose row a LIVE ref outside the
    superseded_by chain holds is someone else's claim: the restore
    leaves the row and says so in a ``note:`` line — no steal."""
    surv = _paper(store, "survog")
    loser = _paper(store, "loserog")
    outsider = _paper(store, "outsiderog")
    # The loser's own pre-merge redirect — folded to S, recorded under
    # rehomed_cite_key_aliases.
    _sql(
        store,
        "INSERT INTO ref_identifiers (id_kind, id_value, ref_id, "
        "source) VALUES ('cite_key_alias', 'oldkeyog', %s, 'test')",
        (loser,),
    )
    _merge(store, loser, surv)
    assert "oldkeyog" in _meta(store, loser)["rehomed_cite_key_aliases"]
    # The row ends up held by a live ref outside the chain.
    _sql(
        store,
        "DELETE FROM ref_identifiers WHERE id_kind = 'cite_key_alias' "
        "AND id_value = 'oldkeyog'",
        (),
    )
    _sql(
        store,
        "INSERT INTO ref_identifiers (id_kind, id_value, ref_id, "
        "source) VALUES ('cite_key_alias', 'oldkeyog', %s, 'test')",
        (outsider,),
    )

    _run(store, monkeypatch, f"pa{loser}", "--reason", "x")
    out = capsys.readouterr().out
    assert "  note:" in out and "oldkeyog" in out and f"pa{outsider}" in out
    # The row stays on the outsider; the loser still restored its key.
    assert store.find_ref_by_identifier("cite_key_alias", "oldkeyog") == outsider
    assert _ref(store, loser).retired_at is None
    assert store.find_ref_by_identifier("cite_key", "loserog") == loser


def test_restore_skips_alias_shadowed_by_a_live_cite_key(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """(review should-fix 3) A recorded alias whose value some ref
    already holds as a live ``cite_key`` is skipped — a restored alias
    would only shadow the live key. The row itself is left alone and a
    ``note:`` line says why."""
    surv = _paper(store, "survsh")
    loser = _paper(store, "losersh")
    _sql(
        store,
        "INSERT INTO ref_identifiers (id_kind, id_value, ref_id, "
        "source) VALUES ('cite_key_alias', 'shadowkeysh', %s, 'test')",
        (loser,),
    )
    _merge(store, loser, surv)
    # A post-merge re-mint claimed the value as a live cite_key — the
    # (id_kind, id_value) PK allows it beside the survivor's alias row.
    squatter = _paper(store, "shadowkeysh")

    _run(store, monkeypatch, f"pa{loser}", "--reason", "x")
    out = capsys.readouterr().out
    assert "  note:" in out and "shadowkeysh" in out and f"pa{squatter}" in out
    assert store.find_ref_by_identifier("cite_key", "shadowkeysh") == squatter
    assert store.find_ref_by_identifier("cite_key_alias", "shadowkeysh") == surv
    assert _ref(store, loser).retired_at is None


def test_door_on_restored_losers_own_alias_not_refused(
    store: Store, hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(p2) A destructive door addressed by L1's own restored alias
    must not refuse on a stale chain claim — the second fold recorded
    the alias on L2's markers too, and L1's restore strips it, so the
    door resolves to live L1 instead of a "merged alias" refusal."""
    surv = _paper(store, "survp2")
    loser2 = _paper(store, "loser2p2")
    loser1 = _paper(store, "loser1p2")
    # L1's own pre-merge redirect — folded to L2, then to S.
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO ref_identifiers (id_kind, id_value, ref_id, "
            "source) VALUES ('cite_key_alias', 'l1ownp2', %s, 'test')",
            (loser1,),
        )
        conn.commit()
    _merge(store, loser1, loser2)
    _merge(store, loser2, surv)

    _run(store, monkeypatch, f"pa{loser1}", "--reason", "first")
    assert store.find_ref_by_identifier("cite_key_alias", "l1ownp2") == loser1

    handler = PaperHandler(hub=hub)
    handler.edit(
        id="l1ownp2",
        args={
            "drop_identifier": {"scheme": "cite_key_alias", "value": "l1ownp2"},
            "reason": "stale redirect",
        },
    )
    assert store.find_ref_by_identifier("cite_key_alias", "l1ownp2") is None


def test_restore_by_key_in_a_chain_restores_the_key_owner(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(p5) Restore by L1's key in a chain: L1 claims it under
    ``rehomed_cite_keys``, L2 under ``rehomed_cite_key_aliases`` (the
    second fold's record). The keys claim outranks the alias claim, so
    L1 — the true owner — is restored instead of an ambiguous refusal."""
    surv = _paper(store, "survp5")
    loser2 = _paper(store, "loser2p5")
    loser1 = _paper(store, "loser1p5")
    _merge(store, loser1, loser2)
    _merge(store, loser2, surv)

    _run(store, monkeypatch, "loser1p5", "--reason", "checking")
    assert _ref(store, loser1).retired_at is None
    assert _ref(store, loser2).retired_at is not None
    assert store.find_ref_by_identifier("cite_key", "loser1p5") == loser1
    assert store.find_ref_by_identifier("cite_key_alias", "loser1p5") is None


def test_rerun_restore_by_key_when_live_stays_put(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """(p7) Once L1 is live holding its key, re-running the same
    by-key restore reports "already live" — L2's (stripped) stale
    marker must not resurrect L2 in its place."""
    surv = _paper(store, "survp7")
    loser2 = _paper(store, "loser2p7")
    loser1 = _paper(store, "loser1p7")
    _merge(store, loser1, loser2)
    _merge(store, loser2, surv)

    _run(store, monkeypatch, "loser1p7", "--reason", "first")
    capsys.readouterr()
    _run(store, monkeypatch, "loser1p7", "--reason", "again")
    out = capsys.readouterr().out
    assert f"no change: pa{loser1} is already live" in out
    assert _ref(store, loser2).retired_at is not None
    assert store.find_ref_by_identifier("cite_key", "loser1p7") == loser1


def test_no_left_in_place_note_for_a_value_moved_back_as_key(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """(review should-fix) A value recorded in BOTH of the restored
    ref's marker lists is moved back as its live cite_key by the keys
    loop — the alias loop must not then emit a "left in place" note
    for the same value (here via a still-retired ref that also claims
    it as its own rehomed key)."""
    surv = _paper(store, "survml")
    loser = _paper(store, "loserml")
    _merge(store, loser, surv)
    owner = _paper(store, "ownerml")
    other_surv = _paper(store, "otherml")
    _merge(store, owner, other_surv)
    # The retired owner also claims the loser's key under
    # rehomed_cite_keys, and the loser's own alias list records it too.
    _sql(
        store,
        "UPDATE refs SET meta = jsonb_set(meta, '{rehomed_cite_keys}', "
        "(meta->'rehomed_cite_keys') || %s::jsonb) WHERE ref_id = %s",
        ('["loserml"]', owner),
    )
    _sql(
        store,
        "UPDATE refs SET meta = jsonb_set(meta, "
        "'{rehomed_cite_key_aliases}', %s::jsonb) WHERE ref_id = %s",
        ('["loserml"]', loser),
    )

    _run(store, monkeypatch, f"pa{loser}", "--reason", "x")
    out = capsys.readouterr().out
    assert "note:" not in out
    assert _ref(store, loser).retired_at is None
    assert store.find_ref_by_identifier("cite_key", "loserml") == loser
    assert store.find_ref_by_identifier("cite_key_alias", "loserml") is None
