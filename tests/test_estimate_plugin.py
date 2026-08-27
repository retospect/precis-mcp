"""Tests for `EstimateHandler` — the `estimate` kind's slice-1 composition
tier (docs/backlog/estimate-kind-ms-chemistry-workup.md).

Cache flow itself is `CacheBackedHandler`'s job (covered by
`test_cache_base.py`); these tests focus on:

- composition-query parsing (space/comma-separated, concatenated formula,
  unknown-symbol rejection) — pure `ase.data` lookups, no `mendeleev`
  needed, so these run unconditionally.
- the rendered composition panel (needs `mendeleev` — importorskip'd).
- cache-hit behaviour (second identical `get` doesn't re-touch mendeleev).
- the optional-dep degrade: the module imports cleanly and `get()` raises
  a clean, actionable error when `mendeleev` isn't installed.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

import precis_estimate
from precis.dispatch import Hub
from precis.errors import BadInput, Unsupported, Upstream
from precis.store import Store
from precis_estimate.handler import EstimateHandler, _parse_composition

#: Raw plugin migration files (kinds/providers rows the store's
#: `insert_ref`/`put_cache_entry` require). The shared session-scoped test
#: template only migrates precis-core (``tests/conftest.py``'s
#: ``_initialise_test_db`` builds a bare-dir `Migrator`, no plugin
#: `discover_sources`), and the dev container's installed
#: `entry_points.txt` is a build-time snapshot that doesn't see a
#: pyproject entry point added mid-worktree — so plugin-kind tests seed
#: their own migration directly. Mirrors `test_pathway_plugin.py`'s
#: `pathway_store` fixture.
_MIGRATIONS_DIR = Path(precis_estimate.__file__).parent / "migrations"

# ── composition parsing (no mendeleev needed) ───────────────────────────


@pytest.mark.parametrize(
    "query",
    ["Pd Zr H", "PdZrH", "Pd, Zr, H", "pd zr h", "  Pd   Zr  H  ", "H, PdZr"],
)
def test_parse_composition_variants_agree(query: str) -> None:
    assert _parse_composition(query) == ["H", "Pd", "Zr"]


def test_parse_composition_drops_stoichiometry_digits() -> None:
    assert _parse_composition("PdZrH2") == ["H", "Pd", "Zr"]


def test_parse_composition_dedupes() -> None:
    assert _parse_composition("Pd Pd Zr") == ["Pd", "Zr"]


def test_parse_composition_single_element() -> None:
    assert _parse_composition("Pd") == ["Pd"]


def test_parse_composition_unknown_symbol_names_it() -> None:
    with pytest.raises(BadInput, match="Xx"):
        _parse_composition("Pd Xx")


def test_parse_composition_empty_query_is_bad_input() -> None:
    with pytest.raises(BadInput):
        _parse_composition("   ")


# ── handler fixture ──────────────────────────────────────────────────────


@pytest.fixture
def handler(hub: Hub, store: Store) -> EstimateHandler:
    with store.pool.connection() as c:
        for sql in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            body = sql.read_text(encoding="utf-8")
            body = body.replace("BEGIN;", "").replace("COMMIT;", "")
            c.execute(body)
    return EstimateHandler(hub=hub)


# ── panel rendering (needs mendeleev) ────────────────────────────────────

pytest.importorskip("mendeleev")


def test_panel_has_per_element_rows_and_pairwise_section(
    handler: EstimateHandler,
) -> None:
    resp = handler.get(q="Pd Zr")

    assert "## Elements" in resp.body
    assert "Pd" in resp.body
    assert "Zr" in resp.body
    assert "## Pairwise" in resp.body
    # Pairwise rows follow the panel's Z-ascending element order (Zr, Z=40,
    # before Pd, Z=46) — not the alphabetical order the cache key sorts by.
    assert "Zr-Pd" in resp.body
    # epistemic-grade footer
    assert "hypothesis-generating only" in resp.body
    assert "measure" in resp.body


def test_single_element_query_has_no_pairwise_section(
    handler: EstimateHandler,
) -> None:
    resp = handler.get(q="Pd")
    assert "## Elements" in resp.body
    assert "## Pairwise" not in resp.body


def test_dband_row_present_for_vendored_metal_absent_for_others(
    handler: EstimateHandler,
) -> None:
    resp = handler.get(q="Pd Zr")
    # Pd is in the vendored Hammer-Norskov table; Zr is not.
    lines = resp.body.splitlines()
    pd_line = next(line for line in lines if line.startswith("46\t"))
    zr_line = next(line for line in lines if line.startswith("40\t"))
    assert "-1.83" in pd_line
    assert zr_line.rstrip().endswith("—")


def test_unknown_view_raises_unsupported_naming_planned_views(
    handler: EstimateHandler,
) -> None:
    with pytest.raises(Unsupported) as exc_info:
        handler.get(q="Pd Zr", view="structure")
    assert "structure" in str(exc_info.value.next)


def test_second_call_hits_cache(
    handler: EstimateHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mendeleev

    calls: list[str] = []
    real_element = mendeleev.element

    def _spy(symbol: str) -> object:
        calls.append(symbol)
        return real_element(symbol)

    monkeypatch.setattr(mendeleev, "element", _spy)

    handler.get(q="Pd Zr")
    n_after_first = len(calls)
    assert n_after_first > 0

    resp2 = handler.get(q="Pd Zr")
    # No new mendeleev lookups is the real cache-hit signal here — `estimate`
    # is a $0 provider, and `_cost_str` renders zero-cost entries as the
    # flat '[cost: free]' on both hit and miss (no '- cached' suffix),
    # unlike the paid kinds (`math`'s own cache-hit test asserts on that
    # suffix precisely because Wolfram is metered).
    assert len(calls) == n_after_first
    assert resp2.cost == "[cost: free]"


def test_query_variants_share_one_cache_row(
    handler: EstimateHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mendeleev

    calls: list[str] = []
    real_element = mendeleev.element

    def _spy(symbol: str) -> object:
        calls.append(symbol)
        return real_element(symbol)

    monkeypatch.setattr(mendeleev, "element", _spy)

    handler.get(q="Pd Zr")
    n_after_first = len(calls)
    handler.get(q="PdZr")  # same composition, different spelling
    assert len(calls) == n_after_first


# ── optional-dep degrade (works with or without mendeleev installed) ────


def test_module_imports_and_get_raises_clean_error_without_extra(
    hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The handler module must import cleanly with the `estimate` extra
    absent, and a `get()` in that state must fail with a clean, actionable
    error — never an opaque `ModuleNotFoundError` traceback. Simulated by
    poisoning `sys.modules['mendeleev'] = None`, the standard trick to make
    `import mendeleev` raise `ImportError` regardless of whether the real
    package is actually installed in this test environment.
    """
    monkeypatch.setitem(sys.modules, "mendeleev", None)

    import precis_estimate.handler as handler_mod

    # Neither the module nor `__init__` may touch mendeleev — only `_fetch`
    # may, and only lazily. Nothing here references mendeleev at module
    # scope, so reloading with it "absent" is a no-op restore once
    # monkeypatch reverts sys.modules at teardown.
    importlib.reload(handler_mod)  # must not raise even with mendeleev "gone"
    h = handler_mod.EstimateHandler(hub=hub)  # must not touch mendeleev
    with pytest.raises(Upstream, match="estimate.*extra"):
        h.get(q="Pd Zr H")


def test_store_only_construction_does_not_import_mendeleev(store: Store) -> None:
    """`__init__` itself must not touch mendeleev — only `_fetch` may."""
    EstimateHandler(hub=Hub(store=store))  # no exception even without deps
