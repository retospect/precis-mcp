"""The operator CLI restores the addressed loser, without undoing transfers."""

from __future__ import annotations

from typing import Any

import pytest
from psycopg import Connection

from precis.cli import garden
from precis.cli.main import _build_parser
from precis.graph.garden.apply import apply_merge
from precis.ingest.dedup import merge_duplicate
from precis.store import Ref, Store
from precis.store.revision_context import current_revision_context


def _paper(store: Store, slug: str) -> int:
    return store.insert_ref(
        kind="paper", slug=slug, title="A reversible duplicate", provider="manual"
    ).id


def _pair(store: Store) -> tuple[int, int]:
    loser = _paper(store, "clirestoreloser2026")
    winner = _paper(store, "clirestorewinner2026")
    with store.tx() as conn:
        merge_duplicate(
            store,
            survivor_ref_id=winner,
            duplicate_ref_id=loser,
            source="test-garden-restore-cli",
            reason="duplicate",
            conn=conn,
        )
    return loser, winner


def _ref(store: Store, ref_id: int) -> Ref:
    return store.fetch_refs_by_ids([ref_id], include_deleted=True)[ref_id]


def _edges(store: Store, loser: int) -> list[tuple[Any, ...]]:
    with store.pool.connection() as conn:
        return conn.execute(
            "SELECT * FROM links WHERE dst_ref_id = %s OR src_ref_id = %s "
            "ORDER BY link_id",
            (loser, loser),
        ).fetchall()


def _run(store: Store, monkeypatch: pytest.MonkeyPatch, *args: str) -> None:
    parser = _build_parser(("garden",))
    monkeypatch.setattr(Store, "connect", lambda dsn: store)
    garden.run(parser.parse_args(["garden", "restore", *args, "--dsn", "test://"]))


@pytest.mark.parametrize("address", ["numeric", "handle", "slug"])
def test_restore_cli_uses_operator_transaction_and_original_row(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    address: str,
) -> None:
    loser, winner = _pair(store)
    winner_before = _ref(store, winner)
    winner_events = store.events_for(winner)
    door = store.restore_superseded_ref
    calls: list[int] = []

    def spy(
        ref_id: int,
        *,
        conn: Connection | None = None,
        notes: list[str] | None = None,
    ) -> bool:
        ctx = current_revision_context()
        assert ctx is not None
        assert (ctx.actor, ctx.trust_origin, ctx.trust_actor_type) == (
            "test-operator",
            "human",
            "operator",
        )
        assert ctx.reason == "incorrect duplicate"
        assert conn is not None
        assert conn.execute(
            "SELECT current_setting('precis.actor'), "
            "current_setting('precis.trust_actor_type')"
        ).fetchone() == ("test-operator", "operator")
        calls.append(ref_id)
        return door(ref_id, conn=conn, notes=notes)

    monkeypatch.setattr(store, "restore_superseded_ref", spy)
    target = {
        "numeric": str(loser),
        "handle": f"pa{loser}",
        "slug": "clirestoreloser2026",
    }[address]
    _run(
        store,
        monkeypatch,
        target,
        "--by",
        "test-operator",
        "--reason",
        "incorrect duplicate",
    )

    assert calls == [loser]
    assert _ref(store, loser).retired_at is None
    assert "superseded_by" not in _ref(store, loser).meta
    assert _edges(store, loser) == []
    assert _ref(store, winner) == winner_before
    assert store.events_for(winner) == winner_events
    events = store.events_for(loser, event="restored_superseded")
    assert len(events) == 1
    assert events[0].payload == {
        "actor": "test-operator",
        "reason": "incorrect duplicate",
        "superseded_by": winner,
    }
    output = capsys.readouterr().out
    assert f"restored pa{loser}" in output
    assert "Identifiers, body/PDF" in output
    assert "ordinary links are not moved back" in output

    before = _ref(store, loser)
    _run(store, monkeypatch, target)
    assert f"no change: pa{loser} is already live" in capsys.readouterr().out
    assert calls == [loser]
    assert _ref(store, loser) == before
    assert store.events_for(loser, event="restored_superseded") == events


@pytest.mark.parametrize("dry_run", [False, True])
def test_restore_cli_refuses_garden_merge_without_writes(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    dry_run: bool,
) -> None:
    loser = _paper(store, "cligardenloser2026")
    winner = _paper(store, "cligardenwinner2026")
    record = apply_merge(store, loser, winner, actor="operator")
    before, events, edges = (
        _ref(store, loser),
        store.events_for(loser),
        _edges(store, loser),
    )
    with pytest.raises(SystemExit) as exc:
        _run(store, monkeypatch, f"pa{loser}", *(["--dry-run"] if dry_run else []))
    assert exc.value.code == 2
    assert f"precis garden undo {record.merge_id}" in capsys.readouterr().err
    assert _ref(store, loser) == before
    assert store.events_for(loser) == events
    assert _edges(store, loser) == edges


@pytest.mark.parametrize("state", ["retired", "missing", "wrong-kind"])
def test_restore_cli_refuses_invalid_target(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    state: str,
) -> None:
    ref_id = (
        store.insert_ref(kind="memory", slug=None, title="Not a paper").id
        if state == "wrong-kind"
        else _paper(store, "cliplainretired2026")
    )
    if state == "retired":
        store.retire_ref(ref_id)
    before, events = _ref(store, ref_id), store.events_for(ref_id)
    with pytest.raises(SystemExit) as exc:
        _run(store, monkeypatch, "999999999" if state == "missing" else str(ref_id))
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert {
        "retired": "retired but not superseded",
        "missing": "no paper ref",
        "wrong-kind": "not paper",
    }[state] in err
    if state == "retired":
        assert "'retire': False" in err
    assert _ref(store, ref_id) == before
    assert store.events_for(ref_id) == events


def test_restore_cli_dry_run_does_not_write(
    store: Store, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    loser, winner = _pair(store)
    before, events, edges = (
        _ref(store, loser),
        store.events_for(loser),
        _edges(store, loser),
    )
    winner_before = _ref(store, winner)
    _run(store, monkeypatch, "clirestoreloser2026", "--dry-run")
    assert f"DRY RUN (no write) — would restore pa{loser}" in capsys.readouterr().out
    assert _ref(store, loser) == before
    assert _ref(store, winner) == winner_before
    assert store.events_for(loser) == events
    assert _edges(store, loser) == edges


def test_restore_cli_dry_run_surfaces_the_real_refusal(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A dry run must not print "would restore" when the real run
    refuses: it runs the same restore inside a rolled-back savepoint,
    so the refusal reaches the operator and nothing is written."""
    loser, _ = _pair(store)
    outsider = _paper(store, "clidryoutsider2026")
    # A live ref outside the chain holds the loser's rehomed key as a
    # cite_key_alias — the real restore refuses on the outside holder.
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE ref_identifiers SET ref_id = %s "
            "WHERE id_kind = 'cite_key_alias' AND id_value = %s",
            (outsider, "clirestoreloser2026"),
        )
        conn.commit()
    before, events, edges = (
        _ref(store, loser),
        store.events_for(loser),
        _edges(store, loser),
    )
    with pytest.raises(SystemExit) as exc:
        _run(store, monkeypatch, f"pa{loser}", "--dry-run")
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "DRY RUN" not in err
    assert f"pa{outsider}" in err and "cite_key_alias" in err
    assert "drop_identifier" in err
    assert _ref(store, loser) == before
    assert store.events_for(loser) == events
    assert _edges(store, loser) == edges
    assert (
        store.find_ref_by_identifier("cite_key_alias", "clirestoreloser2026")
        == outsider
    )


def test_restore_cli_rolls_back_restoration_and_event_together(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    loser, _ = _pair(store)
    before, events, edges = (
        _ref(store, loser),
        store.events_for(loser),
        _edges(store, loser),
    )
    append = store.append_event

    def fail_after_append(*args: Any, **kwargs: Any) -> int:
        assert kwargs["conn"] is not None
        append(*args, **kwargs)
        raise RuntimeError("abort restoration audit")

    monkeypatch.setattr(store, "append_event", fail_after_append)
    with pytest.raises(RuntimeError, match="abort restoration audit"):
        _run(store, monkeypatch, f"pa{loser}")
    assert _ref(store, loser) == before
    assert store.events_for(loser) == events
    assert _edges(store, loser) == edges
