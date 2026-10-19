"""Materials Project adapter — bulk crystals, formation energies, surface energies.

Materials Project (MP) serves bulk structures with formation energies and,
for some materials, a ``surface_properties`` record (per-facet surface
energy and work function, weighted Wulff values). Two layers, per the
registry's adapter contract:

* :func:`adapter` — **pure**, no I/O. Normalises one already-fetched record
  ``{"summary": <mp-api summary doc>, "surface_properties": <doc> | None}``
  (see :func:`fetch_record`) into ``(Scene, ExternalRun, ExternalId)``. The
  summary's ``structure`` is a pymatgen ``Structure.as_dict()`` (lattice
  matrix + sites with fractional ``abc``), decoded here without pymatgen.
* :func:`fetch_record` — the thin network layer: two GETs through the
  SSRF-guarded :func:`safe_get` against ``api.materialsproject.org`` with the
  ``PRECIS_MP_API_KEY`` secret (the same one the ``pourbaix_bulk`` job uses)
  as ``X-API-Key``. A missing key raises :class:`MaterialsProjectKeyMissing`
  before any request; a 401/403 raises :class:`MaterialsProjectAuthRejected`.
  The key is never logged or echoed.

Field names follow the documented mp-api summary and emmet
``SurfacePropDoc`` schemas; they are exercised against recorded fixtures
only, not a live query in this slice.

Unit enclave (`precis.structure` package docstring): ``ExternalRun.energy`` is
the **formation energy per atom in eV/atom** (``method['energy_kind']`` says
so) — not a total energy, so it must never be compared with a slab total.
Surface energies are kept in MP's J/m², named in the keys.
"""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any

import numpy as np

from ..cell import Cell
from ..scene import Atom, Scene
from . import ExternalId, ExternalRun, register_adapter

#: Materials Project REST API root.
API_ROOT = "https://api.materialsproject.org"

#: Secret holding the MP API key (vault, process env first); shared with the
#: ``pourbaix_bulk`` job (``workers/job_types/pourbaix_bulk.py``).
MP_KEY_SECRET = "PRECIS_MP_API_KEY"

_MATERIAL_ID_RE = re.compile(r"^mp-\d{1,9}$")

_SUMMARY_FIELDS = (
    "material_id,formula_pretty,structure,formation_energy_per_atom,"
    "energy_above_hull,symmetry,database_version"
)


class MaterialsProjectKeyMissing(RuntimeError):
    """No ``PRECIS_MP_API_KEY`` secret is set; nothing was requested."""


class MaterialsProjectAuthRejected(RuntimeError):
    """The server answered 401/403 to the configured key."""

    def __init__(self, status: int) -> None:
        self.status = status
        super().__init__(
            f"Materials Project rejected the configured {MP_KEY_SECRET} (HTTP {status})"
        )


def valid_material_id(material_id: str) -> bool:
    """True for an ``mp-<digits>`` id (the only shape :func:`fetch_record` accepts)."""
    return bool(_MATERIAL_ID_RE.match(material_id))


def _scene_from_structure(struct: dict[str, Any]) -> tuple[Scene, dict[str, Any]]:
    """``Structure.as_dict()`` -> (:class:`Scene`, ASE-shaped geometry dict)."""
    lattice = np.asarray(struct["lattice"]["matrix"], dtype=float)
    cell = Cell(lattice, (True, True, True))
    scene = Scene(cell=cell)
    symbols: list[str] = []
    cart: list[list[float]] = []
    for site in struct["sites"]:
        species = site["species"]
        if len(species) != 1 or float(species[0].get("occu", 1.0)) != 1.0:
            raise ValueError(
                "materials_project adapter: disordered/partially occupied site "
                f"{[sp.get('element') for sp in species]!r} — only ordered "
                "structures are importable"
            )
        element = str(species[0]["element"])
        label = scene.next_label(element)
        frac = np.asarray(site["abc"], dtype=float)
        scene.atoms[label] = Atom(label=label, element=element, frac=frac)
        symbols.append(element)
        cart.append(cell.frac_to_cart(frac).tolist())
    geometry = {
        "cell": lattice.tolist(),
        "pbc": [True, True, True],
        "symbols": symbols,
        "positions": cart,
    }
    return scene, geometry


def _surface_block(doc: dict[str, Any] | None) -> dict[str, Any] | None:
    """Compact the ``surface_properties`` doc (drops per-facet structures)."""
    if not doc:
        return None
    facets = [
        {
            "miller_index": s.get("miller_index"),
            "surface_energy_J_m2": s.get("surface_energy"),
            "work_function_eV": s.get("work_function"),
            "area_fraction": s.get("area_fraction"),
            "is_reconstructed": s.get("is_reconstructed"),
        }
        for s in doc.get("surfaces") or []
    ]
    return {
        "weighted_surface_energy_J_m2": doc.get("weighted_surface_energy"),
        "weighted_work_function_eV": doc.get("weighted_work_function"),
        "surface_anisotropy": doc.get("surface_anisotropy"),
        "shape_factor": doc.get("shape_factor"),
        "has_reconstructed": doc.get("has_reconstructed"),
        "facets": facets,
    }


def adapter(raw: object) -> tuple[Scene, ExternalRun, ExternalId]:
    """Normalise one MP record ``{"summary": {...}, "surface_properties": {...}|None}``.

    Pure — no I/O. Mapping:

    * ``Scene`` — the summary's bulk ``structure`` (3D periodic).
    * ``ExternalRun.energy`` — ``formation_energy_per_atom`` (eV/atom);
      absent → ``ValueError`` (the run needs a number, and total energies
      are not formation energies).
    * ``ExternalRun.max_force`` — ``None`` (MP's summary carries none).
    * ``ExternalRun.method`` — ``energy_kind``, ``energy_above_hull_eV``,
      ``spacegroup``, ``formula``, ``database_version``, ``functional``
      (MP's PBE-family GGA/GGA+U mix, not one functional), ``code`` and, when
      the record has one, a ``surface`` block (weighted + per-facet surface
      energies in J/m², work functions in eV).
    * ``ExternalId`` — ``dataset='materials-project'``, ``config_id`` = the
      ``mp-<n>`` material id.
    """
    assert isinstance(raw, dict), (
        f"materials-project adapter expects a dict record, got {type(raw)}"
    )
    summary = raw["summary"]
    scene, geometry = _scene_from_structure(summary["structure"])

    energy = summary.get("formation_energy_per_atom")
    if energy is None:
        raise ValueError(
            f"materials_project adapter: {summary.get('material_id')!r} has no "
            "formation_energy_per_atom — nothing to record as its energy"
        )

    symmetry = summary.get("symmetry") or {}
    method: dict[str, Any] = {
        "source": "materials-project",
        "functional": "PBE-family GGA/GGA+U (MP mixing scheme)",
        "code": "VASP",
        "energy_kind": "formation_energy_per_atom_eV",
        "energy_above_hull_eV": summary.get("energy_above_hull"),
        "spacegroup": symmetry.get("symbol"),
        "formula": summary.get("formula_pretty"),
        "database_version": summary.get("database_version"),
        "surface": _surface_block(raw.get("surface_properties")),
    }
    run = ExternalRun(
        energy=float(energy),
        max_force=None,
        final_geometry=geometry,
        method=method,
        provenance="external",
    )
    return scene, run, ExternalId("materials-project", str(summary["material_id"]))


def _get_json(
    client: Any,
    url: str,
    params: dict[str, str],
    headers: dict[str, str],
    *,
    allow_404: bool = False,
) -> dict[str, Any] | None:
    from ...utils.http import VendorError, policy_for, with_backoff
    from ...utils.safe_fetch import safe_get

    try:
        resp = with_backoff(
            lambda: safe_get(client, url, params=params, headers=headers),
            service="materialsproject",
            # Interactive lookup: bounded, not the 5-try / 60 s default.
            policy=replace(
                policy_for("materialsproject"),
                attempts=2,
                cap=10.0,
                limiter_wait_s=5.0,
            ),
            url=url,
        )
    except VendorError as exc:
        # 401/403 are fatal statuses: with_backoff raises instead of returning.
        if exc.status in (401, 403):
            raise MaterialsProjectAuthRejected(exc.status) from exc
        raise
    if allow_404 and resp.status_code == 404:
        return None
    resp.raise_for_status()
    payload: dict[str, Any] = resp.json()
    return payload


def fetch_record(
    material_id: str, *, api_root: str = API_ROOT
) -> dict[str, Any] | None:
    """Fetch one material's summary (+ surface properties when MP has them).

    Returns the dict :func:`adapter` consumes, or ``None`` when MP has no
    such material.

    Raises:
        ValueError: ``material_id`` is not ``mp-<digits>``.
        MaterialsProjectKeyMissing: no ``PRECIS_MP_API_KEY`` secret (no request made).
        MaterialsProjectAuthRejected: the server answered 401/403.
    """
    if not valid_material_id(material_id):
        raise ValueError(f"not a Materials Project id: {material_id!r}")

    from ... import secrets as _secrets
    from ...utils.http import http_client, polite_user_agent

    api_key = (_secrets.get_secret(MP_KEY_SECRET) or "").strip()
    if not api_key:
        raise MaterialsProjectKeyMissing(f"no {MP_KEY_SECRET} secret set")
    headers = {"X-API-Key": api_key}

    with http_client(timeout=30.0, user_agent=polite_user_agent()) as client:
        summary_payload = _get_json(
            client,
            f"{api_root}/materials/summary/",
            {"material_ids": material_id, "_fields": _SUMMARY_FIELDS},
            headers,
        )
        docs = (summary_payload or {}).get("data") or []
        if not docs:
            return None
        surf_payload = _get_json(
            client,
            f"{api_root}/materials/surface_properties/",
            {"material_ids": material_id},
            headers,
            allow_404=True,
        )
    surf_docs = (surf_payload or {}).get("data") or []
    return {
        "summary": docs[0],
        "surface_properties": surf_docs[0] if surf_docs else None,
    }


register_adapter("materials-project", adapter)

__all__ = [
    "API_ROOT",
    "MP_KEY_SECRET",
    "MaterialsProjectAuthRejected",
    "MaterialsProjectKeyMissing",
    "adapter",
    "fetch_record",
    "valid_material_id",
]
