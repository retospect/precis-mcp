"""Tests for ``workers/gen_abstract.py`` (gr635825).

DB-backed via the ``store`` fixture with a fake LLM client — no network.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from precis.dispatch import Hub
from precis.handlers.paper import PaperHandler
from precis.ingest.cards import GENERATED_LABEL, ensure_abstract_card
from precis.workers.gen_abstract import (
    NO_SUMMARY,
    body_input,
    clean_summary,
    run_gen_abstract_pass,
)
from tests.workers._helpers import seed_chunk, seed_ref

_PROSE = (
    "We measured the thermal transport of silicon nanowires at room "
    "temperature using a suspended microdevice, and found that the "
    "conductivity falls well below the bulk value as the wire diameter "
    "shrinks from one hundred to twenty nanometres across all samples."
)
_SUMMARY = (
    "This study measures thermal transport in silicon nanowires with a "
    "suspended microdevice at room temperature. Conductivity falls "
    "below the bulk value as the diameter shrinks from 100 nm to 20 nm, "
    "reflecting boundary scattering of phonons. The measurements cover "
    "several wires and show a monotonic decrease with diameter, "
    "supporting a size-dependent model of heat conduction in nanostructures."
)
_MASTHEAD = "J. Zhao, Department of Physics, University of Somewhere, Somewhere"


class _Client:
    def __init__(self, text: str = _SUMMARY) -> None:
        self.text = text
        self.calls: list[list[dict[str, str]]] = []

    def complete(self, messages: list[dict[str, str]]) -> Any:
        self.calls.append(messages)
        return SimpleNamespace(text=self.text, model="fake-model")


def _seed(store: Any, *, n: int = 10, first: str | None = None) -> int:
    ref_id = seed_ref(store, title="Thermal transport in silicon nanowires")
    for i in range(n):
        seed_chunk(
            store,
            ref_id=ref_id,
            text=first if (i == 0 and first) else _PROSE + f" Sample {i}.",
            ord=i,
        )
    return ref_id


def _gen(store: Any, ref_id: int) -> list[tuple[str, dict]]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT text, meta FROM chunks WHERE ref_id = %s "
            "AND chunk_kind = 'card_gen_abstract'",
            (ref_id,),
        ).fetchall()
    return [(str(r[0]), dict(r[1])) for r in rows]


def test_body_input_drops_mastheads_and_thin_bodies() -> None:
    block = body_input([(_MASTHEAD, None)] + [(_PROSE, None)] * 5, "T")
    assert _MASTHEAD not in block
    assert body_input([(_MASTHEAD, None)] * 5, "T") == ""


def test_clean_summary_refuses_decline_and_fragments() -> None:
    assert clean_summary(NO_SUMMARY) == ""
    assert clean_summary("too short") == ""
    assert clean_summary("Abstract: " + _SUMMARY) == _SUMMARY


def test_pass_writes_one_labelled_chunk_and_is_idempotent(store: Any) -> None:
    ref_id = _seed(store)
    client = _Client()
    r = run_gen_abstract_pass(store, client)
    assert r["ok"] == 1
    ((text, meta),) = _gen(store, ref_id)
    assert text == _SUMMARY
    assert meta["generated"] is True
    assert "BODY TEXT" in client.calls[0][0]["content"]
    run_gen_abstract_pass(store, client)
    assert len(client.calls) == 1
    assert len(_gen(store, ref_id)) == 1


def test_abstract_on_file_is_never_claimed(store: Any) -> None:
    ref_id = _seed(store)
    with store.pool.connection() as conn:
        conn.execute(
            'UPDATE refs SET meta = \'{"abstract": "Published."}\' WHERE ref_id = %s',
            (ref_id,),
        )
        conn.commit()
    client = _Client()
    assert run_gen_abstract_pass(store, client)["claimed"] == 0
    assert not client.calls


def test_masthead_only_body_never_reaches_the_model(store: Any) -> None:
    ref_id = seed_ref(store, title="Zhao")
    for i in range(10):
        seed_chunk(store, ref_id=ref_id, text=_MASTHEAD, ord=i)
    client = _Client()
    r = run_gen_abstract_pass(store, client)
    assert r["skipped"] == 1
    assert not client.calls
    assert _gen(store, ref_id) == []


def test_decline_writes_nothing(store: Any) -> None:
    ref_id = _seed(store)
    r = run_gen_abstract_pass(store, _Client(NO_SUMMARY))
    assert r["failed"] == 1
    assert _gen(store, ref_id) == []


def test_view_abstract_serves_the_labelled_summary(store: Any) -> None:
    ref_id = _seed(store)
    run_gen_abstract_pass(store, _Client())
    handler = PaperHandler(hub=Hub(store=store))
    ref = store.get_ref(kind="paper", id=ref_id)
    resp = handler._render_view(ref, "abstract")
    assert GENERATED_LABEL in resp.body
    assert _SUMMARY in resp.body


def test_publisher_abstract_supersedes_generated(store: Any) -> None:
    ref_id = _seed(store)
    run_gen_abstract_pass(store, _Client())
    with store.pool.connection() as conn:
        assert ensure_abstract_card(
            conn, ref_id, set_by="system", abstract="The published abstract."
        )
        conn.commit()
    assert _gen(store, ref_id) == []
