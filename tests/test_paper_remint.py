"""gr521515: junk-mint cite_keys re-mint on metadata repair; old keys redirect.

A rekeyed paper's displaced key stays resolvable through the
``cite_key_alias`` row :meth:`Store.set_ref_identifier` writes — every
slug reader (``get_ref``, handle mentions, the drive identifier probe,
the draft bib resolver) falls back to it only when no live ``cite_key``
claims the value. Each repair path that makes a junk-keyed paper's
byline real re-mints through the same door.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import precis.cli.paper_remint_junk as remint_cli
import precis.config
import precis.runtime
from precis.corpus_layout import corpus_pdf_dest
from precis.dispatch import Hub
from precis.draftimport.resolve import _try_cite_key
from precis.errors import BadInput
from precis.handlers._query_identifier import resolve_query_identifier
from precis.handlers.paper import PaperHandler
from precis.identity import is_junk_cite_key
from precis.ingest.dedup import inherit_missing_identity
from precis.ingest.metadata_resolve import Resolution, apply_resolution
from precis.ingest.remediate import _apply_fix, _Suspect
from precis.store import Store
from precis.store.paper_rekey import AUTO_REMINT_REASON, rekey_paper
from precis.utils.mentions import resolve_handle_ref

_MURTY = [{"family": "Murty", "given": "Katta G."}]


@pytest.fixture
def handler(hub: Hub) -> PaperHandler:
    return PaperHandler(hub=hub)


def _seed(
    store: Store,
    key: str,
    *,
    authors: list[dict[str, Any]] | None = None,
    year: int | None = None,
) -> int:
    return store.insert_ref(
        kind="paper",
        slug=key,
        title=f"Title of {key}",
        provider="manual",
        meta={},
        authors=authors,
        year=year,
    ).id


def _ident_rows(store: Store, ref_id: int) -> list[tuple[str, str]]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT id_kind, id_value FROM ref_identifiers "
            "WHERE ref_id = %s ORDER BY id_kind, id_value",
            (ref_id,),
        ).fetchall()
    return [(str(r[0]), str(r[1])) for r in rows]


def _write_snapshot(store: Store) -> dict[str, list[tuple[Any, ...]]]:
    """Every table the remint machinery may write, for dry-run checks."""
    with store.pool.connection() as conn:
        return {
            t: conn.execute(f"SELECT * FROM {t} ORDER BY 1, 2").fetchall()
            for t in ("refs", "ref_identifiers", "ref_events")
        }


@pytest.mark.parametrize(
    ("key", "junk"),
    [
        ("anon13", True),
        ("anon13a", True),
        ("anon13aa", True),
        ("anon00", True),
        ("user88", True),
        ("user88z", True),
        ("the24", True),
        ("the24z", True),
        ("murty88", False),
        ("theo88", False),
        ("usera13", False),
        ("anonymous13", False),
        ("smith24a", False),
        ("", False),
        (None, False),
        ("ANON13A", True),
    ],
)
def test_is_junk_cite_key(key: Any, junk: bool) -> None:
    assert is_junk_cite_key(key) is junk


def test_rekeyed_old_key_resolves_through_every_slug_door(store: Store) -> None:
    ref_id = _seed(store, "smith2024")

    receipt = rekey_paper(store, ref_id, "jones24", reason="test rekey", source="test")

    assert "smith2024" in receipt and "jones24" in receipt
    # The live scheme moved; the old value is a cite_key_alias redirect.
    assert store.find_ref_by_identifier("cite_key", "smith2024") is None
    assert store.find_ref_by_identifier("cite_key", "jones24") == ref_id
    assert store.find_ref_by_identifier("cite_key_alias", "smith2024") == ref_id
    old = store.get_ref(kind="paper", id="smith2024")
    assert old is not None and old.id == ref_id
    handle = resolve_handle_ref(store, "smith2024")
    assert handle is not None and handle.id == ref_id
    match = resolve_query_identifier(store, "smith2024")
    assert match.ref is not None and match.ref.id == ref_id
    assert _try_cite_key(store, "smith2024") == ref_id


def test_alias_never_shadows_another_papers_live_key(store: Store) -> None:
    """A live cite_key outranks an alias at read time — even though a
    reserved alias can no longer be *minted*, a directly inserted row
    (or legacy data) still resolves to the live owner."""
    ref_id = _seed(store, "smith2024")
    rekey_paper(store, ref_id, "jones24", reason="test rekey", source="test")
    handle = resolve_handle_ref(store, "smith2024")
    assert handle is not None and handle.id == ref_id

    owner = _seed(store, "smith2024")

    assert store.find_ref_by_identifier("cite_key", "smith2024") == owner
    # Every slug door returns the live owner, never the alias holder.
    got = store.get_ref(kind="paper", id="smith2024")
    assert got is not None and got.id == owner
    handle = resolve_handle_ref(store, "smith2024")
    assert handle is not None and handle.id == owner
    match = resolve_query_identifier(store, "smith2024")
    assert match.ref is not None and match.ref.id == owner
    assert _try_cite_key(store, "smith2024") == owner


def test_rekey_keeps_every_displaced_key_as_alias(store: Store) -> None:
    """A ref carrying several cite_key rows keeps each as a redirect."""
    ref_id = _seed(store, "smith2024")
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO ref_identifiers (id_kind, id_value, ref_id, source) "
            "VALUES ('cite_key', 'smith24x', %s, 'test')",
            (ref_id,),
        )

    rekey_paper(store, ref_id, "jones24", reason="test rekey", source="test")

    assert ("cite_key_alias", "smith2024") in _ident_rows(store, ref_id)
    assert ("cite_key_alias", "smith24x") in _ident_rows(store, ref_id)
    got = store.get_ref(kind="paper", id="smith2024")
    assert got is not None and got.id == ref_id
    got = store.get_ref(kind="paper", id="smith24x")
    assert got is not None and got.id == ref_id

    # A second rekey adds another alias without dropping the first.
    rekey_paper(store, ref_id, "jones25", reason="test rekey", source="test")
    got = store.get_ref(kind="paper", id="smith2024")
    assert got is not None and got.id == ref_id
    got = store.get_ref(kind="paper", id="jones24")
    assert got is not None and got.id == ref_id


def test_edit_door_remints_junk_key_on_author_repair(
    store: Store, handler: PaperHandler
) -> None:
    ref_id = _seed(store, "anon88")

    response = handler.edit(id=f"pa{ref_id}", authors=["Murty, Katta G."], year=1988)

    assert "re-minted" in response.body and "murty88" in response.body
    assert store.find_ref_by_identifier("cite_key", "murty88") == ref_id
    assert store.find_ref_by_identifier("cite_key_alias", "anon88") == ref_id
    got = store.get_ref(kind="paper", id="anon88")
    assert got is not None and got.id == ref_id
    events = store.events_for(ref_id, event="cite_key_changed")
    assert len(events) == 1
    assert events[0].source == "auto-remint"
    assert events[0].payload == {
        "old": "anon88",
        "new": "murty88",
        "reason": AUTO_REMINT_REASON,
    }


def test_edit_door_never_remints_a_real_key(
    store: Store, handler: PaperHandler
) -> None:
    ref_id = _seed(store, "smith24", authors=_MURTY, year=1988)

    response = handler.edit(id=f"pa{ref_id}", authors=["Jones, A."], year=2001)

    assert "re-minted" not in response.body
    assert store.find_ref_by_identifier("cite_key", "smith24") == ref_id
    assert store.events_for(ref_id, event="cite_key_changed") == []


def test_edit_door_no_usable_byline_leaves_junk_key(
    store: Store, handler: PaperHandler
) -> None:
    """A repair whose suggestion is still a junk mint does not rename."""
    ref_id = _seed(store, "anon88")

    response = handler.edit(id=f"pa{ref_id}", year=1990)

    assert "re-minted" not in response.body
    assert store.find_ref_by_identifier("cite_key", "anon88") == ref_id
    assert store.events_for(ref_id, event="cite_key_changed") == []


def test_remint_collision_lands_on_letter_suffix(store: Store) -> None:
    """A taken suggestion rolls to ``<base>a`` — never steals the key."""
    from precis.store.paper_rekey import remint_junk_cite_key

    owner = _seed(store, "murty88", authors=_MURTY, year=1988)
    ref_id = _seed(store, "anon88", authors=_MURTY, year=1988)

    receipt = remint_junk_cite_key(store, ref_id, source="test")

    assert receipt is not None and "murty88a" in receipt
    assert store.find_ref_by_identifier("cite_key", "murty88") == owner
    assert store.find_ref_by_identifier("cite_key", "murty88a") == ref_id


def test_apply_resolution_remints_after_metadata_write(store: Store) -> None:
    ref_id = _seed(store, "anon88")
    res = Resolution(
        ref_id=ref_id,
        verdict="auto",
        track="title",
        reason="test",
        title="A recovered title",
        author_dicts=_MURTY,
        year=1988,
    )

    apply_resolution(store, res, source="test")

    assert store.find_ref_by_identifier("cite_key", "murty88") == ref_id
    assert store.find_ref_by_identifier("cite_key_alias", "anon88") == ref_id
    events = store.events_for(ref_id, event="cite_key_changed")
    assert len(events) == 1
    assert events[0].payload["reason"] == AUTO_REMINT_REASON


def test_remediate_fix_remints_only_junk_keys(store: Store, tmp_path: Path) -> None:
    junk_id = _seed(store, "anon88")
    real_id = _seed(store, "smith24")
    junk_pdf = corpus_pdf_dest("anon88", tmp_path)
    real_pdf = corpus_pdf_dest("smith24", tmp_path)
    for pdf in (junk_pdf, real_pdf):
        pdf.parent.mkdir(parents=True, exist_ok=True)
        pdf.write_bytes(b"%PDF-1.4\nremint fixture\n")

    fixed = _apply_fix(
        store,
        _Suspect(ref_id=junk_id, cite_key="anon88", title="", authors=[]),
        junk_pdf,
        (tmp_path,),
        title="A recovered title",
        year=1988,
        author_dicts=_MURTY,
        author_names=["Murty, Katta G."],
        abstract="",
        keywords=[],
        doi="",
        source="test",
    )
    kept = _apply_fix(
        store,
        _Suspect(ref_id=real_id, cite_key="smith24", title="", authors=[]),
        real_pdf,
        (tmp_path,),
        title="Another title",
        year=1988,
        author_dicts=_MURTY,
        author_names=["Murty, Katta G."],
        abstract="",
        keywords=[],
        doi="",
        source="test",
    )

    assert fixed.new_cite_key == "murty88"
    assert store.find_ref_by_identifier("cite_key", "murty88") == junk_id
    assert store.find_ref_by_identifier("cite_key_alias", "anon88") == junk_id
    assert corpus_pdf_dest("murty88", tmp_path).is_file()
    assert not junk_pdf.exists()
    events = store.events_for(junk_id, event="cite_key_changed")
    assert len(events) == 1
    assert events[0].payload == {
        "old": "anon88",
        "new": "murty88",
        "reason": AUTO_REMINT_REASON,
    }
    # The real (possibly operator-set) key is never re-minted.
    assert kept.new_cite_key == "smith24"
    assert store.find_ref_by_identifier("cite_key", "smith24") == real_id
    assert real_pdf.is_file()


def test_inherit_missing_identity_remints_in_tx(store: Store) -> None:
    """A stub gaining a byline in a dedup merge drops its junk key."""
    survivor = _seed(store, "anon88", year=1988)
    # A junk-keyed loser has nothing to adopt — the suggester suffixes.
    loser = _seed(store, "anon99", authors=_MURTY, year=1988)

    with store.tx() as conn:
        filled = inherit_missing_identity(
            store,
            survivor_ref_id=survivor,
            loser_ref_id=loser,
            source="test",
            conn=conn,
        )

    assert "authors" in filled
    assert store.find_ref_by_identifier("cite_key", "murty88") == survivor
    assert store.find_ref_by_identifier("cite_key_alias", "anon88") == survivor
    events = store.events_for(survivor, event="cite_key_changed")
    assert len(events) == 1
    assert events[0].payload["reason"] == AUTO_REMINT_REASON


def test_inherit_missing_identity_adopts_losers_real_key(store: Store) -> None:
    """The survivor takes the loser's non-junk cite_key instead of
    minting a suffix — drafts citing the loser keep resolving after it
    retires."""
    survivor = _seed(store, "anon88", year=1988)
    loser = _seed(store, "murty88", authors=_MURTY, year=1988)

    with store.tx() as conn:
        filled = inherit_missing_identity(
            store,
            survivor_ref_id=survivor,
            loser_ref_id=loser,
            source="test",
            conn=conn,
        )

    assert "authors" in filled
    assert store.find_ref_by_identifier("cite_key", "murty88") == survivor
    # The loser's own key is gone — it moved, it was not aliased over.
    assert store.find_ref_by_identifier("cite_key", "anon99") is None
    assert store.find_ref_by_identifier("cite_key_alias", "anon88") == survivor
    events = store.events_for(survivor, event="cite_key_changed")
    assert len(events) == 1
    assert events[0].payload["reason"] == AUTO_REMINT_REASON
    assert events[0].payload["adopted_from"] == loser


def test_merge_migrates_loser_alias_redirects(store: Store) -> None:
    """A rekeyed loser folded into a survivor keeps its old keys
    resolving — the cite_key_alias rows move instead of dying on the
    retired ref."""
    from precis.ingest.dedup import merge_duplicate

    survivor = _seed(store, "murty88", authors=_MURTY, year=1988)
    loser = _seed(store, "loser24")
    # The loser was once rekeyed: winner24 -> loser24, alias stays.
    rekey_paper(store, loser, "loser24b", reason="test rekey", source="test")
    assert store.find_ref_by_identifier("cite_key_alias", "loser24") == loser

    with store.tx() as conn:
        merge_duplicate(
            store,
            survivor_ref_id=survivor,
            duplicate_ref_id=loser,
            source="test",
            reason="test merge",
            conn=conn,
        )

    assert store.find_ref_by_identifier("cite_key_alias", "loser24") == survivor
    got = store.get_ref(kind="paper", id="loser24")
    assert got is not None and got.id == survivor


def test_retired_cite_key_does_not_shadow_live_alias(store: Store) -> None:
    """A dead ref's leftover cite_key row must not outrank a live ref's
    alias for the same value — the redirect should win."""
    live = _seed(store, "smith99")
    rekey_paper(store, live, "jones99", reason="test rekey", source="test")
    dead = _seed(store, "smith99")  # direct insert: live cite_key over the alias
    with store.pool.connection() as conn:
        conn.execute("UPDATE refs SET retired_at = now() WHERE ref_id = %s", (dead,))

    got = store.get_ref(kind="paper", id="smith99")
    assert got is not None and got.id == live
    handle = resolve_handle_ref(store, "smith99")
    assert handle is not None and handle.id == live


def test_in_tx_remint_failure_leaves_caller_tx_usable(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The savepoint leg: a mid-remint SQL failure rolls back only the
    remint — the caller's transaction still commits."""
    survivor = _seed(store, "anon88", year=1988)
    loser = _seed(store, "anon99", authors=_MURTY, year=1988)

    def boom(*args: Any, **kwargs: Any) -> bool:
        raise RuntimeError("simulated mid-tx failure")

    monkeypatch.setattr(store, "set_ref_identifier", boom)
    with store.tx() as conn:
        inherit_missing_identity(
            store,
            survivor_ref_id=survivor,
            loser_ref_id=loser,
            source="test",
            conn=conn,
        )
        # The caller transaction is still usable after the failed leg.
        assert conn.execute("SELECT 1").fetchone() is not None

    assert store.find_ref_by_identifier("cite_key", "anon88") == survivor
    assert store.events_for(survivor, event="cite_key_changed") == []


def test_displaced_key_is_reserved_for_minting(store: Store) -> None:
    """gr521515 decision: an old key kept as an alias is RESERVED —
    ``suggest_cite_key`` never hands it out again."""
    ref_id = _seed(store, "smith24")
    rekey_paper(store, ref_id, "jones24", reason="test rekey", source="test")
    assert store.find_ref_by_identifier("cite_key_alias", "smith24") == ref_id

    suggested = store.suggest_cite_key([{"family": "Smith"}], 2024)

    assert suggested != "smith24"
    assert suggested == "smith24a"


def test_remint_freed_junk_key_is_not_reused(store: Store) -> None:
    """An ``anon88`` freed by a remint is never minted again — the
    ingest minter counts the alias row too."""
    from precis.ingest.db_writer import resolve_cite_key
    from precis.store.paper_rekey import remint_junk_cite_key

    ref_id = _seed(store, "anon88", authors=_MURTY, year=1988)
    remint_junk_cite_key(store, ref_id, source="test")
    assert store.find_ref_by_identifier("cite_key_alias", "anon88") == ref_id

    with store.pool.connection() as conn:
        assert resolve_cite_key("anon88", conn=conn) == "anon88a"


def test_rekey_refuses_a_key_another_ref_holds_as_alias(store: Store) -> None:
    """The rekey probe treats an alias-held value as taken."""
    first = _seed(store, "smith24")
    rekey_paper(store, first, "jones24", reason="test rekey", source="test")
    second = _seed(store, "other99")

    with pytest.raises(BadInput, match="reserved"):
        rekey_paper(store, second, "smith24", reason="test rekey", source="test")

    assert store.find_ref_by_identifier("cite_key", "other99") == second
    assert store.find_ref_by_identifier("cite_key_alias", "smith24") == first


def test_rekey_reclaims_its_own_displaced_key(store: Store) -> None:
    """A ref may move back to a key it itself displaced — its own
    alias is not a reservation against itself."""
    ref_id = _seed(store, "smith24")
    rekey_paper(store, ref_id, "jones24", reason="test rekey", source="test")

    receipt = rekey_paper(store, ref_id, "smith24", reason="revert", source="test")

    assert "smith24" in receipt
    assert store.find_ref_by_identifier("cite_key", "smith24") == ref_id
    assert store.find_ref_by_identifier("cite_key_alias", "jones24") == ref_id


def _patch_cli_runtime(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the CLI's config/runtime plumbing at the test store."""
    monkeypatch.setattr(
        remint_cli, "resolve_dsn", lambda override, cfg=None: "test-dsn"
    )
    monkeypatch.setattr(
        precis.config, "load_config", lambda: precis.config.PrecisConfig()
    )
    monkeypatch.setattr(
        precis.runtime,
        "build_runtime",
        lambda cfg: SimpleNamespace(store=store),
    )


def test_cli_dry_run_lists_candidates_and_writes_nothing(
    store: Store, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    junk_id = _seed(store, "anon88", authors=_MURTY, year=1988)
    _seed(store, "smith24", authors=_MURTY, year=1988)  # real key: skipped
    _seed(store, "anon13")  # no byline: suggestion stays junk — skipped
    _patch_cli_runtime(store, monkeypatch)
    before = _write_snapshot(store)

    remint_cli.run(argparse.Namespace(apply=False, limit=None, database_url=None))

    out = capsys.readouterr()
    assert "DRY-RUN" in out.err and "1 candidate" in out.err
    assert f"pa{junk_id}  anon88 -> murty88" in out.out
    assert _write_snapshot(store) == before


def test_cli_apply_rekeys_each_candidate(
    store: Store, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    junk_id = _seed(store, "anon88", authors=_MURTY, year=1988)
    _patch_cli_runtime(store, monkeypatch)

    remint_cli.run(argparse.Namespace(apply=True, limit=None, database_url=None))

    out = capsys.readouterr()
    assert f"pa{junk_id}  anon88 -> murty88" in out.out
    assert store.find_ref_by_identifier("cite_key", "murty88") == junk_id
    assert store.find_ref_by_identifier("cite_key_alias", "anon88") == junk_id
    events = store.events_for(junk_id, event="cite_key_changed")
    assert len(events) == 1
    assert events[0].source == "paper-remint-junk"


def test_cli_plans_collision_aware_keys(
    store: Store, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """Two junk papers that both mint ``murty88`` get distinct planned
    keys — the dry run names exactly what the apply writes."""
    first = _seed(store, "anon88", authors=_MURTY, year=1988)
    second = _seed(store, "anon88a", authors=_MURTY, year=1988)
    _patch_cli_runtime(store, monkeypatch)

    before = _write_snapshot(store)
    remint_cli.run(argparse.Namespace(apply=False, limit=None, database_url=None))

    out = capsys.readouterr()
    assert f"pa{first}  anon88 -> murty88" in out.out
    assert f"pa{second}  anon88a -> murty88a" in out.out
    assert _write_snapshot(store) == before

    remint_cli.run(argparse.Namespace(apply=True, limit=None, database_url=None))

    out = capsys.readouterr()
    assert f"pa{first}  anon88 -> murty88" in out.out
    assert f"pa{second}  anon88a -> murty88a" in out.out
    assert "rekeyed 2, skipped 0" in out.err
    assert store.find_ref_by_identifier("cite_key", "murty88") == first
    assert store.find_ref_by_identifier("cite_key", "murty88a") == second
    assert store.find_ref_by_identifier("cite_key_alias", "anon88") == first
    assert store.find_ref_by_identifier("cite_key_alias", "anon88a") == second


def test_cli_apply_reports_and_skips_failed_rows(
    store: Store, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """A row whose rekey refuses reports the reason and the sweep
    continues — no traceback, later rows still land."""
    first = _seed(store, "anon88", authors=_MURTY, year=1988)
    second = _seed(store, "anon99", authors=[{"family": "Chen"}], year=2001)
    _patch_cli_runtime(store, monkeypatch)

    def refuse_first(store: Store, ref_id: int, new_key: str, **kwargs: Any) -> str:
        if ref_id == first:
            raise BadInput("simulated mid-run claim", next="retry")
        return rekey_paper(store, ref_id, new_key, **kwargs)

    monkeypatch.setattr("precis.store.paper_rekey.rekey_paper", refuse_first)

    remint_cli.run(argparse.Namespace(apply=True, limit=None, database_url=None))

    out = capsys.readouterr()
    assert "skipped: simulated mid-run claim" in out.out
    assert "rekeyed 1, skipped 1" in out.err
    assert f"skipped pa{first} anon88 -> murty88" in out.err
    assert store.find_ref_by_identifier("cite_key", "anon88") == first
    assert store.find_ref_by_identifier("cite_key", "chen01") == second
