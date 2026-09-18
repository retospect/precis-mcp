"""Joining-precedent DRC — blocktree slice 5
(docs/backlog/blocktree-library-build-plan.md §Slice 5,
:mod:`precis_se.precedent`).

Covers the four findings over a two-block connect (unnamed / class
unknown / unprecedented / precedent with counts), the same finding when
one endpoint is an INSTANCE of a template that declares the reaction
transition (the adversarial template-resolution case the 2026-09-18
readiness pass added), the plain-mechanical-connect silence, and the
``view='drc'`` header counts ignoring ``info`` rows.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import precis_se
from precis.dispatch import Hub
from precis.handlers.rxn import RxnHandler
from precis.store import Store
from precis_se import persist
from precis_se import precedent as se_precedent
from precis_se.handler import SeHandler

_MIGRATIONS_DIR = Path(precis_se.__file__).parent / "migrations"


def _apply_se_migrations(store: Store) -> None:
    with store.pool.connection() as c:
        for sql in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            body = sql.read_text(encoding="utf-8")
            body = body.replace("BEGIN;", "").replace("COMMIT;", "")
            c.execute(body)


@pytest.fixture
def handler(hub: Hub, store: Store) -> SeHandler:
    _apply_se_migrations(store)
    return SeHandler(hub=hub)


def _rxn(store: Store) -> RxnHandler:
    return RxnHandler(hub=Hub(store=store))


def _load(store: Store, slug: str) -> tuple[Any, int]:
    ref = store.get_ref(kind="se", id=slug)
    assert ref is not None
    return persist.load_tree(store, ref.id), ref.id


def _rule(findings: list[Any], rule: str) -> list[Any]:
    return [f for f in findings if f.rule == rule]


#: Two plain blocks, ports affording nothing — a mechanical connect with no
#: joining chemistry to talk about at all.
_PLAIN_OPS = [
    {"op": "add_block", "name": "a", "envelope": "box:w0.01d0.01h0.01"},
    {"op": "add_port", "block": "a", "name": "p"},
    {
        "op": "add_block",
        "name": "b",
        "envelope": "box:w0.01d0.01h0.01",
        "pose": [0.0, 0.0, 0.0],
    },
    {"op": "add_port", "block": "b", "name": "p"},
    {"op": "connect", "a": "a.p", "b": "b.p"},
]

#: Two blocks whose ports afford CuAAC's complementary halves
#: (azide/alkyne, :data:`precis_se.atomic.vocab.JOINING_HALVES`) — a
#: declared joining, no reaction transition naming it yet.
_JOINING_OPS = [
    {"op": "add_block", "name": "a", "envelope": "box:w0.01d0.01h0.01"},
    {"op": "add_port", "block": "a", "name": "p", "roles": ["azide"]},
    {
        "op": "add_block",
        "name": "b",
        "envelope": "box:w0.01d0.01h0.01",
        "pose": [0.0, 0.0, 0.0],
    },
    {"op": "add_port", "block": "b", "name": "p", "roles": ["alkyne"]},
    {"op": "connect", "a": "a.p", "b": "b.p"},
]


def _reaction_transition_ops(*, block: str, driver_ref: str) -> list[dict[str, Any]]:
    return [
        {
            "op": "declare_states",
            "block": block,
            "states": [{"name": "free"}, {"name": "bonded"}],
        },
        {
            "op": "declare_transitions",
            "block": block,
            "transitions": [
                {
                    "from_state": "free",
                    "to_state": "bonded",
                    "driver_kind": "reaction",
                    "driver_ref": driver_ref,
                }
            ],
        },
    ]


# ── plain mechanical connect: silence ───────────────────────────────────


def test_plain_mechanical_connect_is_silent(handler: SeHandler, store: Store) -> None:
    handler.put(id="plain1", text=json.dumps({"ops": _PLAIN_OPS}))
    tree, ref_id = _load(store, "plain1")
    assert se_precedent.findings(store, tree, ref_id) == []


# ── joining declared by roles only, no rxn named ────────────────────────


def test_joining_unnamed_when_no_reaction_transition_names_an_rxn(
    handler: SeHandler, store: Store
) -> None:
    handler.put(id="unnamed1", text=json.dumps({"ops": _JOINING_OPS}))
    tree, ref_id = _load(store, "unnamed1")
    findings = se_precedent.findings(store, tree, ref_id)
    hits = _rule(findings, "joining_unnamed")
    assert len(hits) == 1
    assert hits[0].severity == "info"
    assert "CuAAC" in hits[0].detail
    assert hits[0].subject == "a.p—b.p"


# ── rxn named but has no reaction_class ─────────────────────────────────


def test_joining_class_unknown_when_rxn_has_no_reaction_class(
    handler: SeHandler, store: Store
) -> None:
    _rxn(store).put(id="click-noclass", rxn_smiles="CC(=O)O.OCC>>CC(=O)OCC.O")
    handler.put(
        id="classunknown1",
        text=json.dumps(
            {
                "ops": [
                    *_JOINING_OPS,
                    *_reaction_transition_ops(block="a", driver_ref="click-noclass"),
                ]
            }
        ),
    )
    tree, ref_id = _load(store, "classunknown1")
    findings = se_precedent.findings(store, tree, ref_id)
    hits = _rule(findings, "joining_class_unknown")
    assert len(hits) == 1
    assert hits[0].severity == "warn"
    assert "click-noclass" in hits[0].detail
    assert not _rule(findings, "joining_unnamed")


# ── reaction_class known, zero yield precedent ──────────────────────────


def test_joining_unprecedented_when_zero_yield_rows(
    handler: SeHandler, store: Store
) -> None:
    _rxn(store).put(
        id="click-noprec",
        rxn_smiles="CC(=O)O.OCC>>CC(=O)OCC.O",
        reaction_class="RXNO:0000024",
    )
    handler.put(
        id="unprec1",
        text=json.dumps(
            {
                "ops": [
                    *_JOINING_OPS,
                    *_reaction_transition_ops(block="a", driver_ref="click-noprec"),
                ]
            }
        ),
    )
    tree, ref_id = _load(store, "unprec1")
    findings = se_precedent.findings(store, tree, ref_id)
    hits = _rule(findings, "joining_unprecedented")
    assert len(hits) == 1
    assert hits[0].severity == "warn"
    assert "RXNO:0000024" in hits[0].detail
    assert "0 yield rows" in hits[0].detail


# ── reaction_class known, precedent exists ──────────────────────────────


def test_joining_precedent_reports_counts(handler: SeHandler, store: Store) -> None:
    _rxn(store).put(
        id="click-prec",
        rxn_smiles="CC(=O)O.OCC>>CC(=O)OCC.O",
        reaction_class="RXNO:0000024",
    )
    _rxn(store).put(id="click-prec", property="yield", value=83, unit="%")
    handler.put(
        id="prec1",
        text=json.dumps(
            {
                "ops": [
                    *_JOINING_OPS,
                    *_reaction_transition_ops(block="a", driver_ref="click-prec"),
                ]
            }
        ),
    )
    tree, ref_id = _load(store, "prec1")
    findings = se_precedent.findings(store, tree, ref_id)
    hits = _rule(findings, "joining_precedent")
    assert len(hits) == 1
    assert hits[0].severity == "info"
    assert "1 yield row(s) across 1 rxn(s)" in hits[0].detail
    assert "RXNO:0000024" in hits[0].detail
    assert "click-prec" in hits[0].detail


# ── adversarial: an INSTANCED endpoint's transitions live on its template ──


def test_precedent_resolves_through_instance_to_template_transitions(
    handler: SeHandler, store: Store
) -> None:
    """``declare_transitions`` is template-owned (``_template_owned``); an
    instance's own uid carries no ``design_transitions`` rows. The 2026-
    09-18 readiness pass's blocker: the precedent walk must resolve
    ``node.template`` first (:func:`precis.blocktree.ops.resolve_template`)
    before reading transitions, or a connect touching an INSTANCE reads as
    if nothing were ever declared."""
    _rxn(store).put(
        id="click-inst",
        rxn_smiles="CC(=O)O.OCC>>CC(=O)OCC.O",
        reaction_class="RXNO:0000024",
    )
    _rxn(store).put(id="click-inst", property="yield", value=91, unit="%")
    handler.put(
        id="instprec1",
        text=json.dumps(
            {
                "ops": [
                    {
                        "op": "add_block",
                        "name": "tmpl",
                        "envelope": "box:w0.01d0.01h0.01",
                    },
                    {
                        "op": "add_port",
                        "block": "tmpl",
                        "name": "p",
                        "roles": ["azide"],
                    },
                    *_reaction_transition_ops(block="tmpl", driver_ref="click-inst"),
                    {"op": "instance_block", "template": "tmpl", "name": "inst"},
                    {
                        "op": "add_block",
                        "name": "other",
                        "envelope": "box:w0.01d0.01h0.01",
                        "pose": [0.02, 0.0, 0.0],
                    },
                    {
                        "op": "add_port",
                        "block": "other",
                        "name": "p",
                        "roles": ["alkyne"],
                    },
                    {"op": "connect", "a": "inst.p", "b": "other.p"},
                ]
            }
        ),
    )
    tree, ref_id = _load(store, "instprec1")
    assert tree.blocks["inst"].uid != tree.blocks["tmpl"].uid
    findings = se_precedent.findings(store, tree, ref_id)
    hits = _rule(findings, "joining_precedent")
    assert len(hits) == 1
    assert "click-inst" in hits[0].detail
    assert "1 yield row(s) across 1 rxn(s)" in hits[0].detail


# ── DRC header counts ignore info rows ──────────────────────────────────


def test_drc_header_counts_ignore_info_findings(
    handler: SeHandler, store: Store
) -> None:
    """``joining_unnamed``/``joining_precedent`` are ``info`` — the DRC
    header's error/warn counts must not move because of them."""
    handler.put(id="header1", text=json.dumps({"ops": _JOINING_OPS}))
    body = handler.get(id="header1", view="drc").body
    assert "joining_unnamed" in body
    assert "0 error(s), 0 warning(s)" in body
