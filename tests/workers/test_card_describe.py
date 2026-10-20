"""Tests for ``workers/card_describe.py`` (gr497688 S1).

DB-backed via the ``store``/``hub`` fixtures with an injected fake LLM
client (``.complete`` shape only) — no network, no real model, and the
worker never touches an embedder.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from precis.dispatch import Hub
from precis.handlers.cad import CadHandler
from precis.handlers.pcb import PcbHandler
from precis.handlers.structure import StructureHandler
from precis.workers.card_describe import run_card_describe_pass

_PD = (
    '{"cell": {"a": 10.0, "b": 10.0, "c": 10.0, "pbc": [true, true, false]},'
    ' "ops": ['
    '{"op": "add_atom", "element": "Pd", "frac": [0.1, 0.1, 0.1]},'
    '{"op": "add_atom", "element": "Pd", "frac": [0.3, 0.1, 0.1]},'
    '{"op": "add_atom", "element": "O", "frac": [0.2, 0.2, 0.2]}'
    "]}"
)

_DESC = (
    "A palladium oxide slab with in-plane periodicity, likely a model "
    "catalyst surface for oxidation chemistry studies."
)


class _FakeClient:
    """Records calls; answers with a fixed description."""

    def __init__(self, text: str = _DESC) -> None:
        self.text = text
        self.calls: list[list[dict[str, str]]] = []

    def complete(self, messages: list[dict[str, str]]) -> Any:
        self.calls.append(messages)
        return SimpleNamespace(text=self.text, model="fake-sonnet", total_tokens=5)


def _desc_chunks(store: Any, ref_id: int) -> list[tuple[int, str, dict]]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT chunk_id, text, meta FROM chunks "
            "WHERE ref_id = %s AND chunk_kind = 'card_description'",
            (ref_id,),
        ).fetchall()
    return [(int(r[0]), str(r[1]), dict(r[2])) for r in rows]


def _put_structure(hub: Any, slug: str = "pdo") -> int:
    StructureHandler(hub=hub).put(id=slug, text=_PD)
    ref = hub.store.get_ref(kind="structure", id=slug)
    assert ref is not None
    return int(ref.id)


def test_missing_description_gets_exactly_one_chunk(store: Any, hub: Any) -> None:
    ref_id = _put_structure(hub)
    client = _FakeClient()

    r = run_card_describe_pass(store, client=client, batch_size=10)

    assert (r["claimed"], r["ok"], r["failed"]) == (1, 1, 0)
    chunks = _desc_chunks(store, ref_id)
    assert len(chunks) == 1
    assert chunks[0][1] == _DESC
    meta = chunks[0][2]
    assert meta["generator"] == "card_describe"
    assert meta["model"] == "fake-sonnet"
    assert meta["source_sha"]


def test_rerun_is_a_noop(store: Any, hub: Any) -> None:
    ref_id = _put_structure(hub)
    client = _FakeClient()
    run_card_describe_pass(store, client=client, batch_size=10)

    again = _FakeClient()
    r = run_card_describe_pass(store, client=again, batch_size=10)

    assert (r["claimed"], r["ok"], r["failed"]) == (0, 0, 0)
    assert again.calls == []
    assert len(_desc_chunks(store, ref_id)) == 1


def test_edited_card_regenerates_one_chunk(store: Any, hub: Any) -> None:
    ref_id = _put_structure(hub)
    run_card_describe_pass(store, client=_FakeClient(), batch_size=10)
    first = _desc_chunks(store, ref_id)[0]

    StructureHandler(hub=hub).edit(
        id="pdo", ops=[{"op": "add_atom", "element": "N", "frac": [0.4, 0.4, 0.1]}]
    )
    client2 = _FakeClient(
        "A nitrogen-doped palladium oxide slab, plausibly a doped "
        "catalyst surface for selective oxidation."
    )
    r = run_card_describe_pass(store, client=client2, batch_size=10)

    assert (r["claimed"], r["ok"], r["failed"]) == (1, 1, 0)
    chunks = _desc_chunks(store, ref_id)
    assert len(chunks) == 1  # DELETE+INSERT replaced, never a second row
    assert chunks[0][0] != first[0]  # replaced, not updated in place
    assert "nitrogen-doped" in chunks[0][1]
    assert chunks[0][2]["source_sha"] != first[2]["source_sha"]


def test_worker_never_embeds(store: Any, hub: Any) -> None:
    ref_id = _put_structure(hub)
    run_card_describe_pass(store, client=_FakeClient(), batch_size=10)

    with store.pool.connection() as conn:
        n = conn.execute(
            "SELECT count(*) FROM chunk_embeddings ce "
            "JOIN chunks c ON c.chunk_id = ce.chunk_id "
            "WHERE c.ref_id = %s AND c.chunk_kind = 'card_description'",
            (ref_id,),
        ).fetchone()
    assert n is not None and n[0] == 0


def test_search_finds_ref_by_description_only_phrase(store: Any, hub: Any) -> None:
    _put_structure(hub)
    run_card_describe_pass(store, client=_FakeClient(), batch_size=10)

    # 'catalyst surface' appears only in the generated description, not
    # in the card text — a lexical hit means card_description is in
    # structure search's card_kinds.
    res = StructureHandler(hub=hub).search(q="catalyst", mode="lexical")
    assert "pdo" in res.body


def test_pre_watermark_card_is_described_after_edit(store: Any, hub: Any) -> None:
    """An old (pre-watermark) structure that is edited re-mints its
    ``card_combined`` after the watermark — DELETE+INSERT gives it a new
    created_at — so the edit claims it and it gets exactly one
    description (gr497688 ruling 3)."""
    ref_id = _put_structure(hub)
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE chunks SET created_at = "
            "(SELECT created_at FROM chunk_kinds WHERE slug = 'card_description')"
            " - interval '1 second' WHERE ref_id = %s",
            (ref_id,),
        )
        conn.commit()

    # Untouched old card: below the watermark, never claimed.
    r = run_card_describe_pass(store, client=_FakeClient(), batch_size=10)
    assert r["claimed"] == 0
    assert _desc_chunks(store, ref_id) == []

    # The edit re-mints card_combined (new created_at) -> claimed.
    StructureHandler(hub=hub).edit(
        id="pdo", ops=[{"op": "add_atom", "element": "N", "frac": [0.4, 0.4, 0.1]}]
    )
    r = run_card_describe_pass(store, client=_FakeClient(), batch_size=10)
    assert (r["claimed"], r["ok"], r["failed"]) == (1, 1, 0)
    assert len(_desc_chunks(store, ref_id)) == 1


def test_card_without_description_chunk_is_skipped_after_watermark(
    store: Any,
) -> None:
    """Refs whose card predates the `card_description` kind registration
    are S4's backfill, not this worker's — simulate by rewriting the
    card row's created_at to before the kind's."""
    with store.pool.connection() as conn:
        ref_id = conn.execute(
            "INSERT INTO refs (kind, set_by, title) VALUES ('structure', 'system', 'old')"
            " RETURNING ref_id"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO chunks (ref_id, set_by, ord, chunk_kind, text) "
            "VALUES (%s, 'agent', -1, 'card_combined', 'old card')",
            (ref_id,),
        )
        conn.execute(
            "UPDATE chunks SET created_at = "
            "(SELECT created_at FROM chunk_kinds WHERE slug = 'card_description')"
            " - interval '1 second' WHERE ref_id = %s",
            (ref_id,),
        )
        conn.commit()

    r = run_card_describe_pass(store, client=_FakeClient(), batch_size=10)
    assert r["claimed"] == 0
    assert _desc_chunks(store, ref_id) == []


# ── S2: cad + pcb ─────────────────────────────────────────────────────

_FLANGE = """
component flange
plate     add  cyl:r25mmh8mm
hub_bore  cut  cyl:r8mmh10mm    @0mm,0mm,-1mm
bolts     cut  cyl:r2.5mmh10mm  @18mm,0mm,-1mm  polar:n6r18mm
"""

_PCB_DESIGN = {
    "components": [
        {"refdes": "U1", "label": "ewod driver", "pins": [{"name": "1"}]},
        {"refdes": "J1", "label": "conn", "pins": [{"name": "1"}]},
    ],
    "nets": [{"name": "VHV"}, {"name": "GND"}],
}


def _put_cad(store: Any) -> int:
    CadHandler(hub=Hub(store=store)).put(id="flange", text=_FLANGE)
    ref = store.get_ref(kind="cad", id="flange")
    assert ref is not None
    return int(ref.id)


def _put_pcb(store: Any, slug: str = "b1") -> int:
    PcbHandler(hub=Hub(store=store)).put(id=slug, args=dict(_PCB_DESIGN))
    ref = store.get_ref(kind="pcb", id=slug)
    assert ref is not None
    return int(ref.id)


def test_cad_description_lifecycle_and_search(store: Any) -> None:
    ref_id = _put_cad(store)
    client = _FakeClient(
        "A flanged hub plate with a central bore and a six-bolt circle, "
        "plausibly a shaft coupling or motor-mount interface."
    )
    r = run_card_describe_pass(store, client=client, batch_size=10)
    assert (r["claimed"], r["ok"], r["failed"]) == (1, 1, 0)
    assert len(_desc_chunks(store, ref_id)) == 1

    # re-run is a no-op
    r = run_card_describe_pass(store, client=_FakeClient(), batch_size=10)
    assert r["claimed"] == 0

    # description-only phrase -> lexical hit via card_kinds
    res = CadHandler(hub=Hub(store=store)).search(q="coupling", mode="lexical")
    assert "flange" in res.body

    # edit (a re-put that changes the design) re-mints the card -> regen
    CadHandler(hub=Hub(store=store)).put(id="flange", text="plate add cyl:r10mmh5mm")
    r = run_card_describe_pass(store, client=_FakeClient(), batch_size=10)
    assert r["claimed"] == 1
    assert len(_desc_chunks(store, ref_id)) == 1


def test_pcb_description_lifecycle_and_search(store: Any) -> None:
    ref_id = _put_pcb(store)
    client = _FakeClient(
        "A two-part board pairing an EWOD high-voltage driver with a "
        "connector, plausibly an electrowetting electrode controller."
    )
    r = run_card_describe_pass(store, client=client, batch_size=10)
    assert (r["claimed"], r["ok"], r["failed"]) == (1, 1, 0)
    assert len(_desc_chunks(store, ref_id)) == 1

    r = run_card_describe_pass(store, client=_FakeClient(), batch_size=10)
    assert r["claimed"] == 0

    res = PcbHandler(hub=Hub(store=store)).search(q="electrowetting", mode="lexical")
    assert "b1" in res.body

    # a put that changes the design re-mints the card -> regen
    PcbHandler(hub=Hub(store=store)).put(
        id="b1",
        args={
            "components": [
                {"refdes": "U1", "label": "ewod driver", "pins": [{"name": "1"}]},
                {"refdes": "J1", "label": "conn", "pins": [{"name": "1"}]},
                {"refdes": "C1", "label": "100nF", "pins": [{"name": "1"}]},
            ],
            "nets": [{"name": "VHV"}, {"name": "GND"}],
        },
    )
    r = run_card_describe_pass(store, client=_FakeClient(), batch_size=10)
    assert r["claimed"] == 1
    assert len(_desc_chunks(store, ref_id)) == 1


def test_claude_p_json_reply_is_parsed(store: Any, hub: Any) -> None:
    """gr500371: CLAUDE_P only accepts a JSON block, surfaced as res.data;
    the description is its `description` field, not the raw envelope."""
    ref_id = _put_structure(hub)
    desc = "A PdO slab, plausibly a catalyst surface model for CO oxidation."

    class _JsonClient(_FakeClient):
        def complete(self, messages: list[dict[str, str]]) -> Any:
            self.calls.append(messages)
            return SimpleNamespace(
                text=json.dumps({"description": desc}),
                data={"description": desc},
                model="claude-sonnet-5",
            )

    client = _JsonClient()
    r = run_card_describe_pass(store, client=client, batch_size=10)

    assert (r["claimed"], r["ok"], r["failed"]) == (1, 1, 0)
    assert _desc_chunks(store, ref_id)[0][1] == desc
    assert "JSON" in client.calls[0][0]["content"]


def test_truncated_json_reply_fails_not_stored(store: Any, hub: Any) -> None:
    """A JSON-looking reply with no usable `description` (truncated, wrong
    shape) is a per-item failure to retry, never stored as the card text."""
    ref_id = _put_structure(hub)

    class _TruncClient(_FakeClient):
        def complete(self, messages: list[dict[str, str]]) -> Any:
            self.calls.append(messages)
            return SimpleNamespace(
                text='{"description": "A PdO slab, plausibly a cat',
                data=None,
                model="local",
            )

    r = run_card_describe_pass(store, client=_TruncClient(), batch_size=10)

    assert (r["claimed"], r["ok"], r["failed"]) == (1, 0, 1)
    assert _desc_chunks(store, ref_id) == []
