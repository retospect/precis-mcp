"""`{"op": "join", "dry_run": true}` — resolve, compose, report, mint nothing
(docs/backlog/se-join-observability.md slice 3).

The proof that it wrote nothing is the design itself: byte-identical tree
JSON, the same structure refs, revision count and catalogue rows after the
call. The proof that it reported honestly is that its report and catalogue
sections are line-for-line what `view='report'` / `view='catalogue'` show
for the same join once minted.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.store import Store
from precis_se import persist
from precis_se.handler import SeHandler

_SPEC = "hexfold 0.2\na: tube(8,0, len=3)\n"
_JOIN: dict[str, Any] = {
    "op": "join",
    "name": "joined",
    "a": "a.out",
    "b": "b.in",
    "seam_radius": {"b": 2},
}


@pytest.fixture
def design(store: Store) -> SeHandler:
    handler = SeHandler(hub=Hub(store=store))
    handler.put(
        id="dry",
        args={
            "ops": [
                {
                    "op": "generate",
                    "generator": "hexfold",
                    "params": {"spec": _SPEC},
                    "name": name,
                }
                for name in ("a", "b")
            ]
        },
    )
    return handler


def _fingerprint(store: Store) -> dict[str, Any]:
    """Everything a join could write: the design tree, every ref, the
    revision ledger and the shared catalogue."""
    ref = store.get_ref(kind="se", id="dry")
    assert ref is not None
    tree = json.dumps(
        persist.tree_to_json(persist.load_tree(store, ref.id)), sort_keys=True
    )
    with store.pool.connection() as conn:
        refs = conn.execute(
            "SELECT kind, count(*) FROM refs GROUP BY kind ORDER BY kind"
        ).fetchall()
        catalogue = conn.execute("SELECT count(*) FROM se_hexfold_catalogue").fetchone()
        revisions = conn.execute(
            "SELECT count(*) FROM design_revisions WHERE ref_id = %s", (ref.id,)
        ).fetchone()
    return {"tree": tree, "refs": refs, "catalogue": catalogue, "revisions": revisions}


def _section(body: str, header: str) -> str:
    """The text from `header` to the next line starting with `## ` or `# `."""
    lines = body.splitlines()
    start = lines.index(header)
    out = [lines[start]]
    for line in lines[start + 1 :]:
        if line.startswith(("## ", "# ")):
            break
        out.append(line)
    return "\n".join(out).rstrip()


def test_dry_run_leaves_the_design_byte_identical(
    design: SeHandler, store: Store
) -> None:
    before = _fingerprint(store)
    assert before["catalogue"] == (0,)  # nothing has seeded it yet
    body = design.edit(id="dry", ops=[{**_JOIN, "dry_run": True}]).body
    assert "join dry run" in body and "nothing written" in body
    assert _fingerprint(store) == before
    # not even the catalogue seed a real join performs
    assert store.get_ref(kind="structure", id="dry-joined") is None
    assert "joined" not in design.get(id="dry").body


def test_dry_run_matches_the_views_once_minted(design: SeHandler, store: Store) -> None:
    dry = design.edit(id="dry", ops=[{**_JOIN, "dry_run": True}]).body
    design.edit(id="dry", ops=[dict(_JOIN)])
    report = design.get(id="dry", view="report", args={"block": "joined"}).body
    catalogue = design.get(id="dry", view="catalogue", args={"block": "joined"}).body

    # report: the block section, header through findings table
    assert _section(dry, "## block 'joined'") == _section(report, "## block 'joined'")
    assert "seam.rings" in dry
    # catalogue: the per-join resolution section, label by label
    header = "## block 'joined' (join, rung=stick)"
    assert _section(dry, header) == _section(catalogue, header)
    assert "consulted: pinned z" in dry
    assert "in force: radius 2 (explicit)" in dry


def test_dry_run_reports_the_errors_a_real_join_would_raise(
    design: SeHandler, store: Store
) -> None:
    before = _fingerprint(store)
    for bad, needle in (
        ({**_JOIN, "a": "a.nope"}, "nope"),
        ({**_JOIN, "name": "a"}, "duplicate block name"),
        ({**_JOIN, "seam": "adapter"}, "seam.mismatch"),
    ):
        with pytest.raises((BadInput, NotFound), match=needle):
            design.edit(id="dry", ops=[{**bad, "dry_run": True}])
    assert _fingerprint(store) == before


def test_dry_run_must_be_a_bool_and_ride_alone(design: SeHandler, store: Store) -> None:
    before = _fingerprint(store)
    with pytest.raises(BadInput, match="'dry_run' must be true or false"):
        design.edit(id="dry", ops=[{**_JOIN, "dry_run": "yes"}])
    add = {"op": "add_block", "name": "x", "envelope": "cyl:r5e-9h5e-9"}
    with pytest.raises(BadInput, match="alone in its edit call"):
        design.edit(id="dry", ops=[add, {**_JOIN, "dry_run": True}])
    with pytest.raises(BadInput, match="alone in its edit call"):
        design.edit(id="dry", ops=[{**_JOIN, "dry_run": True}, dict(_JOIN)])
    assert _fingerprint(store) == before


def test_dry_run_refused_in_put(design: SeHandler, store: Store) -> None:
    before = _fingerprint(store)
    with pytest.raises(BadInput, match="dry_run writes nothing"):
        design.put(id="dry", args={"ops": [{**_JOIN, "dry_run": True}]})
    assert _fingerprint(store) == before


def test_dry_run_then_real_join_still_works(design: SeHandler, store: Store) -> None:
    design.edit(id="dry", ops=[{**_JOIN, "dry_run": True}])
    design.edit(id="dry", ops=[dict(_JOIN)])
    assert store.get_ref(kind="structure", id="dry-joined") is not None
