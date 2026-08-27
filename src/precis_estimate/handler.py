"""The ``estimate`` kind — a precis-mcp plugin handler.

Slice 1: composition-tier workup only. `get(kind='estimate', q='Pd Zr H')`
parses the query into a set of element symbols (space/comma-separated
list, or a concatenated formula like ``'PdZrH'`` or ``'PdZrH2'`` — digits
are read as stoichiometry and dropped, tier 1 cares about identity only),
validates each symbol against the real periodic table, and renders the
composition panel (`compute/composition.py`): one row per element (Z,
group/period, Pauling electronegativity, covalent radius, ground-state
magmom, d-electron count, Hammer–Nørskov d-band center where vendored) plus
a pairwise alloying-heuristic section when ≥2 elements are given.

Subclasses :class:`~precis.handlers._cache_base.CacheBackedHandler` (the
same base `math`/`youtube`/`web` share) so the cache-flow plumbing (hash →
lookup → freshness → fetch-on-miss → attribution footer → cost trailer) is
free. Results are deterministic for a fixed composition — ``ttl_seconds =
None`` pins the cache forever, same as `math`.

**Views.** Slice 1 ships the default composition panel only (``view=None``
or ``view='panel'``). Every other view named in the design doc (`structure`,
`whatif`, `compare`, `shape`, `orbitals`, `spin`, `kinetics`, `card`) is
slice 2 — asking for one now raises a clean :class:`Unsupported` naming
what exists and what's coming, mirroring `precis_pathway.handler`'s
unknown-view error shape rather than silently falling through.

**Optional deps, lazily loaded.** `mendeleev` backs every per-element
property lookup; it is *not* imported at module scope (nor in `__init__`)
so a venv without the `[estimate]` extra still loads this module and every
other handler in the registry cleanly (`dispatch._load_plugins` merely logs
a warning on a failed plugin `__init__`, but a failed *module import* is
worse — it would show up as a `precis.handlers` entry-point that can't even
be discovered). The import happens inside `_fetch`, on the first real
`get()`; a missing extra there raises a clean, actionable error instead of
an opaque `ModuleNotFoundError` traceback.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, ClassVar

from precis.errors import BadInput, Unsupported, Upstream
from precis.handlers._cache_base import CacheBackedHandler, FetchResult
from precis.protocol import KindSpec
from precis.response import Response
from precis.store.types import BlockInsert

if TYPE_CHECKING:
    from precis.dispatch import Hub
    from precis.store.types import CacheEntry, Ref

#: Slice-1 views. ``None`` / ``''`` / ``'panel'`` all mean "the default
#: composition panel" — there's only one view today.
_PANEL_VIEWS = ("panel",)

#: Named here (not built) so the unknown-view error can point at what's
#: coming without silently pretending it exists. See the module docstring
#: and docs/backlog/estimate-kind-ms-chemistry-workup.md.
_PLANNED_VIEWS = (
    "structure",
    "whatif",
    "compare",
    "shape",
    "orbitals",
    "spin",
    "kinetics",
    "card",
)

#: A composition token is one or more element symbols, either given
#: individually (space/comma separated: ``'Pd Zr H'``, ``'Pd, Zr'``) or
#: concatenated as a formula (``'PdZrH'``, ``'PdZrH2'``). Each element
#: symbol is an uppercase letter optionally followed by one lowercase
#: letter, optionally followed by a stoichiometric digit run (dropped —
#: tier 1 is identity-only, not stoichiometry-weighted).
_ELEMENT_TOKEN_RE = re.compile(r"[A-Z][a-z]?\d*")
_ELEMENT_SPLIT_RE = re.compile(r"([A-Z][a-z]?)(\d*)")


def _valid_symbols() -> frozenset[str]:
    """The real periodic table's symbol set, from `ase.data` (a core dep —
    always installed, unlike `mendeleev`). Index 0 of `chemical_symbols` is
    the placeholder ``'X'``, not a real element."""
    from ase.data import chemical_symbols

    return frozenset(chemical_symbols[1:])


def _decompose_token(raw: str) -> list[str]:
    """One whitespace/comma-delimited token → its embedded element symbol(s).

    A token that already spells one whole valid symbol case-insensitively
    (``'pd'``, ``'PD'``, ``'Pd'``) is a single element — this is the
    ``'Pd Zr H'`` / ``'pd zr h'`` shape. Otherwise it's read as a
    concatenated formula (``'PdZrH'``, ``'PdZrH2'``) by matching
    Title-case element boundaries; case is **not** folded in that branch
    (folding a concatenated formula first would destroy the symbol
    boundaries — ``'pdzrh'`` has no way back to ``'Pd'``/``'Zr'``/``'H'``).
    A token that fits neither shape round-trips through unchanged, so the
    caller's unknown-symbol check names it verbatim rather than silently
    dropping it.
    """
    valid = _valid_symbols()
    whole = raw[:1].upper() + raw[1:].lower() if raw else raw
    if whole in valid:
        return [whole]
    covered = "".join(_ELEMENT_TOKEN_RE.findall(raw))
    if covered != raw:
        return [raw]
    return [m.group(1) for m in _ELEMENT_SPLIT_RE.finditer(raw)]


def _parse_composition(query: str) -> list[str]:
    """Parse a composition query into a sorted, deduplicated list of valid
    element symbols. Raises :class:`BadInput` naming any unknown symbol."""
    raw_tokens = [t for t in re.split(r"[\s,]+", query.strip()) if t]
    if not raw_tokens:
        raise BadInput(
            "estimate needs one or more element symbols as q= (or id=)",
            next="get(kind='estimate', q='Pd Zr H')",
        )
    symbols: list[str] = []
    for raw in raw_tokens:
        symbols.extend(_decompose_token(raw))

    valid = _valid_symbols()
    unknown = sorted({s for s in symbols if s not in valid})
    if unknown:
        raise BadInput(
            f"unknown element symbol(s): {', '.join(unknown)} (from query {query!r})",
            next="get(kind='estimate', q='Pd Zr H')",
        )
    return sorted(set(symbols))


class EstimateHandler(CacheBackedHandler):
    """``estimate`` — the ms chemistry-workup panel. Slice 1: composition
    tier only (element-property lookup + pairwise alloying heuristics, no
    geometry needed). Cache-pinned (a fixed composition always workups the
    same, deterministic — see `math` for the same pattern)."""

    spec: ClassVar[KindSpec] = KindSpec(
        kind="estimate",
        title="Estimate (ms chemistry workup)",
        description=(
            "Millisecond semi-empirical chemistry workup — a "
            "hypothesis-generator, NOT admissible for rulings (measure "
            "before citing as fact). Slice 1: composition-tier only — "
            "get(kind='estimate', q='Pd Zr H') (or 'PdZrH', 'Pd, Zr') "
            "returns per-element descriptors (electronegativity, covalent "
            "radius, magmom, d-electron count, Hammer-Norskov d-band "
            "center where known) plus pairwise alloying heuristics. See "
            "precis-estimate-help."
        ),
        supports_get=True,
        supports_search=True,
        supports_search_hits=True,
        is_numeric=False,
        id_required=True,
        role="system",
    )

    provider: ClassVar[str] = "estimate"
    # Deterministic for a fixed composition — pin the cache, like `math`.
    ttl_seconds: ClassVar[int | None] = None
    attribution: ClassVar[str] = (
        "ms element-descriptor tier (mendeleev + ase.data + Hammer-Norskov "
        "vendored d-band table) - hypothesis-generating only, "
        "inadmissible for rulings; measure before citing as fact."
    )
    corpus_slug: ClassVar[str] = "default"
    example_query: ClassVar[str] = "Pd Zr H"
    # A short structured answer, not a paragraph worth splitting.
    chunk_target_chars: ClassVar[int] = 0

    def __init__(self, *, hub: Hub) -> None:
        # Deliberately NO dep import here (not even a probe import) — see
        # the module docstring. `CacheBackedHandler.__init__` only needs
        # `hub.store`/`hub.embedder`, neither of which touches mendeleev.
        super().__init__(hub=hub)

    # -- view gate ---------------------------------------------------------
    def get(
        self,
        *,
        id: str | int | None = None,
        q: str | None = None,
        view: str | None = None,
        **kw: Any,
    ) -> Response:
        v = (view or "").strip().lower()
        if v and v not in _PANEL_VIEWS and not self._is_listing_request(id, q):
            raise Unsupported(
                f"unknown view {view!r} for kind='estimate' - slice 1 ships "
                "the default composition panel only",
                options=[*_PANEL_VIEWS, *_PLANNED_VIEWS],
                next=(
                    "get(kind='estimate', q='Pd Zr H') - default "
                    "composition-tier panel",
                    f"planned (slice 2, not built yet): {', '.join(_PLANNED_VIEWS)}",
                ),
            )
        return super().get(id=id, q=q, view=view, **kw)

    # -- cache-base hooks ----------------------------------------------

    def _canonical_key(self, query: str, *, literal: bool = False) -> str:
        """Composition identity, order-independent: sorted, deduplicated,
        space-joined element symbols. ``'Pd Zr H'`` / ``'PdZrH'`` /
        ``'H, Zr, Pd'`` all canonicalise to the same key (and so share one
        cache row)."""
        return " ".join(_parse_composition(query))

    def _recover_key(self, ref: Ref, cache: CacheEntry) -> str | None:
        """Support `mode='refresh'` by slug — mirrors `math`'s
        `_input_query`-in-meta pattern (results are deterministic, so a
        refresh only matters after a code/vendored-table change)."""
        symbols = (cache.meta or {}).get("symbols")
        return " ".join(symbols) if symbols else None

    def _fetch(self, key: str) -> FetchResult:
        symbols = key.split()
        try:
            import mendeleev  # noqa: F401
        except ImportError as e:
            raise Upstream(
                "estimate needs the optional 'estimate' extra "
                "(mendeleev/pymatgen/tblite) — not installed in this venv",
                next="pip install 'precis-mcp[estimate]' "
                "(or: uv sync --extra estimate)",
            ) from e

        from precis_estimate.compute.composition import composition_panel

        title = "estimate: " + " · ".join(symbols) + " (composition tier)"
        body = composition_panel(symbols)
        return FetchResult(
            title=title,
            body_blocks=[BlockInsert(pos=0, text=body)],
            model="ms-element-descriptors-v1",
            cost_usd=0.0,
            meta={"symbols": symbols, "tier": "composition"},
        )
