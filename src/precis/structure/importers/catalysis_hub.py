"""Catalysis-Hub adapter — external DFT library import's first per-source normaliser.

Catalysis-Hub (SUNCAT) publishes DFT surface-reaction data (adsorption +
reaction energies over Pd/Cu/Ni/Pt facets — exactly the catalyst quest's
chemistry) behind a public, key-free GraphQL endpoint
(``https://api.catalysis-hub.org/graphql``). Two layers, per the registry's
adapter contract:

* :func:`adapter` — **pure**, no I/O. Normalises one already-fetched,
  flattened Catalysis-Hub record (one ``(reaction, system)`` pair — see
  :func:`_flatten`) into ``(Scene, ExternalRun, ExternalId)``. Exercised in
  tests against a checked-in fixture, no network, no optional deps.
* :func:`fetch_config` — the thin network layer. POSTs^H^H GETs a GraphQL
  query (Catalysis-Hub's server accepts a ``?query=`` GET, which lets us
  route it through the SSRF-guarded :func:`safe_get` instead of a raw
  ``httpx.Client(follow_redirects=True)`` call) and flattens the
  ``reactions { edges { node { reactionSystems { systems { ... } } } } }``
  response into one dict per system — the shape :func:`adapter` consumes.
  Gated behind the ``[import]`` extra (``httpx``); a missing dep raises
  :class:`CatalysisHubUnsupported` with an install hint, never a bare
  ``ImportError``.

Field-name note: the exact GraphQL field names below (``reactionEnergy``,
``dftFunctional``, ``InputFile``, ``uniqueId``, ...) are the documented
Catalysis-Hub/``cathub`` schema as of this writing, hand-verified against
the public schema docs — **not** exercised against a live query in this
slice (T5 is offline-only, no network in the test gate). Verify against a
live ``https://api.catalysis-hub.org/graphql`` introspection before the
batch-import CLI depends on it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import numpy as np

from ..cell import Cell, as_pbc3
from ..scene import FIX_ALL, Atom, Scene
from . import ExternalId, ExternalRun, register_adapter

if TYPE_CHECKING:
    import httpx

#: The public, key-free Catalysis-Hub GraphQL endpoint.
GRAPHQL_URL = "https://api.catalysis-hub.org/graphql"

#: Atomic-number -> element symbol, scoped to the catalyst quest's palette
#: (NOx/NRR chemistry on Pd/Cu/Ni/Pt) plus common adsorbate/support elements.
#: An adapter deliberately stays dependency-free (no ASE import) — Catalysis-
#: Hub's ``InputFile`` is already ASE-db JSON (numbers/positions/cell/pbc as
#: plain JSON, no binary/pickle), so no ASE is needed to decode it. Extend
#: this table if a future record needs an element outside the current scope.
_SYMBOLS: dict[int, str] = {
    1: "H",
    6: "C",
    7: "N",
    8: "O",
    11: "Na",
    13: "Al",
    14: "Si",
    19: "K",
    22: "Ti",
    24: "Cr",
    25: "Mn",
    26: "Fe",
    27: "Co",
    28: "Ni",
    29: "Cu",
    30: "Zn",
    40: "Zr",
    42: "Mo",
    44: "Ru",
    45: "Rh",
    46: "Pd",
    47: "Ag",
    74: "W",
    75: "Re",
    77: "Ir",
    78: "Pt",
    79: "Au",
}


class CatalysisHubUnsupported(RuntimeError):
    """The Catalysis-Hub fetch layer's dep (``httpx``, the ``[import]`` extra) is missing."""


def _symbol(number: int) -> str:
    try:
        return _SYMBOLS[int(number)]
    except KeyError:
        raise ValueError(
            f"catalysis_hub adapter: unmapped atomic number {number!r} — "
            "extend _SYMBOLS in structure/importers/catalysis_hub.py"
        ) from None


def _scene_from_input_file(geom: dict[str, Any]) -> Scene:
    """Build a :class:`Scene` from an ASE-db-JSON-shaped geometry dict.

    ``geom`` carries ``cell`` (3x3), ``pbc`` (3-bool), ``numbers`` (atomic
    numbers, len N), ``positions`` (Cartesian Å, Nx3), and an optional
    ``constraints`` list (ASE ``FixAtoms``-shaped: ``{"name": "FixAtoms",
    "kwargs": {"indices": [...]}}``) — exactly what Catalysis-Hub's
    ``InputFile(format: "json")`` field returns (ASE's own db-json writer).
    """
    lattice = np.asarray(geom["cell"], dtype=float)
    cell = Cell(lattice, as_pbc3(geom.get("pbc")))

    fixed_indices: set[int] = set()
    for con in geom.get("constraints") or []:
        if con.get("name") == "FixAtoms":
            fixed_indices.update(
                int(i) for i in con.get("kwargs", {}).get("indices", [])
            )

    numbers = geom["numbers"]
    positions = np.asarray(geom["positions"], dtype=float)
    scene = Scene(cell=cell)
    for i, number in enumerate(numbers):
        element = _symbol(number)
        label = scene.next_label(element)
        frac = cell.cart_to_frac(positions[i])
        scene.atoms[label] = Atom(
            label=label,
            element=element,
            frac=frac,
            fixed=FIX_ALL if i in fixed_indices else 0,
        )
    return scene


def adapter(raw: object) -> tuple[Scene, ExternalRun, ExternalId]:
    """Normalise one flattened Catalysis-Hub ``(reaction, system)`` record.

    Pure — no I/O. ``raw`` is the shape :func:`_flatten` (and the
    ``tests/fixtures/catalysis/`` fixture) produce: reaction-level fields
    (``reactionEnergy``, ``facet``, ``surfaceComposition``, ``reactants``,
    ``products``, ``dftCode``, ``dftFunctional``, ``publication``) alongside
    one nested ``system`` dict (``uniqueId``/``id``, ``energy``,
    ``InputFile`` — the already-decoded ASE-db-JSON geometry, see
    :func:`_scene_from_input_file`).

    Mapping:

    * ``Scene`` — built from ``system.InputFile`` (cell/pbc/numbers/
      positions/constraints).
    * ``ExternalRun.energy`` — ``reactionEnergy`` when present (the
      adsorption/reaction energy, the quantity the catalyst quest cares
      about), else the raw DFT total ``system.energy``.
    * ``ExternalRun.max_force`` — Catalysis-Hub's GraphQL schema does not
      expose a per-system max-force field (only the relaxed geometry +
      final energy), so this is always ``None`` for this source.
    * ``ExternalRun.final_geometry`` — the ``system.InputFile`` dict
      verbatim (already JSONB-serializable).
    * ``ExternalRun.method`` — ``functional``/``code``/``facet``/
      ``surface_composition``/``reactants``/``products``/``dataset_doi``
      (+ ``cutoff_eV``/``spin`` when the record carries them — Catalysis-
      Hub's public schema does not, today, so they land as ``None``).
    * ``ExternalId`` — ``dataset='catalysis-hub'``, ``config_id`` = the
      system's ``uniqueId`` (falls back to its numeric ``id``) — the
      idempotent collapse key.
    """
    assert isinstance(raw, dict), (
        f"catalysis-hub adapter expects a dict record, got {type(raw)}"
    )
    system = raw["system"]
    geometry = system["InputFile"]
    scene = _scene_from_input_file(geometry)

    energy = raw.get("reactionEnergy")
    if energy is None:
        energy = system["energy"]

    publication = raw.get("publication") or {}
    method = {
        "functional": raw.get("dftFunctional"),
        "code": raw.get("dftCode"),
        "cutoff_eV": raw.get("cutoffEnergy"),
        "spin": raw.get("spin"),
        "dataset_doi": publication.get("doi"),
        "facet": raw.get("facet"),
        "surface_composition": raw.get("surfaceComposition"),
        "reactants": raw.get("reactants"),
        "products": raw.get("products"),
    }

    run = ExternalRun(
        energy=float(energy),
        max_force=None,
        final_geometry=geometry,
        method=method,
        provenance="external",
    )

    config_id = str(system.get("uniqueId") or system["id"])
    eid = ExternalId(dataset="catalysis-hub", config_id=config_id)
    return scene, run, eid


def _flatten(node: dict[str, Any]) -> list[dict[str, Any]]:
    """One GraphQL ``reactions.edges[].node`` -> one flattened dict per
    ``reactionSystems[].systems`` entry (the shape :func:`adapter` wants).

    Reaction-level fields ride along on every flattened row; only the
    per-system geometry/energy/id differ. ``InputFile`` arrives from
    GraphQL as a JSON-encoded *string* (a String-typed scalar) — decoded
    here via stdlib ``json``, no ASE needed (see the module docstring).
    """
    import json

    reaction_fields = {
        "Equation": node.get("Equation"),
        "chemicalComposition": node.get("chemicalComposition"),
        "surfaceComposition": node.get("surfaceComposition"),
        "facet": node.get("facet"),
        "reactants": node.get("reactants"),
        "products": node.get("products"),
        "reactionEnergy": node.get("reactionEnergy"),
        "activationEnergy": node.get("activationEnergy"),
        "dftCode": node.get("dftCode"),
        "dftFunctional": node.get("dftFunctional"),
        "username": node.get("username"),
        "pubId": node.get("pubId"),
        "publication": node.get("publication"),
    }
    out: list[dict[str, Any]] = []
    for rs in node.get("reactionSystems") or []:
        sys_raw = dict(rs.get("systems") or {})
        input_file = sys_raw.get("InputFile")
        if isinstance(input_file, str):
            sys_raw["InputFile"] = json.loads(input_file)
        row = dict(reaction_fields)
        row["name"] = rs.get("name")
        row["system"] = sys_raw
        out.append(row)
    return out


_REACTIONS_QUERY = """
{{
  reactions(first: {first}, {filter_expr}) {{
    edges {{
      node {{
        Equation
        chemicalComposition
        surfaceComposition
        facet
        reactants
        products
        reactionEnergy
        activationEnergy
        dftCode
        dftFunctional
        username
        pubId
        publication {{ doi }}
        reactionSystems {{
          name
          systems {{
            id
            uniqueId
            energy
            InputFile(format: "json")
          }}
        }}
      }}
    }}
  }}
}}
""".strip()


def fetch_config(
    *,
    surface_composition: str | None = None,
    facet: str | None = None,
    first: int = 25,
    url: str = GRAPHQL_URL,
) -> list[dict[str, Any]]:
    """Query Catalysis-Hub's GraphQL API and flatten the result.

    Thin network layer only — all normalisation happens in :func:`adapter`.
    Filters by ``surfaceComposition``/``facet`` when given (e.g.
    ``surface_composition="Pd", facet="111"``). GraphQL's server accepts a
    plain ``?query=...`` GET, so this routes through the SSRF-guarded
    :func:`precis.utils.safe_fetch.safe_get` rather than an unguarded POST.

    Raises:
        CatalysisHubUnsupported: ``httpx`` (the ``[import]`` extra) isn't
            installed.
    """
    try:
        import httpx  # noqa: F401  availability probe (raise the domain error); used in the annotation below
    except ImportError as exc:
        raise CatalysisHubUnsupported(
            "Catalysis-Hub fetch needs httpx — pip install 'precis-mcp[import]'"
        ) from exc

    from ...utils.http import http_client
    from ...utils.safe_fetch import safe_get

    clauses = []
    if surface_composition is not None:
        clauses.append(f'surfaceComposition: "{surface_composition}"')
    if facet is not None:
        clauses.append(f'facet: "{facet}"')
    filter_expr = ", ".join(clauses)
    query = _REACTIONS_QUERY.format(first=int(first), filter_expr=filter_expr)

    with http_client(timeout=30.0, user_agent=None) as client:
        resp: httpx.Response = safe_get(client, url, params={"query": query})
        resp.raise_for_status()
        payload = resp.json()

    if payload.get("errors"):
        raise RuntimeError(f"Catalysis-Hub GraphQL error: {payload['errors']}")

    out: list[dict[str, Any]] = []
    for edge in payload["data"]["reactions"]["edges"]:
        out.extend(_flatten(edge["node"]))
    return out


register_adapter("catalysis-hub", adapter)

__all__ = [
    "GRAPHQL_URL",
    "CatalysisHubUnsupported",
    "adapter",
    "fetch_config",
]
