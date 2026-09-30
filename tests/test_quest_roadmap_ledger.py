"""Tests for the capability ledger — :mod:`precis.quest.roadmap_ledger`.

The roadmap body's derived read (``bootstrap-roadmap-quest.md`` §Design
"Derived (never stored) reads"): best supply per (capability, axis) is the
better of ``meta.supply`` and every **done** rung's ``produces``, by the
axis' ``sense``; open rungs are promises, not supply. Runs against real PG
(the ``store`` fixture) so the ``serves`` walk + STATUS-tag SQL is
exercised end to end, through the same handlers the roadmap tick will use.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.handlers.quest import QuestHandler
from precis.handlers.todo import TodoHandler
from precis.quest import roadmap_ledger as ledger
from precis.quest.roadmap_ledger import (
    LedgerRow,
    best_supply,
    compute_ledger,
    ledger_signature,
    render_ledger_markdown,
)
from tests.conftest import id_of

KEY = "positional_accuracy_nm"


def _quests(store: Any) -> QuestHandler:
    return QuestHandler(hub=Hub(store=store))


def _todos(store: Any) -> TodoHandler:
    return TodoHandler(hub=Hub(store=store))


def _qid(resp: Any) -> int:
    m = re.search(r"\bqu(\d+)\b", resp.body)
    assert m is not None, f"no quest handle in ack: {resp.body!r}"
    return int(m.group(1))


def make_root(
    store: Any,
    *,
    sense: str = "min",
    unit: str | None = "nm",
    demand: float | None = 2.0,
    supply: float | None = 6.0,
    supply_evidence: list[str] | None = None,
    key: str = KEY,
    cap_title: str = "Capability: positional accuracy",
) -> tuple[int, int]:
    """A roadmap root served by one capability with one axis. Returns
    ``(root_id, capability_id)``. Goes through ``QuestHandler.edit`` so the
    stored shapes are exactly what the write guards admit."""
    h = _quests(store)
    root = _qid(h.put(text="Bootstrap: a chain of assemblers"))
    cap = _qid(h.put(text=cap_title))
    axis: dict[str, Any] = {"key": key, "sense": sense}
    if unit is not None:
        axis["unit"] = unit
    meta: dict[str, Any] = {"quest_body": "roadmap", "rubric_objectives": [axis]}
    if demand is not None:
        meta["demand"] = {
            key: {"value": demand, "source": "se:hexfold-valve", "reason": "port pitch"}
        }
    if supply is not None:
        meta["supply"] = {
            key: {"value": supply, "evidence": supply_evidence or ["fi42"]}
        }
    h.edit(id=cap, meta=meta)
    h.edit(id=root, meta={"quest_body": "roadmap"})
    store.add_link(src_ref_id=cap, dst_ref_id=root, relation="serves")
    return root, cap


def make_rung(
    store: Any,
    cap: int,
    *,
    value: float,
    evidence: list[str] | None = None,
    status: str | None = None,
    key: str = KEY,
    title: str = "rung: place one tile",
    produces_for: int | None = None,
) -> int:
    """A ``meta.rung`` todo serving ``cap`` and producing ``value`` on
    ``key``. ``status=None`` leaves it untagged (resolves to ``open``)."""
    th = _todos(store)
    rung = {
        "pathway": "qu161906",
        "consumes": [],
        "produces": [
            {
                "capability": f"qu{produces_for if produces_for is not None else cap}",
                "key": key,
                "value": value,
                "evidence": evidence or ["fi7"],
            }
        ],
    }
    rid = id_of(th.put(text=title, meta={"rung": rung}).body)
    store.add_link(src_ref_id=rid, dst_ref_id=cap, relation="serves")
    if status is not None:
        th.tag(id=rid, add=[f"STATUS:{status}"])
    return rid


def _rows(store: Any, root: int) -> list[LedgerRow]:
    return compute_ledger(store, root)


# ── best_supply ───────────────────────────────────────────────────────


class TestBestSupply:
    def test_stored_supply_alone(self, store: Any) -> None:
        _root, cap = make_root(store, supply=6.0, supply_evidence=["fi42"])
        assert best_supply(store, cap, KEY, "min") == (6.0, ("fi42",))

    def test_nothing_cited_is_none(self, store: Any) -> None:
        _root, cap = make_root(store, supply=None)
        assert best_supply(store, cap, KEY, "min") == (None, ())

    def test_sense_lower_picks_the_smaller(self, store: Any) -> None:
        _root, cap = make_root(store, sense="lower", supply=6.0)
        make_rung(store, cap, value=2.1, evidence=["fi99"], status="done")
        make_rung(store, cap, value=9.0, evidence=["fi98"], status="done")
        assert best_supply(store, cap, KEY, "lower") == (2.1, ("fi99",))

    def test_sense_higher_picks_the_larger(self, store: Any) -> None:
        _root, cap = make_root(store, sense="higher", supply=6.0)
        make_rung(store, cap, value=2.1, evidence=["fi99"], status="done")
        make_rung(store, cap, value=9.0, evidence=["fi98"], status="done")
        assert best_supply(store, cap, KEY, "higher") == (9.0, ("fi98",))

    def test_done_rung_beats_worse_stored_supply(self, store: Any) -> None:
        _root, cap = make_root(store, sense="min", supply=6.0, supply_evidence=["fi42"])
        make_rung(store, cap, value=2.1, evidence=["fi99"], status="done")
        value, evidence = best_supply(store, cap, KEY, "min")
        assert value == 2.1
        assert evidence == ("fi99",)  # the winner's evidence, not the stored one

    def test_open_rung_does_not_count(self, store: Any) -> None:
        _root, cap = make_root(store, sense="min", supply=6.0)
        make_rung(store, cap, value=0.1, evidence=["fi99"])  # untagged → open
        make_rung(store, cap, value=0.2, evidence=["fi98"], status="doing")
        make_rung(store, cap, value=0.3, evidence=["fi97"], status="blocked")
        assert best_supply(store, cap, KEY, "min") == (6.0, ("fi42",))

    def test_stored_supply_kept_on_tie(self, store: Any) -> None:
        _root, cap = make_root(store, sense="min", supply=6.0, supply_evidence=["fi42"])
        make_rung(store, cap, value=6.0, evidence=["fi99"], status="done")
        assert best_supply(store, cap, KEY, "min") == (6.0, ("fi42",))

    def test_rung_naming_another_capability_is_ignored(self, store: Any) -> None:
        _root, cap = make_root(store, sense="min", supply=6.0)
        make_rung(store, cap, value=0.1, status="done", produces_for=cap + 100_000)
        assert best_supply(store, cap, KEY, "min") == (6.0, ("fi42",))

    def test_unknown_sense_is_refused(self, store: Any) -> None:
        _root, cap = make_root(store)
        with pytest.raises(ValueError, match="sense"):
            best_supply(store, cap, KEY, "sideways")


# ── compute_ledger ────────────────────────────────────────────────────


class TestComputeLedger:
    def test_the_display_stub_is_cut_but_the_statement_is_whole(
        self, store: Any
    ) -> None:
        """The first prod dry-run (2026-09-30) showed the demand role a
        capability cut at 60 characters — "…where we choose," — and told it
        to derive the number from that statement alone. The stub stays for
        the ledger table; the prompt reads ``capability_statement``."""
        long_title = (
            "Capability: positional accuracy: place a building block where we "
            "choose, in liquid at room temperature, to within the distance the "
            "next step tolerates"
        )
        root, _cap = make_root(store, cap_title=long_title)
        (r,) = _rows(store, root)
        assert len(r.capability_title) == 60
        assert r.capability_title == long_title[:60]
        assert r.capability_statement == long_title

    def test_one_row_per_axis_with_evidence(self, store: Any) -> None:
        root, cap = make_root(store, supply=6.0, supply_evidence=["fi42", "pa7"])
        rows = _rows(store, root)
        assert len(rows) == 1
        r = rows[0]
        assert r.capability == f"qu{cap}"
        assert r.capability_title.startswith("Capability: positional accuracy")
        assert r.capability_statement == "Capability: positional accuracy"
        assert (r.key, r.sense, r.unit) == (KEY, "min", "nm")
        assert (r.demanded, r.best_supply) == (2.0, 6.0)
        assert r.best_supply_evidence == ("fi42", "pa7")
        assert r.closing_rung is None and r.closing_rung_status is None
        assert r.state == "unmet"

    def test_met_via_stored_supply(self, store: Any) -> None:
        root, _cap = make_root(store, demand=2.0, supply=1.5)
        (r,) = _rows(store, root)
        assert r.state == "met" and r.closing_rung is None

    def test_met_via_done_rung_names_it_as_closing(self, store: Any) -> None:
        root, cap = make_root(store, demand=2.0, supply=6.0)
        rid = make_rung(store, cap, value=1.9, evidence=["fi99"], status="done")
        (r,) = _rows(store, root)
        assert r.state == "met"
        assert r.best_supply == 1.9 and r.best_supply_evidence == ("fi99",)
        assert (r.closing_rung, r.closing_rung_status) == (f"td{rid}", "done")

    def test_partial_when_an_open_rung_would_meet(self, store: Any) -> None:
        root, cap = make_root(store, demand=2.0, supply=6.0)
        rid = make_rung(store, cap, value=1.0, status="doing")
        (r,) = _rows(store, root)
        assert r.state == "partial"
        assert r.best_supply == 6.0  # the promise is not supply
        assert (r.closing_rung, r.closing_rung_status) == (f"td{rid}", "doing")

    def test_open_rung_short_of_demand_stays_unmet(self, store: Any) -> None:
        root, cap = make_root(store, demand=2.0, supply=6.0)
        rid = make_rung(store, cap, value=3.0)
        (r,) = _rows(store, root)
        assert r.state == "unmet"
        assert r.closing_rung == f"td{rid}"  # still the nearest in-flight rung

    def test_closing_rung_is_the_best_in_flight_producer(self, store: Any) -> None:
        root, cap = make_root(store, sense="min", demand=2.0, supply=6.0)
        make_rung(store, cap, value=1.8, title="rung A")
        best = make_rung(store, cap, value=0.5, title="rung B")
        make_rung(store, cap, value=1.9, title="rung C")
        (r,) = _rows(store, root)
        assert r.closing_rung == f"td{best}"

    def test_closing_rung_tie_goes_to_the_older(self, store: Any) -> None:
        root, cap = make_root(store, sense="min", demand=2.0, supply=6.0)
        older = make_rung(store, cap, value=1.0, title="rung A")
        make_rung(store, cap, value=1.0, title="rung B")
        (r,) = _rows(store, root)
        assert r.closing_rung == f"td{older}"

    def test_wont_do_rung_neither_closes_nor_promises(self, store: Any) -> None:
        root, cap = make_root(store, demand=2.0, supply=6.0)
        make_rung(store, cap, value=0.1, status="won't-do")
        (r,) = _rows(store, root)
        assert r.state == "unmet" and r.closing_rung is None
        assert r.best_supply == 6.0

    def test_dead_end_entry_naming_the_key(self, store: Any) -> None:
        root, cap = make_root(store, demand=2.0, supply=6.0)
        _quests(store).put(
            id=cap,
            text=f"nothing plausible closes {KEY}: 6.0 nm is the literature floor",
            entry="dead-end",
        )
        (r,) = _rows(store, root)
        assert r.state == "dead-end"

    def test_dead_end_entry_for_another_key_does_not_apply(self, store: Any) -> None:
        root, cap = make_root(store, demand=2.0, supply=6.0)
        _quests(store).put(id=cap, text="cycle_time_s is hopeless", entry="dead-end")
        (r,) = _rows(store, root)
        assert r.state == "unmet"

    def test_no_demand_is_unmet_with_no_target(self, store: Any) -> None:
        root, _cap = make_root(store, demand=None, supply=6.0)
        (r,) = _rows(store, root)
        assert r.demanded is None and r.state == "unmet"

    def test_axis_without_sense_is_skipped(self, store: Any) -> None:
        root, cap = make_root(store)
        _quests(store).edit(
            id=cap,
            meta={
                "rubric_objectives": [{"key": KEY, "sense": "min"}, {"key": "force_pN"}]
            },
        )
        rows = _rows(store, root)
        assert [r.key for r in rows] == [KEY]

    def test_pathway_quest_without_axes_yields_no_rows(self, store: Any) -> None:
        h = _quests(store)
        root = _qid(h.put(text="Bootstrap root"))
        pathway = _qid(h.put(text="Pathway: DNA scaffold"))
        h.edit(id=pathway, meta={"quest_body": "roadmap"})
        store.add_link(src_ref_id=pathway, dst_ref_id=root, relation="serves")
        assert _rows(store, root) == []

    def test_malformed_stored_supply_does_not_crash(self, store: Any) -> None:
        root, cap = make_root(store, supply=None)
        # bypass the write guard on purpose — a render must survive it
        store.stamp_ref_meta(cap, {"supply": {KEY: "six"}})
        (r,) = _rows(store, root)
        assert r.best_supply is None and r.state == "unmet"

    def test_capability_fanout_is_capped(self, store: Any, monkeypatch: Any) -> None:
        monkeypatch.setattr(ledger, "LEDGER_MAX_CAPABILITIES", 2)
        root, _cap = make_root(store)
        h = _quests(store)
        for i in range(3):
            c = _qid(h.put(text=f"Capability {i}"))
            h.edit(
                id=c,
                meta={
                    "quest_body": "roadmap",
                    "rubric_objectives": [{"key": f"k{i}", "sense": "max"}],
                },
            )
            store.add_link(src_ref_id=c, dst_ref_id=root, relation="serves")
        assert len(_rows(store, root)) == 2


# ── signature + render ────────────────────────────────────────────────


class TestSignatureAndRender:
    def test_signature_is_flat_and_skips_unsupplied(self, store: Any) -> None:
        root, cap = make_root(store, supply=6.0)
        h = _quests(store)
        h.edit(
            id=cap,
            meta={
                "rubric_objectives": [
                    {"key": KEY, "sense": "min"},
                    {"key": "cycle_time_s", "sense": "min"},
                ]
            },
        )
        sig = ledger_signature(_rows(store, root))
        assert sig == {f"qu{cap}:{KEY}": 6.0}
        assert all(isinstance(v, float) for v in sig.values())

    def test_signature_moves_when_a_done_rung_improves_supply(self, store: Any) -> None:
        root, cap = make_root(store, sense="min", supply=6.0)
        before = ledger_signature(_rows(store, root))
        make_rung(store, cap, value=2.1, status="done")
        after = ledger_signature(_rows(store, root))
        assert before == {f"qu{cap}:{KEY}": 6.0}
        assert after == {f"qu{cap}:{KEY}": 2.1}

    def test_render_carries_every_column(self, store: Any) -> None:
        root, cap = make_root(store, demand=2.0, supply=6.0, supply_evidence=["fi42"])
        rid = make_rung(store, cap, value=1.0, status="doing")
        md = render_ledger_markdown(_rows(store, root))
        header, sep, row = md.splitlines()
        assert header.startswith("| capability |") and sep.startswith("|---")
        for token in (
            f"qu{cap}",
            KEY,
            "2 nm",
            "6 nm",
            "fi42",
            f"td{rid} (doing)",
            "partial",
        ):
            assert token in row, f"{token!r} missing from {row!r}"

    def test_render_empty(self) -> None:
        assert "no capability axes" in render_ledger_markdown([])

    def test_render_marks_missing_cells(self, store: Any) -> None:
        root, _cap = make_root(store, demand=None, supply=None, unit=None)
        row = render_ledger_markdown(_rows(store, root)).splitlines()[-1]
        assert row.count("—") == 4  # demanded · supply · evidence · rung
