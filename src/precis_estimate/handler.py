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

import copy
import json
import re
from typing import TYPE_CHECKING, Any, ClassVar

from precis.errors import BadInput, NotFound, Unsupported, Upstream
from precis.handlers._cache_base import CacheBackedHandler, FetchResult
from precis.handlers._slug_ref_shared import resolve_live_slug_ref
from precis.protocol import KindSpec
from precis.response import Response
from precis.store.types import ChunkInsert
from precis.structure import OpError, apply_ops
from precis.structure.cache import structure_sha
from precis.utils import handle_registry

if TYPE_CHECKING:
    from precis.dispatch import Hub
    from precis.store.types import CacheEntry, Ref

#: Slice-1 views. ``None`` / ``''`` / ``'panel'`` all mean "the default
#: composition panel" — there's only one view today.
_PANEL_VIEWS = ("panel",)

#: Slice-2 views over a structure handle (``id='st<...>'``) — the plain
#: workup panel (``None``/``'panel'``, same names as the composition tier)
#: plus ``'compare'`` (needs ``args={'against': 'st<...>'}``). ``whatif``
#: is not a *view* — it's ``args={'ops': [...]}`` on the plain panel or on
#: ``compare``'s primary side, see the module docstring.
_STRUCTURE_VIEWS = ("compare",)

#: Named here (not built) so the unknown-view error can point at what's
#: coming without silently pretending it exists. See the module docstring
#: and docs/backlog/estimate-kind-ms-chemistry-workup.md.
_PLANNED_VIEWS = (
    "shape",
    "orbitals",
    "spin",
    "kinetics",
    "card",
)

#: Marks a ``_canonical_key`` query string as the slice-2 structure-tier
#: composite (a canonical JSON payload the handler built in ``get()``, not
#: user-facing text) rather than a slice-1 composition token string. Chosen
#: to be unambiguous against any real composition query (which is always
#: bare element symbols — never starts with this).
_STRUCT_KEY_PREFIX = "estimate-structure-v1:"

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
    tier (element-property lookup + pairwise alloying heuristics, no
    geometry needed). Slice 2: structure-coupled workup
    (``id='st<...>'``) — geometry lint, coordination/strain, symmetry,
    dedup-vs-quest, and own-campaign BEP scaling
    (:mod:`precis_estimate.compute.structure`), plus a structural what-if
    (``args={'ops': [...]}``) and a two-structure ``view='compare'``. Both
    tiers are cache-pinned (deterministic for a fixed input — see `math`
    for the same pattern)."""

    spec: ClassVar[KindSpec] = KindSpec(
        kind="estimate",
        title="Estimate (ms chemistry workup)",
        description=(
            "Millisecond semi-empirical chemistry workup — a "
            "hypothesis-generator, NOT admissible for rulings (measure "
            "before citing as fact). Composition tier — "
            "get(kind='estimate', q='Pd Zr H') (or 'PdZrH', 'Pd, Zr') "
            "returns per-element descriptors (electronegativity, covalent "
            "radius, magmom, d-electron count, Hammer-Norskov d-band "
            "center where known) plus pairwise alloying heuristics. "
            "Structure tier — get(kind='estimate', id='st<...>') runs "
            "geometry lint + coordination/strain + spglib symmetry + a "
            "StructureMatcher dedup + own-campaign BEP scaling on a held "
            "structure design; args={'ops': [...]} applies a what-if "
            "mutation first (a structure/ops.py op list, on a copy — the "
            "held design is untouched); args={'quest': 'qu<...>'} grounds "
            "dedup/BEP in that quest's served structures; "
            "view='compare', args={'against': 'st<...>'} runs both and "
            "adds a numeric delta table. See precis-estimate-help."
        ),
        supports_get=True,
        supports_search=True,
        supports_search_hits=True,
        is_numeric=False,
        id_required=True,
        placement="system",
    )

    provider: ClassVar[str] = "estimate"
    # Deterministic for a fixed composition — pin the cache, like `math`.
    ttl_seconds: ClassVar[int | None] = None
    attribution: ClassVar[str] = (
        "ms chemistry workup (composition tier: mendeleev + ase.data + "
        "Hammer-Norskov vendored d-band table; structure tier: preflight "
        "geometry lint + invariants coordination/strain + spglib symmetry "
        "+ pymatgen StructureMatcher dedup + own-campaign BEP scaling) - "
        "hypothesis-generating only, inadmissible for rulings; measure "
        "before citing as fact."
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

    # -- view gate + structure-tier routing ---------------------------------
    def get(
        self,
        *,
        id: str | int | None = None,
        q: str | None = None,
        view: str | None = None,
        args: dict[str, Any] | None = None,
        **kw: Any,
    ) -> Response:
        v = (view or "").strip().lower()
        allowed = (*_PANEL_VIEWS, *_STRUCTURE_VIEWS)
        if v and v not in allowed and not self._is_listing_request(id, q):
            raise Unsupported(
                f"unknown view {view!r} for kind='estimate'",
                options=[*allowed, *_PLANNED_VIEWS],
                next=(
                    "get(kind='estimate', q='Pd Zr H') - composition-tier panel",
                    "get(kind='estimate', id='st123') - structure-tier panel",
                    f"planned (slice 3, not built yet): {', '.join(_PLANNED_VIEWS)}",
                ),
            )

        parsed = (
            handle_registry.parse(id) if isinstance(id, str) and id.strip() else None
        )
        if parsed is not None and parsed[0] == "structure" and not parsed[1]:
            return self._get_structure(
                ref_id=parsed[2], view=v or "panel", args=args or {}, **kw
            )
        if v == "compare":
            raise BadInput(
                "view='compare' needs id='st<...>' (a structure handle)",
                next="get(kind='estimate', id='st123', view='compare', "
                "args={'against': 'st456'})",
            )
        return super().get(id=id, q=q, view=view, **kw)

    # -- structure-tier request assembly ------------------------------------

    def _resolve_ref_arg(self, kind: str, raw: Any) -> int | None:
        """Coerce an ``args=`` value (handle string / slug / bare id) naming
        another ref to its ref_id, or ``None`` when the arg was omitted.
        Raises :class:`NotFound` on a value that was given but doesn't
        resolve — never silently drops it."""
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return None
        ref = resolve_live_slug_ref(self.store, kind=kind, id=raw)
        return int(ref.id)

    def _get_structure(
        self, *, ref_id: int, view: str, args: dict[str, Any], **kw: Any
    ) -> Response:
        """Assemble the slice-2 composite cache key (structure ref id +
        content-identity sha + ops + quest + against) and hand it to the
        base cache flow like any other query — ``_fetch`` decodes it back
        on a miss (see :func:`_STRUCT_KEY_PREFIX`)."""
        ref = self.store.get_ref(kind="structure", id=ref_id)
        if ref is None:
            raise NotFound(
                f"structure ref {ref_id} not found",
                next="search(kind='structure', q='...')",
            )
        base_scene, _handles = self.store.structure_load(ref_id)
        sha = structure_sha(base_scene)

        ops = args.get("ops")
        if ops is not None and not isinstance(ops, list):
            raise BadInput("args['ops'] must be a list of op dicts")

        quest_ref_id = self._resolve_ref_arg("quest", args.get("quest"))

        against: dict[str, Any] | None = None
        if view == "compare":
            against_raw = args.get("against")
            if not against_raw:
                raise BadInput(
                    "view='compare' needs args={'against': 'st<...>'}",
                    next="get(kind='estimate', id='st123', view='compare', "
                    "args={'against': 'st456'})",
                )
            against_ref_id = self._resolve_ref_arg("structure", against_raw)
            assert against_ref_id is not None  # non-empty raw ⇒ resolved or raised
            against_scene, _ = self.store.structure_load(against_ref_id)
            against = {"ref": against_ref_id, "sha": structure_sha(against_scene)}

        payload = {
            "ref": ref_id,
            "sha": sha,
            "view": view,
            "ops": ops or [],
            "quest": quest_ref_id,
            "against": against,
        }
        key = _STRUCT_KEY_PREFIX + json.dumps(
            payload, sort_keys=True, separators=(",", ":")
        )
        return super().get(id=key, view=view, **kw)

    # -- cache-base hooks ----------------------------------------------

    def _canonical_key(self, query: str, *, literal: bool = False) -> str:
        """Composition identity, order-independent: sorted, deduplicated,
        space-joined element symbols. ``'Pd Zr H'`` / ``'PdZrH'`` /
        ``'H, Zr, Pd'`` all canonicalise to the same key (and so share one
        cache row). A slice-2 structure-tier composite key (built by
        :meth:`_get_structure`, already canonical JSON) passes through
        unchanged — recognised by its :data:`_STRUCT_KEY_PREFIX`."""
        if query.startswith(_STRUCT_KEY_PREFIX):
            return query
        return " ".join(_parse_composition(query))

    def _recover_key(self, ref: Ref, cache: CacheEntry) -> str | None:
        """Support `mode='refresh'` by slug — mirrors `math`'s
        `_input_query`-in-meta pattern (results are deterministic, so a
        refresh only matters after a code/vendored-table change).
        Structure-tier entries don't stash `symbols` — refresh-by-slug
        isn't supported for them yet (re-`get` by id= re-keys automatically
        whenever the structure's content sha or the ops/quest/against args
        change)."""
        symbols = (cache.meta or {}).get("symbols")
        return " ".join(symbols) if symbols else None

    def _fetch(self, key: str) -> FetchResult:
        if key.startswith(_STRUCT_KEY_PREFIX):
            return self._fetch_structure(json.loads(key[len(_STRUCT_KEY_PREFIX) :]))
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
            body_blocks=[ChunkInsert(ord=0, text=body)],
            model="ms-element-descriptors-v1",
            cost_usd=0.0,
            meta={"symbols": symbols, "tier": "composition"},
        )

    def _load_ops_scene(self, ref_id: int, ops: list[dict[str, Any]]) -> Any:
        """The design's live scene, with ``ops`` applied on a COPY (the
        what-if mutation never touches the held design). An :class:`OpError`
        surfaces as :class:`BadInput` naming the bad op — the design doc's
        "ops errors surface as BadInput" contract."""
        scene, _handles = self.store.structure_load(ref_id)
        if not ops:
            return scene
        mutant = copy.deepcopy(scene)
        try:
            apply_ops(mutant, ops)
        except OpError as exc:
            raise BadInput(f"estimate what-if op error: {exc}") from exc
        return mutant

    def _fetch_structure(self, spec: dict[str, Any]) -> FetchResult:
        # No dep import at module scope — pymatgen is lazily imported inside
        # compute/structure.py's dedup path only, mirroring mendeleev above.
        from precis_estimate.compute.structure import render_compare, structure_workup

        ref_id = int(spec["ref"])
        ref = self.store.get_ref(kind="structure", id=ref_id)
        if ref is None:
            raise NotFound(f"structure ref {ref_id} not found")
        name = str(ref.slug or ref_id)
        scene = self._load_ops_scene(ref_id, spec.get("ops") or [])
        quest_ref_id = spec.get("quest")

        against = spec.get("against")
        if against is not None:
            against_ref_id = int(against["ref"])
            against_ref = self.store.get_ref(kind="structure", id=against_ref_id)
            if against_ref is None:
                raise NotFound(f"structure ref {against_ref_id} not found")
            against_name = str(against_ref.slug or against_ref_id)
            against_scene, _ = self.store.structure_load(against_ref_id)
            body = render_compare(
                name,
                scene,
                against_name,
                against_scene,
                store=self.store,
                quest_ref_id=quest_ref_id,
            )
            title = f"estimate: compare {name} vs {against_name}"
            meta = {
                "tier": "structure",
                "view": "compare",
                "ref": ref_id,
                "against": against_ref_id,
            }
        else:
            body = structure_workup(
                scene, store=self.store, quest_ref_id=quest_ref_id, title=name
            )
            title = f"estimate: {name} (structure tier)"
            meta = {
                "tier": "structure",
                "view": spec.get("view") or "panel",
                "ref": ref_id,
            }

        return FetchResult(
            title=title,
            body_blocks=[ChunkInsert(ord=0, text=body)],
            model="ms-structure-workup-v1",
            cost_usd=0.0,
            meta=meta,
        )
