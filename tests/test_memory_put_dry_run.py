"""``put(dry_run=True)`` on memory / finding / concept (gr548271): the
preview writes nothing, lists a seeded near-duplicate, proposes a kind by
rule, and repeats the create ack's ``Next:`` rows; ``dry_run=False`` is the
ordinary create."""

from __future__ import annotations

import pytest

from precis.cli.memory import SPACE_TAG, _created_id
from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers._put_preview import DRY_RUN_HEADER, kind_rule
from precis.handlers.concept import ConceptHandler
from precis.handlers.finding import FindingHandler
from precis.handlers.memory import MemoryHandler
from precis.runtime import PrecisRuntime
from precis.store import Store
from precis.utils import handle_registry

_TABLES = ("refs", "chunks", "links", "chunk_embeddings", "ref_tags", "ref_events")


def _h(kind: str, ref_id: int) -> str:
    h = handle_registry.try_format(kind, ref_id)
    assert h is not None
    return h


def _counts(store: Store) -> dict[str, int]:
    out: dict[str, int] = {}
    with store.pool.connection() as conn:
        for t in _TABLES:
            row = conn.execute(f"SELECT count(*) FROM {t}").fetchone()
            assert row is not None
            out[t] = int(row[0])
    return out


def _embed_card(hub: Hub, ref_id: int, text: str) -> None:
    """Embed ``ref_id``'s card as ``text`` — what the worker would store."""
    store = hub.live_store
    (cid,) = store.chunks.card_chunk_ids([ref_id])
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO chunk_embeddings (chunk_id, embedder, vector, status, attempts) "
            "VALUES (%s, 'bge-m3', %s, 'ok', 1) "
            "ON CONFLICT (chunk_id, embedder) DO UPDATE "
            "SET vector = EXCLUDED.vector, status = 'ok'",
            (cid, hub.embed_one(text)),
        )


def test_dry_run_writes_nothing(hub: Hub) -> None:
    store, handler = hub.live_store, MemoryHandler(hub=hub)
    target = _created_id(handler.put(text="target", title="Target", tags=[SPACE_TAG]))
    before = _counts(store)

    out = handler.put(
        text="the quokka scheduler stalls on a lapsed heartbeat",
        title="Quokka stall",
        tags=[SPACE_TAG, "section:gotchas"],
        link=_h("memory", target),
        rel="part-of",
        meta={"hook": "restart the quokka scheduler"},
        dry_run=True,
    ).body

    assert out.startswith(DRY_RUN_HEADER), out
    assert _counts(store) == before


def test_dry_run_lists_a_seeded_near_duplicate_in_its_space(hub: Hub) -> None:
    handler = MemoryHandler(hub=hub)
    title, body = "Quokka stall", "the quokka scheduler stalls on a lapsed heartbeat"
    dup = _created_id(handler.put(text=body, title=title, tags=[SPACE_TAG]))
    _embed_card(hub, dup, f"{title}\n{body}")
    other = _created_id(handler.put(text=body, title=title, tags=["SPACE:personal"]))
    _embed_card(hub, other, f"{title}\n{body}")

    out = handler.put(text=body, title=title, tags=[SPACE_TAG], dry_run=True).body

    assert f"near-duplicate {_h('memory', dup)} (0.00) — edit it instead?" in out
    assert f"nearest duplicates in {SPACE_TAG} (embedding distance):" in out
    assert f"  {_h('memory', dup)}  0.000  {title}" in out
    assert _h("memory", other) not in out


def test_dry_run_without_a_near_duplicate_has_no_retain_line(hub: Hub) -> None:
    handler = MemoryHandler(hub=hub)
    out = handler.put(
        text="a fresh unrelated thought", tags=[SPACE_TAG], dry_run=True
    ).body
    assert "near-duplicate" not in out
    assert f"nearest duplicates in {SPACE_TAG}: none within" in out


def test_dry_run_without_embedder_says_not_checked(hub_no_embedder: Hub) -> None:
    out = MemoryHandler(hub=hub_no_embedder).put(text="a thought", dry_run=True).body
    assert "nearest duplicates: not checked (no embedder)" in out


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("The {{c1::Faraday}} constant is 96485 C/mol", "anki"),
        (
            "Copper lowers the nitrate reduction overpotential.\n"
            "Measured in pc293 at pH 7.",
            "finding",
        ),
        ("Overpotential is the extra voltage beyond equilibrium.", "taxon"),
        ("Restart the worker when the queue stalls\nthen read its log", "memory"),
        # two different cites, or a question, are not one claim
        ("Copper lowers the overpotential per pc1 and pa2.", "memory"),
        ("Does copper lower the overpotential in pc293?", "memory"),
    ],
)
def test_kind_rule_fixtures(text: str, kind: str) -> None:
    assert kind_rule(text)[0] == kind


def test_dry_run_prints_the_kind_rule(hub: Hub) -> None:
    handler = MemoryHandler(hub=hub)
    plain = handler.put(text="Restart the worker when it stalls", dry_run=True).body
    assert "kind: memory (rule: default)" in plain
    cloze = handler.put(text="The {{c1::Faraday}} constant", dry_run=True).body
    assert "kind: anki (rule: cloze markup {{cN::)" in cloze


def test_dry_run_next_block_matches_the_real_put(hub: Hub) -> None:
    handler = MemoryHandler(hub=hub)
    hub_id = _created_id(
        handler.put(
            text="pumps cavitation impeller",
            title="Pump cavitation hub",
            tags=[SPACE_TAG, "section:index"],
        )
    )
    text, title = "cavitation damages the impeller", "Cavitation note"
    dry = handler.put(text=text, title=title, tags=[SPACE_TAG], dry_run=True).body
    real = handler.put(text=text, title=title, tags=[SPACE_TAG], dry_run=False)
    new = _h("memory", _created_id(real))

    target = _h("memory", hub_id)
    trip = "search(kind='memory', q='Cavitation note', view='index')"
    for me, out in (("<new>", dry), (new, real.body)):
        assert (
            f"link(kind='memory', id='{me}', target='{target}', rel='part-of')" in out
        )
        assert f"tag(kind='memory', id='{me}', add=['section:threads'])" in out
        assert trip in out
    assert "created memory" in real.body and DRY_RUN_HEADER not in real.body


def test_dry_run_part_of_link_counts_as_placed(hub: Hub) -> None:
    handler = MemoryHandler(hub=hub)
    hub_id = _created_id(
        handler.put(text="x", title="Root hub", tags=[SPACE_TAG, "section:index"])
    )
    out = handler.put(
        text="a gotcha",
        tags=[SPACE_TAG, "section:gotchas"],
        link=_h("memory", hub_id),
        rel="part-of",
        dry_run=True,
    ).body
    assert "rel='part-of')" not in out


def test_dry_run_flags_a_same_prefix_thread(hub: Hub) -> None:
    handler = MemoryHandler(hub=hub)
    peer = _created_id(
        handler.put(
            text="Left: none",
            title="Fleet wave — 2026-10-13",
            tags=[SPACE_TAG, "section:threads"],
        )
    )
    out = handler.put(
        text="Left: the reviewer pass",
        title="Fleet wave — 2026-10-14",
        tags=[SPACE_TAG, "section:threads"],
        dry_run=True,
    ).body
    assert f"thread node {_h('memory', peer)} already covers" in out


def test_dry_run_refuses_what_the_put_refuses(hub: Hub) -> None:
    handler = MemoryHandler(hub=hub)
    with pytest.raises(BadInput):
        handler.put(dry_run=True)
    with pytest.raises(BadInput):
        handler.put(text="x", tags=["STATUS:open"], dry_run=True)
    with pytest.raises(BadInput):
        handler.put(id=1, link="memory:2", dry_run=True)


def test_dry_run_through_dispatch_args(runtime_with_store: PrecisRuntime) -> None:
    rt = runtime_with_store
    before = _counts(rt.hub.live_store)
    out, is_error = rt.dispatch_with_status(
        "put", {"kind": "memory", "text": "a thought", "dry_run": True}
    )
    assert not is_error, out
    assert DRY_RUN_HEADER in out
    assert _counts(rt.hub.live_store) == before


def test_finding_dry_run_lists_duplicates_without_kind_rule(hub: Hub) -> None:
    store, handler = hub.live_store, FindingHandler(hub=hub)
    before = _counts(store)
    out = handler.put(
        title="Copper lowers the nitrate overpotential",
        body="claim text",
        cited_in="pc1",
        dry_run=True,
    ).body
    assert out.startswith(DRY_RUN_HEADER)
    assert "kind:" not in out
    assert "nearest duplicates" in out
    assert _counts(store) == before


def test_concept_dry_run_lists_a_seeded_near_duplicate(hub: Hub) -> None:
    store, handler = hub.live_store, ConceptHandler(hub=hub)
    text = "overpotential — the extra voltage beyond equilibrium"
    dup = _created_id(handler.put(text=text))
    _embed_card(hub, dup, handler._card_combined_text(text))
    before = _counts(store)

    out = handler.put(text=text, dry_run=True).body

    assert f"near-duplicate {_h('concept', dup)} (0.00) — edit it instead?" in out
    assert "kind:" not in out
    assert _counts(store) == before
    assert "created concept" in handler.put(text=text, dry_run=False).body
