"""Retiring an se design does not retire the structures its blocks were
bound to, so ``delete`` has to say so (gr459058) — and a join whose
endpoint is unbound has to name the reason anyone actually hits it
(gr459057).

Both are message contracts, and both exist because the honest-looking
output was the problem: ``retired se design 'd' (4 block(s))`` reads as
"all of it is gone" while four structures stay live, and ``block 'tube_a'
is not bound to a structure design`` sends the reader to inspect a
``generate`` that succeeded. The orphan set td458221 tracks is the
accumulated residue of the first one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.structure import StructureHandler
from precis.store import Store
from precis_se.handler import SeHandler

_MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "src/precis_se/migrations"


def _seed_se_migrations(store: Store) -> None:
    with store.pool.connection() as c:
        for sql in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            body = sql.read_text(encoding="utf-8")
            body = body.replace("BEGIN;", "").replace("COMMIT;", "")
            c.execute(body)


@pytest.fixture
def handler(hub: Hub, store: Store) -> SeHandler:
    _seed_se_migrations(store)
    return SeHandler(hub=hub)


@pytest.fixture
def structure(store: Store) -> StructureHandler:
    return StructureHandler(hub=Hub(store=store))


def _make_structure(structure: StructureHandler, slug: str) -> None:
    structure.put(
        id=slug,
        text=json.dumps(
            {
                "cell": {"a": 20.0, "b": 20.0, "c": 20.0, "pbc": [False, False, False]},
                "ops": [
                    {"op": "add_atom", "element": "C", "cart": [0.0, 0.0, 0.0]},
                    {"op": "add_atom", "element": "C", "cart": [1.3, 0.0, 0.0]},
                ],
            }
        ),
    )


def _design_with_bound_block(
    handler: SeHandler, structure: StructureHandler, *, design: str, struct: str
) -> None:
    _make_structure(structure, struct)
    handler.put(
        id=design,
        text=json.dumps(
            {
                "ops": [
                    {"op": "add_block", "name": "blk", "envelope": "sphere:r2e-10"},
                    {
                        "op": "bind_structure",
                        "block": "blk",
                        "design": struct,
                    },
                ]
            }
        ),
    )


def test_retiring_a_design_names_the_structures_it_leaves_live(
    handler: SeHandler, structure: StructureHandler
) -> None:
    _design_with_bound_block(handler, structure, design="d-orphan", struct="s-orphan")
    body = handler.delete(id="d-orphan").body
    assert "retired se design 'd-orphan'" in body
    # The count alone was the defect: it has to say what survived, and
    # give the call that removes it.
    assert "1 structure(s) stay live" in body
    assert "delete(kind='structure', id='s-orphan')" in body


def test_the_structure_really_does_survive_the_retire(
    handler: SeHandler, structure: StructureHandler, store: Store
) -> None:
    """The message is only honest if the leak is real — pin the behaviour
    it describes, so a later cascade fix has to update both together."""
    _design_with_bound_block(handler, structure, design="d-survive", struct="s-survive")
    handler.delete(id="d-survive")
    assert store.get_ref(kind="structure", id="s-survive") is not None


def test_a_design_with_no_bound_blocks_says_nothing_extra(
    handler: SeHandler,
) -> None:
    """No orphans, no paragraph — the note must not become noise on every
    delete."""
    handler.put(
        id="d-bare",
        text=json.dumps(
            {"ops": [{"op": "add_block", "name": "blk", "envelope": "sphere:r2e-10"}]}
        ),
    )
    body = handler.delete(id="d-bare").body
    assert "stay live" not in body


def test_an_unbound_join_endpoint_names_the_same_ops_list_cause(
    handler: SeHandler,
) -> None:
    """A join against a block that carries no structure: the message has
    to name the deferred mint, because generate-then-join in one list is
    the first thing anyone writes and it can never work."""
    # Ports with a joinable `lattice` annotation but no bound structure:
    # that is what a same-ops-list generate leaves behind, and it is the
    # only way to reach the bound check — endpoint and lattice resolution
    # both run first.
    handler.put(
        id="d-join",
        text=json.dumps(
            {
                "ops": [
                    {"op": "add_block", "name": "a", "envelope": "sphere:r2e-10"},
                    {"op": "add_block", "name": "b", "envelope": "sphere:r2e-10"},
                    {
                        "op": "add_port",
                        "block": "a",
                        "name": "out",
                        "roles": ["covalent"],
                        "annotations": {"lattice": "sp2-hex"},
                    },
                    {
                        "op": "add_port",
                        "block": "b",
                        "name": "in",
                        "roles": ["covalent"],
                        "annotations": {"lattice": "sp2-hex"},
                    },
                ]
            }
        ),
    )
    with pytest.raises(BadInput) as exc:
        handler.edit(
            id="d-join",
            text=json.dumps(
                {
                    "ops": [
                        {
                            "op": "join",
                            "a": "a.out",
                            "b": "b.in",
                            "name": "composite",
                            "params": {"k": 0},
                        }
                    ]
                }
            ),
        )
    msg = str(exc.value)
    assert "not bound to a structure design" in msg
    # The whole point: the deferral is named, and so is the remedy.
    assert "same ops list" in msg
    assert "separate calls" in msg
