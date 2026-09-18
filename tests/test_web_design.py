"""``GET /design`` — the design-workbench tree list (S1, docs/backlog/
the design-workbench build, slice 1): one tree per live ``se`` design down to
each atomic-mode leaf's bound ``structure``, plus a flat "Loose
structures" section. Real-store integration only (the query-budget
assertion needs the actual pool) — no FakeStore degradation layer, unlike
``test_blocktree_view.py``'s split, since this route has no 404 path of
its own to degrade.
"""

from __future__ import annotations

import contextlib
import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

import precis_se
from precis.handlers.structure import StructureHandler
from precis.store import Store
from precis_se.handler import SeHandler
from precis_web.app import create_app
from precis_web.config import WebConfig

_SE_MIGRATIONS = Path(precis_se.__file__).parent / "migrations"


def _apply_se_migrations(store: Store) -> None:
    with store.pool.connection() as c:
        for sql in sorted(_SE_MIGRATIONS.glob("*.sql")):
            body = sql.read_text(encoding="utf-8")
            c.execute(body.replace("BEGIN;", "").replace("COMMIT;", ""))


def _cell() -> dict[str, Any]:
    return {"a": 20.0, "b": 20.0, "c": 20.0, "pbc": [False, False, False]}


def _make_structure(structure: StructureHandler, slug: str) -> str:
    """A tiny two-atom C/N structure design; returns the C atom's label
    (the ``bind_structure`` port target)."""
    structure.put(
        id=slug,
        text=json.dumps(
            {
                "cell": _cell(),
                "ops": [
                    {"op": "add_atom", "element": "C", "cart": [0.0, 0.0, 0.0]},
                    {"op": "add_atom", "element": "N", "cart": [1.3, 0.0, 0.0]},
                ],
            }
        ),
    )
    return "aC1"


@pytest.fixture
def design_client(store: Store, runtime_with_store: Any, tmp_path: Path) -> TestClient:
    _apply_se_migrations(store)
    return TestClient(
        create_app(
            runtime=runtime_with_store, web_config=WebConfig(corpus_dir=tmp_path)
        )
    )


@contextlib.contextmanager
def _counting(store: Store) -> Iterator[list[int]]:
    """Monkeypatch ``store.pool.connection`` so every ``conn.execute``
    inside the ``with`` block increments a counter — the S1 acceptance
    criterion's "≤ 3 SELECTs per render" check, without needing a real
    query-log sidecar."""
    calls: list[int] = []
    original = store.pool.connection

    @contextlib.contextmanager
    def counting_connection() -> Iterator[Any]:
        with original() as conn:
            real_execute = conn.execute

            def counted(*args: Any, **kwargs: Any) -> Any:
                calls.append(1)
                return real_execute(*args, **kwargs)

            conn.execute = counted  # type: ignore[method-assign]
            try:
                yield conn
            finally:
                conn.execute = real_execute  # type: ignore[method-assign]

    store.pool.connection = counting_connection  # type: ignore[assignment]
    try:
        yield calls
    finally:
        store.pool.connection = original  # type: ignore[method-assign]


def _seed(runtime_with_store: Any, store: Store) -> dict[str, str]:
    """One se design with a bound + an array leaf, one loose structure,
    one retired structure (must never render), and a derived-from link
    for the lineage count. Returns the slugs a test wants to assert on."""
    structure = StructureHandler(hub=runtime_with_store.hub)
    se = SeHandler(hub=runtime_with_store.hub)

    c_label = _make_structure(structure, "boundfrag")
    _make_structure(structure, "loosefrag")
    _make_structure(structure, "retiredfrag")

    ops = [
        {"op": "add_block", "name": "hub", "envelope": "sphere:r2e-10"},
        {"op": "add_port", "block": "hub", "name": "p1", "expected_element": "C"},
        {
            "op": "bind_structure",
            "block": "hub",
            "design": "boundfrag",
            "ports": {"p1": c_label},
        },
        {"op": "add_block", "name": "bolt_tpl", "envelope": "sphere:r1e-10"},
        {
            "op": "array_block",
            "name": "bolts",
            "template": "bolt_tpl",
            "linear": {"count": 3, "pitch": 0.01, "axis": [1.0, 0.0, 0.0]},
        },
        {"op": "add_block", "name": "arm"},
        {"op": "set_load", "block": "arm", "duty": "load-bearing"},
    ]
    se.put(id="widget1", text=json.dumps({"ops": ops}))

    retired_ref = store.get_ref(kind="structure", id="retiredfrag")
    assert retired_ref is not None
    store.retire_ref(retired_ref.id)

    bound_ref = store.get_ref(kind="structure", id="boundfrag")
    loose_ref = store.get_ref(kind="structure", id="loosefrag")
    assert bound_ref is not None and loose_ref is not None
    store.add_link(
        src_ref_id=loose_ref.id, dst_ref_id=bound_ref.id, relation="derived-from"
    )

    return {"bound": "boundfrag", "loose": "loosefrag", "retired": "retiredfrag"}


def test_design_list_renders_every_live_structure_exactly_once(
    design_client: TestClient, runtime_with_store: Any, store: Store
) -> None:
    slugs = _seed(runtime_with_store, store)
    r = design_client.get("/design")
    assert r.status_code == 200
    body = r.text

    # "exactly once" means one link to it — the slug text itself also
    # appears inside that link's own href, so a raw substring count would
    # double-count every hit.
    assert body.count(f'href="/structure/{slugs["bound"]}"') == 1, body
    assert body.count(f'href="/structure/{slugs["loose"]}"') == 1, body
    assert slugs["retired"] not in body

    # se design itself, its array leaf collapsed to "bolts ×3", and the
    # load-bearing block all render.
    assert "widget1" in body
    assert "bolts ×3" in body
    assert "hub" in body


def test_design_list_lineage_counts_render_for_the_bound_structure(
    design_client: TestClient, runtime_with_store: Any, store: Store
) -> None:
    _seed(runtime_with_store, store)
    r = design_client.get("/design")
    assert r.status_code == 200
    # loosefrag is derived-from boundfrag: boundfrag has one child.
    assert "1 child" in r.text


def test_design_list_node_hrefs_all_resolve(
    design_client: TestClient, runtime_with_store: Any, store: Store
) -> None:
    _seed(runtime_with_store, store)
    r = design_client.get("/design")
    assert r.status_code == 200
    hrefs = set(re.findall(r'href="(/(?:se|structure)/[^"]+)"', r.text))
    assert hrefs, "expected at least one se/structure href on the page"
    for href in hrefs:
        resp = design_client.get(href)
        assert resp.status_code == 200, f"{href} -> {resp.status_code}"


def test_design_route_queries_issue_at_most_three_selects(
    runtime_with_store: Any, store: Store
) -> None:
    """The route's OWN query budget (S1's "≤ 3 SELECTs per render,
    regardless of design count") — counted against the exact helpers
    ``design_list`` calls, in the order it calls them. Deliberately NOT
    routed through the full HTTP stack: a page render also pays for the
    sitewide nav-badge counts (``nav.py::nav_badges`` — several of its
    own ``COUNT`` queries, injected into every page by the shared
    ``Jinja2Templates`` context processor), which are cross-cutting
    infrastructure this slice neither owns nor budgets for."""
    _apply_se_migrations(store)  # the ``se`` kind row — no app fixture here
    _seed(runtime_with_store, store)
    from precis_web.routes import design as design_route

    with _counting(store) as calls:
        se_rows = design_route._se_rows(store)
        structure_rows = design_route._structure_rows(store)
        structures = design_route._structures_by_slug(structure_rows)
        ref_ids = [s["ref_id"] for s in structures.values()]
        design_route._lineage_counts(store, ref_ids)
        design_route._build_designs(se_rows, structures)
    assert len(calls) <= 3, f"{len(calls)} SELECTs issued: {calls}"


def test_design_list_empty_renders_zero_designs_and_zero_structures(
    design_client: TestClient,
) -> None:
    r = design_client.get("/design")
    assert r.status_code == 200
    assert "No live se designs or structures yet" in r.text
