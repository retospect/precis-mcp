"""pcb-datasheet-review slice 4 (in-scope 6 — re-derivable verdicts):
``checklist_verdicts`` gains ``origin`` (human = sticky / machine =
disposable) and ``inputs`` (the ``{name: revision}`` map of what the
verdict consumed); staleness extends from "target changed / item
revised" to "a consumed input changed"; ``op='forget'`` retires
machine-origin verdicts — optionally per item or per consumed input —
and never a human one.

Covered here:

* verdicts record origin + inputs through ``op='verdict'`` and the
  store layer (bad origin / non-dict inputs refused);
* a bumped input revision marks exactly the verdicts that consumed it
  ``stale (input changed: <name>)`` — human verdicts included (shown
  stale, never silently replaced);
* ``op='forget'`` drops machine verdicts (whole target, per item, or
  per consumed input) and keeps human verdicts;
* an input name nothing resolves is never a staleness trigger.
"""

from __future__ import annotations

from typing import Any

import pytest

import precis.pcb as pcb_pkg
from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.checklist import ChecklistHandler
from precis.runtime import PrecisRuntime
from precis.store import Store
from precis.tools import core as tools_core

pytestmark = pytest.mark.db

_MARGIN_INPUT = "margin:fixed-derating"


def _handler(store: Store) -> ChecklistHandler:
    return ChecklistHandler(hub=Hub(store=store))


def _board(store: Store, slug: str = "cl4-board") -> int:
    ref, _created, _counts = store.pcb_apply(
        slug=slug,
        title=slug,
        components=[
            {"refdes": "U1", "label": "MCU", "pins": [{"name": "1"}, {"name": "2"}]},
        ],
        nets=[{"name": "VCC"}, {"name": "GND"}],
        connections=[
            {"net": "VCC", "refdes": "U1", "pin": "1"},
            {"net": "GND", "refdes": "U1", "pin": "2"},
        ],
    )
    return int(ref.id)


def _checklist(store: Store, name: str = "cl4") -> None:
    _handler(store).put(
        id=name,
        items=[
            {
                "name": "margin-checked",
                "phase": "schematic",
                "severity": "blocking",
                "prevents": "a margin failure ships",
            },
            {
                "name": "other-item",
                "phase": "schematic",
                "severity": "advisory",
                "prevents": "an unrelated failure ships",
            },
        ],
    )


def _status_body(h: ChecklistHandler, name: str, target: str) -> str:
    return h.get(id=name, target=target).body


def _assign(h: ChecklistHandler, name: str, target: str) -> None:
    """A local checklist has no ``default_for`` — the status view needs
    an explicit assignment."""
    h.edit(id=name, op="assign", target=target)


# ── recording origin + inputs ─────────────────────────────────────────


def test_verdict_records_origin_and_inputs(store: Store) -> None:
    _board(store)
    _checklist(store)
    h = _handler(store)
    h.edit(
        id="cl4",
        op="verdict",
        target="pcb:cl4-board",
        item="margin-checked",
        verdict="pass",
        origin="machine",
        inputs={_MARGIN_INPUT: 1},
    )
    cl = store.checklist_get("cl4")
    assert cl is not None
    ref = store.get_ref(kind="pcb", id="cl4-board")
    assert ref is not None
    v = store.checklist_verdict_latest(
        target_ref_id=ref.id,
        checklist_id=cl["id"],
        item_name="margin-checked",
    )
    assert v is not None
    assert v["origin"] == "machine"
    assert v["inputs"] == {_MARGIN_INPUT: 1}


def test_verdict_defaults_to_human_origin(store: Store) -> None:
    """A bare ``op='verdict'`` (and the notes vocab's 'user' default)
    records a HUMAN verdict — nothing existing turns droppable."""
    _board(store)
    _checklist(store)
    h = _handler(store)
    h.edit(
        id="cl4",
        op="verdict",
        target="pcb:cl4-board",
        item="margin-checked",
        verdict="pass",
    )
    cl = store.checklist_get("cl4")
    assert cl is not None
    ref = store.get_ref(kind="pcb", id="cl4-board")
    assert ref is not None
    v = store.checklist_verdict_latest(
        target_ref_id=ref.id,
        checklist_id=cl["id"],
        item_name="margin-checked",
    )
    assert v is not None and v["origin"] == "human" and v["inputs"] == {}


def test_verdict_rejects_bad_origin_and_inputs(store: Store) -> None:
    _board(store)
    _checklist(store)
    h = _handler(store)
    with pytest.raises(BadInput, match="origin"):
        h.edit(
            id="cl4",
            op="verdict",
            target="pcb:cl4-board",
            item="margin-checked",
            verdict="pass",
            origin="robot",
        )
    not_a_dict: Any = "not-a-dict"
    with pytest.raises(BadInput, match="inputs"):
        h.edit(
            id="cl4",
            op="verdict",
            target="pcb:cl4-board",
            item="margin-checked",
            verdict="pass",
            inputs=not_a_dict,
        )


def test_store_rejects_unknown_verdict_origin(store: Store) -> None:
    _board(store)
    _checklist(store)
    cl = store.checklist_get("cl4")
    ref = store.get_ref(kind="pcb", id="cl4-board")
    assert cl is not None and ref is not None
    with pytest.raises(ValueError, match="origin"):
        store.checklist_verdict_insert(
            target_ref_id=ref.id,
            checklist_id=cl["id"],
            item_name="margin-checked",
            item_rev=1,
            verdict="pass",
            origin="robot",
        )


# ── staleness on a consumed input ─────────────────────────────────────


def test_bumped_input_revision_marks_consumers_stale(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC5's mechanism: bumping the margin-policy revision marks exactly
    the verdicts that consumed it stale — a verdict that recorded no
    inputs stays current."""
    _board(store)
    _checklist(store)
    h = _handler(store)
    _assign(h, "cl4", "pcb:cl4-board")
    for item, inputs in (
        ("margin-checked", {_MARGIN_INPUT: 1}),
        ("other-item", None),
    ):
        h.edit(
            id="cl4",
            op="verdict",
            target="pcb:cl4-board",
            item=item,
            verdict="pass",
            origin="machine",
            inputs=inputs,
        )
    body = _status_body(h, "cl4", "pcb:cl4-board")
    assert "stale" not in body

    monkeypatch.setattr(pcb_pkg, "PASSIVE_MARGIN_REVISION", 2)
    body = _status_body(h, "cl4", "pcb:cl4-board")
    assert "stale (input changed: margin:fixed-derating)" in body
    # … and only the consumer went stale.
    margin_row = next(ln for ln in body.splitlines() if "margin-checked" in ln)
    other_row = next(ln for ln in body.splitlines() if "other-item" in ln)
    assert "stale (input changed" in margin_row
    assert "stale" not in other_row


def test_human_verdict_with_inputs_goes_stale_too(store: Store, monkeypatch) -> None:
    """Human verdicts are not exempt from input staleness — they are
    SHOWN stale (the AC5 'stay, shown stale, not replaced' half)."""
    _board(store)
    _checklist(store)
    h = _handler(store)
    _assign(h, "cl4", "pcb:cl4-board")
    h.edit(
        id="cl4",
        op="verdict",
        target="pcb:cl4-board",
        item="margin-checked",
        verdict="pass",
        inputs={_MARGIN_INPUT: 1},
    )
    monkeypatch.setattr(pcb_pkg, "PASSIVE_MARGIN_REVISION", 2)
    body = _status_body(h, "cl4", "pcb:cl4-board")
    assert "stale (input changed: margin:fixed-derating)" in body


def test_unresolvable_input_never_marks_stale(store: Store, monkeypatch) -> None:
    """An input name the kind's bridge cannot resolve cannot be
    re-derived — so it is never a staleness trigger."""
    _board(store)
    _checklist(store)
    h = _handler(store)
    _assign(h, "cl4", "pcb:cl4-board")
    h.edit(
        id="cl4",
        op="verdict",
        target="pcb:cl4-board",
        item="margin-checked",
        verdict="pass",
        inputs={"fact:me99999": 3},
    )
    monkeypatch.setattr(pcb_pkg, "PASSIVE_MARGIN_REVISION", 9)
    body = _status_body(h, "cl4", "pcb:cl4-board")
    assert "stale" not in body


def test_inputs_on_a_bridgless_kind_never_stale(store: Store) -> None:
    """A non-pcb target has no bridge to resolve inputs — a verdict that
    records them just stays current (cannot fault on what you cannot
    re-derive)."""
    store.insert_ref(kind="paper", slug="cl4-paper", title="t")
    _checklist(store)
    h = _handler(store)
    _assign(h, "cl4", "paper:cl4-paper")
    h.edit(
        id="cl4",
        op="verdict",
        target="paper:cl4-paper",
        item="margin-checked",
        verdict="pass",
        inputs={_MARGIN_INPUT: 0},
    )
    body = _status_body(h, "cl4", "paper:cl4-paper")
    assert "stale" not in body


# ── op='forget' ───────────────────────────────────────────────────────


def test_forget_drops_machine_keeps_human(store: Store) -> None:
    _board(store)
    _checklist(store)
    h = _handler(store)
    _assign(h, "cl4", "pcb:cl4-board")
    ref = store.get_ref(kind="pcb", id="cl4-board")
    cl = store.checklist_get("cl4")
    assert cl is not None and ref is not None
    for item, origin in (
        ("margin-checked", "machine"),
        ("other-item", "human"),
    ):
        store.checklist_verdict_insert(
            target_ref_id=ref.id,
            checklist_id=cl["id"],
            item_name=item,
            item_rev=1,
            verdict="pass",
            origin=origin,
            inputs={_MARGIN_INPUT: 1},
        )
    out = h.edit(id="cl4", op="forget", target="pcb:cl4-board")
    assert "forgot 1 item(s)" in out.body and "margin-checked" in out.body
    # The machine verdict is gone from the live ledger; the human one
    # stays — and now reads "not checked" vs "pass" accordingly.
    verdicts = store.checklist_verdicts_latest_for_target(
        target_ref_id=ref.id, checklist_id=cl["id"]
    )
    assert "margin-checked" not in verdicts
    assert verdicts["other-item"]["origin"] == "human"
    body = _status_body(h, "cl4", "pcb:cl4-board")
    margin_row = next(ln for ln in body.splitlines() if "margin-checked" in ln)
    other_row = next(ln for ln in body.splitlines() if "other-item" in ln)
    assert "not checked" in margin_row
    assert "pass" in other_row and "stale" not in other_row


def test_forget_scoped_by_item_and_consumed_input(store: Store) -> None:
    _board(store)
    _checklist(store)
    h = _handler(store)
    ref = store.get_ref(kind="pcb", id="cl4-board")
    cl = store.checklist_get("cl4")
    assert cl is not None and ref is not None
    store.checklist_verdict_insert(
        target_ref_id=ref.id,
        checklist_id=cl["id"],
        item_name="margin-checked",
        item_rev=1,
        verdict="pass",
        origin="machine",
        inputs={_MARGIN_INPUT: 1},
    )
    store.checklist_verdict_insert(
        target_ref_id=ref.id,
        checklist_id=cl["id"],
        item_name="other-item",
        item_rev=1,
        verdict="fail",
        origin="machine",
        inputs={"skill:precis-pcb-review-help": 4},
    )
    # consumed= picks only the verdict that read that input.
    out = h.edit(
        id="cl4",
        op="forget",
        target="pcb:cl4-board",
        consumed=_MARGIN_INPUT,
    )
    assert "margin-checked" in out.body
    verdicts = store.checklist_verdicts_latest_for_target(
        target_ref_id=ref.id, checklist_id=cl["id"]
    )
    assert "margin-checked" not in verdicts
    assert "other-item" in verdicts
    # item= picks one item regardless of its inputs.
    out = h.edit(id="cl4", op="forget", target="pcb:cl4-board", item="other-item")
    assert "other-item" in out.body
    assert (
        store.checklist_verdicts_latest_for_target(
            target_ref_id=ref.id, checklist_id=cl["id"]
        )
        == {}
    )
    # A second forget is a clean no-op.
    out = h.edit(id="cl4", op="forget", target="pcb:cl4-board")
    assert "nothing forgotten" in out.body


def test_forget_never_touches_human_verdicts(store: Store) -> None:
    """The ledger's other half: a human verdict on the same item+input
    survives a forget that drops its machine sibling."""
    _board(store)
    _checklist(store)
    h = _handler(store)
    ref = store.get_ref(kind="pcb", id="cl4-board")
    cl = store.checklist_get("cl4")
    assert cl is not None and ref is not None
    for origin in ("machine", "human"):
        store.checklist_verdict_insert(
            target_ref_id=ref.id,
            checklist_id=cl["id"],
            item_name="margin-checked",
            item_rev=1,
            verdict="pass",
            origin=origin,
            inputs={_MARGIN_INPUT: 1},
        )
    out = h.edit(id="cl4", op="forget", target="pcb:cl4-board")
    assert "forgot" in out.body
    v = store.checklist_verdict_latest(
        target_ref_id=ref.id,
        checklist_id=cl["id"],
        item_name="margin-checked",
    )
    assert v is not None and v["origin"] == "human"


# ── the MCP boundary ──────────────────────────────────────────────────


def test_inputs_and_consumed_cross_the_mcp_edit_verb(
    store: Store, runtime_with_store: PrecisRuntime
) -> None:
    """``inputs=`` and ``consumed=`` must survive ``tools_core.edit`` (the
    callable FastMCP wires up), not only a direct handler call: record a
    machine verdict with inputs, then forget it scoped by consumed=."""
    _board(store)
    _checklist(store)
    tools_core._runtime = runtime_with_store
    try:
        tools_core.edit(
            kind="checklist",
            id="cl4",
            op="verdict",
            target="pcb:cl4-board",
            item="margin-checked",
            verdict="pass",
            origin="machine",
            inputs={_MARGIN_INPUT: 1},
        )
        tools_core.edit(
            kind="checklist",
            id="cl4",
            op="verdict",
            target="pcb:cl4-board",
            item="other-item",
            verdict="pass",
            origin="machine",
            inputs={"skill:precis-pcb-review-help": 4},
        )
        cl = store.checklist_get("cl4")
        ref = store.get_ref(kind="pcb", id="cl4-board")
        assert cl is not None and ref is not None
        v = store.checklist_verdict_latest(
            target_ref_id=ref.id, checklist_id=cl["id"], item_name="margin-checked"
        )
        assert v is not None and v["inputs"] == {_MARGIN_INPUT: 1}

        tools_core.edit(
            kind="checklist",
            id="cl4",
            op="forget",
            target="pcb:cl4-board",
            consumed=_MARGIN_INPUT,
        )
        verdicts = store.checklist_verdicts_latest_for_target(
            target_ref_id=ref.id, checklist_id=cl["id"]
        )
        assert "margin-checked" not in verdicts
        assert "other-item" in verdicts
    finally:
        tools_core._runtime = None
