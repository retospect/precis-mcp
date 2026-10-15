"""Graph trust state, slice 1: writers outside the verb path (migration 0214).

Pins the review findings on 84ce4675c: a non-verb writer of a converted
kind runs in a trust context, so a version-1 row it touches is re-stamped
rather than refused; and a door that also touches a ref of an unconverted
kind (a paper) leaves that ref at version 0, so the paper's own writers
(enrichers, ingest) keep working.
"""

from __future__ import annotations

import argparse
import importlib
import pkgutil
import uuid
from typing import Any

import pytest

import precis.handlers
from precis.errors import BadInput
from precis.ingest import dedup
from precis.protocol import Handler
from precis.store import Store
from precis.store.revision_context import (
    RevisionContext,
    TrustAct,
    current_revision_context,
    trust_door,
    trust_producer,
)
from precis.workers import chase


def _stamp(store: Store, kind: str, target_id: int) -> tuple[str, str, int]:
    state = store.trust_state(kind, target_id)
    assert state is not None
    return (state.origin, state.confidence, state.version)


def _paper(store: Store, title: str) -> int:
    slug = f"trust-{uuid.uuid4().hex[:10]}"
    return store.insert_ref(kind="paper", slug=slug, title=title, meta={}).id


def _handler_kinds() -> list[tuple[str, str | None]]:
    """(kind, trust_origin) for every core handler class."""
    for mod in pkgutil.iter_modules(precis.handlers.__path__):
        importlib.import_module(f"precis.handlers.{mod.name}")
    seen: list[tuple[str, str | None]] = []
    todo: list[type[Handler]] = [Handler]
    while todo:
        for sub in todo.pop().__subclasses__():
            todo.append(sub)
            kind = getattr(sub, "kind", None)
            if isinstance(kind, str):
                seen.append((kind, sub.trust_origin))
    return seen


def _verb_finding(store: Store) -> int:
    """A finding as put(kind='finding') makes it: generated, version 1."""
    with trust_producer("generated", actor_type="agent"):
        return store.insert_ref(
            kind="finding", slug=None, title="claim A", meta={"scope": "s"}
        ).id


# ── unconverted kinds stay version 0 under a door ──────────────────────────


def test_paper_merge_leaves_the_survivor_paper_at_version_zero(store: Store) -> None:
    survivor = _paper(store, "")
    loser = _paper(store, "A real title")
    with store.tx() as conn:
        dedup.merge_duplicate(
            store,
            survivor_ref_id=survivor,
            duplicate_ref_id=loser,
            source="test",
            reason="same doi",
            conn=conn,
        )
    with store.pool.connection() as conn:
        title = conn.execute(
            "SELECT title FROM refs WHERE ref_id = %s", (survivor,)
        ).fetchone()
    assert title == ("A real title",)  # the door filled it
    assert _stamp(store, "ref", survivor) == ("unknown", "unknown", 0)
    # the enrichers write without a context (openalex_meta.enrich_ref)
    store.update_paper_fields(
        survivor,
        title="Enriched title",
        year=2020,
        authors=[{"name": "A. Author"}],
        source="openalex-enrich",
    )
    assert _stamp(store, "ref", survivor) == ("unknown", "unknown", 0)


def test_a_door_restamps_only_links_touching_converted_kinds(store: Store) -> None:
    finding = _verb_finding(store)
    old_paper, new_paper, other = (_paper(store, t) for t in ("old", "new", "x"))
    with trust_producer("generated", actor_type="agent"):
        store.add_link(src_ref_id=finding, dst_ref_id=old_paper, relation="cites")
    store.add_link(src_ref_id=other, dst_ref_id=old_paper, relation="cites")
    with trust_producer("computed", actor_type="worker"), store.tx() as conn:
        store.migrate_links(old_paper, new_paper, conn=conn)
    with store.pool.connection() as conn:
        moved: dict[int, int] = dict(
            conn.execute(
                "SELECT src_ref_id, link_id FROM links "
                "WHERE dst_ref_id = %s AND relation = 'cites'",
                (new_paper,),
            ).fetchall()
        )
    assert set(moved) == {finding, other}
    assert _stamp(store, "link", moved[finding]) == ("computed", "hunch", 1)
    assert _stamp(store, "link", moved[other]) == ("unknown", "unknown", 0)
    assert _stamp(store, "ref", new_paper) == ("unknown", "unknown", 0)


def test_acts_on_unconverted_kinds_are_refused(store: Store) -> None:
    paper = _paper(store, "p")
    state = store.trust_state("ref", paper)
    assert state is not None
    act = TrustAct(
        action="consider",
        target_kind="ref",
        target_id=paper,
        expected_sha=state.sha,
        reason="read it",
    )
    with pytest.raises(BadInput, match="not trust-converted"):
        store.record_trust_act(act, actor="t", actor_type="operator")
    assert _stamp(store, "ref", paper) == ("unknown", "unknown", 0)


def test_converted_kinds_match_the_handlers(store: Store) -> None:
    kinds = _handler_kinds()
    declared = {kind for kind, origin in kinds if origin is not None}
    assert declared == {"memory", "finding", "concept", "taxon", "todo", "gripe"}
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT k FROM unnest(%s::text[]) k WHERE precis_trust_kind_converted(k)",
            (sorted({kind for kind, _ in kinds}),),
        ).fetchall()
    assert {r[0] for r in rows} == declared


# ── converted kinds: non-verb writers carry a context ──────────────────────


def test_chase_snapshot_restamps_a_verb_made_finding(store: Store) -> None:
    finding = _verb_finding(store)
    paper = _paper(store, "primary")
    chain: list[dict[str, Any]] = [
        {"ref_id": paper, "verification": {"caveats": ["in vitro only"]}}
    ]
    with store.tx() as conn:
        chase._snapshot_chain(conn, store, finding, chain)
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta FROM refs WHERE ref_id = %s", (finding,)
        ).fetchone()
    assert row is not None
    meta = row[0]
    assert meta["caveats"] == ["in vitro only"]
    assert _stamp(store, "ref", finding) == ("generated", "hunch", 1)
    assert store.trust_acts_for("ref", finding)[-1]["action"] == "revise"


def test_a_door_handed_an_open_transaction_stamps_then_restores(
    store: Store,
) -> None:
    @trust_door("computed")
    def door(*, conn: Any) -> int:
        return store.insert_ref(
            kind="memory", slug=None, title="in door", meta={}, conn=conn
        ).id

    with store.tx() as conn:
        inside = door(conn=conn)
        after = store.insert_ref(
            kind="memory", slug=None, title="after door", meta={}, conn=conn
        ).id
    assert _stamp(store, "ref", inside) == ("computed", "hunch", 1)
    assert _stamp(store, "ref", after) == ("unknown", "unknown", 0)


def test_purging_a_parent_restamps_its_stamped_child(store: Store) -> None:
    from precis.cli.maintenance import _purge_soft_deleted

    with trust_producer("generated", actor_type="agent"):
        parent = store.insert_ref(kind="memory", slug=None, title="p", meta={}).id
        child = store.insert_ref(kind="memory", slug=None, title="c", meta={}).id
        store.set_parent(child, parent)
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET retired_at = now() - interval '40 days' WHERE ref_id = %s",
            (parent,),
        )
    assert _purge_soft_deleted(store=store, older_than_days=30, dry_run=False) >= 1
    assert _stamp(store, "ref", child) == ("computed", "hunch", 1)


def test_a_tag_overflow_chunk_is_not_the_refs_body(store: Store) -> None:
    finding = _verb_finding(store)
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO chunks (ref_id, ord, chunk_kind, text, meta) "
            "VALUES (%s, 7, 'tag_overflow', 'a long ask-user value', '{}')",
            (finding,),
        )
    assert _stamp(store, "ref", finding) == ("generated", "hunch", 1)


def test_cli_commands_writing_converted_kinds_run_as_producers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli_main = importlib.import_module("precis.cli.main")
    from precis.cli.registry import COMMANDS

    trust = {c.name: c.trust for c in COMMANDS if c.trust is not None}
    assert trust == {"memory": "generated", "migrate-refs": "computed"}
    seen: list[RevisionContext | None] = []

    class _Module:
        @staticmethod
        def run(_args: object) -> None:
            seen.append(current_revision_context())

    monkeypatch.setattr(cli_main, "_load", lambda _cmd: _Module)
    memory = next(c for c in COMMANDS if c.name == "memory")
    cli_main._call(memory, argparse.Namespace())
    (ctx,) = seen
    assert ctx is not None
    assert (ctx.trust_origin, ctx.trust_actor_type) == ("generated", "operator")
