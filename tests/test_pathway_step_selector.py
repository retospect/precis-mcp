"""precis_pathway step selector: ``get(kind='pathway',
id='<slug>~<source>→<target>')`` resolves one edge of the computed graph to a
focused view (Simulation step deep-links, docs/backlog/quest-dossier-dialectic.md).

Store-seeded fake ``meta['graph']`` (no calculator, no NEB) — split out of
``tests/test_pathway_plugin.py`` on purpose: that file's 81 real-EMT
simulation tests (``pytest.mark.slow``) make it expensive to target on its
own for a quick selector check. Still needs ``autocatpath`` importable
(``PathwayHandler.__init__`` imports ``precis_pathway.runner``), so it skips
cleanly on a host without it, same as the big file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("autocatpath")

import precis_pathway
from precis.dispatch import Hub
from precis.errors import BadInput
from precis.store import Store
from precis.utils import handle_registry as hr
from precis_pathway.handler import PathwayHandler

_MIGRATIONS_DIR = Path(precis_pathway.__file__).parent / "migrations"

# networkx node_link_data shape (edges='links') a persisted pathway carries —
# see precis_pathway.analysis's module docstring and persist.pathway_meta.
_GRAPH: dict[str, Any] = {
    "directed": True,
    "nodes": [{"id": "NH2*"}, {"id": "NH3*"}, {"id": "N*"}, {"id": "NH*"}],
    "links": [
        {
            "source": "NH2*",
            "target": "NH3*",
            "barrier": 0.74,
            "barrier_std": 0.05,
            "delta_e": -0.32,
            "low_confidence": False,
            "kind": "reaction",
        },
        {
            "source": "N*",
            "target": "NH*",
            "barrier": 1.2,
            "barrier_std": 0.1,
            "delta_e": 0.4,
            "low_confidence": True,
            "kind": "reaction",
        },
    ],
}


@pytest.fixture
def pathway_store(store: Store, monkeypatch: pytest.MonkeyPatch) -> Store:
    """The shared test store with the precis_pathway migration seeded + the
    dark flag on (mirrors ``test_pathway_plugin.py``'s fixture of the same
    name — duplicated rather than imported to keep this file independent and
    cheap to collect on its own)."""
    monkeypatch.setenv("PRECIS_AUTOCATPATH_ENABLED", "1")
    with store.pool.connection() as c:
        for sql in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            body = sql.read_text(encoding="utf-8")
            body = body.replace("BEGIN;", "").replace("COMMIT;", "")
            c.execute(body)
    return store


def _handler(store: Store) -> PathwayHandler:
    hub = Hub(store=store)
    h = PathwayHandler(hub=hub)
    h._register_with(hub)
    return h


def _seed(store: Store, slug: str, meta: dict[str, Any]) -> int:
    with store.tx() as conn:
        ref = store.insert_ref(
            kind="pathway", slug=slug, title=f"{slug} pathway", meta=meta, conn=conn
        )
    return ref.id


def _register_pathway_handle_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate the ``precis.handle_codes`` entry point discovering
    ``precis_pathway.handles`` — the fast test loop's container runs
    ``uv run --no-sync`` (see ``scripts/test``), so the real installed
    dist-info won't reflect a fresh ``pyproject.toml`` entry-point until the
    next ``uv sync`` (the ship gate does this); mocking discovery here keeps
    this test deterministic regardless of that timing."""
    import types

    from precis_pathway import handles as pathway_handles

    ep = types.SimpleNamespace(name="precis_pathway", load=lambda: pathway_handles)
    monkeypatch.setattr(hr, "entry_points", lambda group: [ep])
    monkeypatch.setattr(hr, "_plugins_loaded", False)
    monkeypatch.setattr(hr, "_plugin_kind_codes", {})
    monkeypatch.setattr(hr, "_plugin_chunk_codes", {})


def test_step_selector_returns_focused_view(
    pathway_store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _register_pathway_handle_code(monkeypatch)
    h = _handler(pathway_store)
    ref_id = _seed(pathway_store, "step-sel-test", {"status": "ready", "graph": _GRAPH})

    r = h.get(id="step-sel-test~NH2*→NH3*")
    assert "NH2*→NH3*" in r.body
    assert "0.74" in r.body  # barrier
    assert "0.05" in r.body  # barrier_std
    assert f"pw{ref_id}" in r.body  # parent pathway handle


def test_step_selector_low_confidence_edge(pathway_store: Store) -> None:
    h = _handler(pathway_store)
    _seed(pathway_store, "step-sel-lowconf", {"status": "ready", "graph": _GRAPH})

    r = h.get(id="step-sel-lowconf~N*→NH*")
    assert "low" in r.body
    assert "1.20" in r.body


def test_step_selector_unknown_label_lists_available(pathway_store: Store) -> None:
    h = _handler(pathway_store)
    _seed(pathway_store, "step-sel-unknown", {"status": "ready", "graph": _GRAPH})

    with pytest.raises(BadInput) as exc:
        h.get(id="step-sel-unknown~bogus→label")
    err = exc.value
    assert "bogus" in err.cause
    assert err.next is not None
    assert "NH2*→NH3*" in str(err.next)
    assert "N*→NH*" in str(err.next)


def test_step_selector_no_graph_errors_cleanly(pathway_store: Store) -> None:
    h = _handler(pathway_store)
    _seed(pathway_store, "step-sel-nograph", {"status": "preview"})

    with pytest.raises(BadInput) as exc:
        h.get(id="step-sel-nograph~a→b")
    assert "no computed graph" in exc.value.cause


def test_step_selector_no_such_pathway(pathway_store: Store) -> None:
    h = _handler(pathway_store)
    with pytest.raises(BadInput) as exc:
        h.get(id="does-not-exist~a→b")
    assert "does-not-exist" in exc.value.cause
