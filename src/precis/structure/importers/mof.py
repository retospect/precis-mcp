"""QMOF and CoRE MOF adapters — MOF structure seeds for the import seam.

Both adapters are pure over one recorded raw record::

    {"id": <QMOF id | CoRE refcode>, "cif_text": <CIF>, "energy"?: eV, "method"?: {...}}

and return ``(Scene, ExternalRun, ExternalId)`` with ``dataset`` the source
name and ``config_id`` the record id. The CIF goes through
:func:`precis.structure.importers.cif.cif_to_scene` (size and atom caps).
Where a raw record comes from is the caller's business: the NAS bundle
index (:mod:`.bundle_index`) for both sources, or MPContribs for one QMOF
record when there is no index (:func:`fetch_qmof_mpcontribs`).

**QMOF** (Rosen et al., Matter 4, 1578 (2021); npj Comput. Mater. 8, 112
(2022)): ~20k MOFs relaxed with VASP at PBE-D3(BJ). Licence: the QMOF
README's Licensing section (the authors' GitHub, read 2026-10-18) states
CC BY 4.0 for the data, and MPContribs labels the ``qmof`` project CCA4
(CC BY 4.0); the figshare bundle page (doi:10.6084/m9.figshare.13147324)
refused the connection that day, so the bundle's own licence is
unconfirmed. The method fingerprint below (code, functional, 520 eV
cutoff) is from the QMOF paper, not read from a record.

Energy source order: the bundle index first (its ``qmof.json`` carries
the PBE total energy), MPContribs only when no index is configured.
MPContribs' ``qmof`` columns hold band gaps and charges, not a total
energy, so a record fetched there imports geometry only.

**CoRE MOF 2024** (Zenodo doi:10.5281/zenodo.15055758, v1.1, CC BY 4.0
confirmed): experimental, solvent-removed CIFs and no DFT energy, so a
CoRE record always imports with ``energy=None``.

Unit enclave (``precis.structure`` package docstring): eV and Å.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from . import ExternalId, ExternalRun, register_adapter
from .cif import cif_to_scene

if TYPE_CHECKING:
    from ..scene import Scene

QMOF_DATASET = "qmof"
CORE_MOF_DATASET = "core-mof"

QMOF_DOI = "10.6084/m9.figshare.13147324"
CORE_MOF_DOI = "10.5281/zenodo.15055758"

#: The QMOF calculation settings, per the QMOF paper.
QMOF_METHOD: dict[str, Any] = {
    "source": "qmof",
    "code": "VASP",
    "functional": "PBE-D3(BJ)",
    "cutoff_eV": 520,
    "geometry": "DFT-relaxed",
    "dataset_doi": QMOF_DOI,
}

CORE_MOF_METHOD: dict[str, Any] = {
    "source": "core-mof-2024",
    "geometry": "experimental, solvent-removed",
    "dataset_doi": CORE_MOF_DOI,
}

#: MPContribs REST root (the same API ``precis_dft.pourbaix_bulk`` reads).
MPCONTRIBS_URL = "https://contribs-api.materialsproject.org"


def _record(raw: object, source: str) -> tuple[str, str]:
    if not isinstance(raw, dict):
        raise ValueError(f"{source} adapter expects a dict record, got {type(raw)}")
    record_id = str(raw.get("id") or "").strip()
    cif_text = raw.get("cif_text")
    if not record_id or not isinstance(cif_text, str):
        raise ValueError(f"{source} adapter needs a record with 'id' and 'cif_text'")
    return record_id, cif_text


def qmof_adapter(raw: object) -> tuple[Scene, ExternalRun, ExternalId]:
    """One QMOF record -> a DFT-relaxed framework, with its PBE energy when known."""
    record_id, cif_text = _record(raw, QMOF_DATASET)
    assert isinstance(raw, dict)
    scene = cif_to_scene(cif_text)
    energy = raw.get("energy")
    method = {**QMOF_METHOD, **(raw.get("method") or {})}
    run = ExternalRun(
        energy=None if energy is None else float(energy),
        max_force=None,
        final_geometry=None,
        method=method,
    )
    return scene, run, ExternalId(dataset=QMOF_DATASET, config_id=record_id)


def core_mof_adapter(raw: object) -> tuple[Scene, ExternalRun, ExternalId]:
    """One CoRE MOF record -> an experimental framework with no energy."""
    record_id, cif_text = _record(raw, CORE_MOF_DATASET)
    assert isinstance(raw, dict)
    scene = cif_to_scene(cif_text)
    method = {**CORE_MOF_METHOD, **(raw.get("method") or {})}
    run = ExternalRun(energy=None, max_force=None, final_geometry=None, method=method)
    return scene, run, ExternalId(dataset=CORE_MOF_DATASET, config_id=record_id)


#: Adapters whose raw record is ``{id, cif_text, ...}`` — the ones a CIF
#: ``put`` with ``source=`` and the bundle index can feed.
CIF_ADAPTERS = (QMOF_DATASET, CORE_MOF_DATASET)


def fetch_qmof_mpcontribs(
    qmof_id: str, api_key: str, *, url: str = MPCONTRIBS_URL
) -> dict[str, Any] | None:
    """One QMOF record from MPContribs as a raw record with no energy.

    Two GETs through :func:`safe_get`: the ``qmof`` contribution by
    ``identifier`` (for its attached structure id), then that structure's
    CIF. ``None`` when MPContribs has no such identifier. Checked keyless on
    2026-10-18Z: the contribution answers ``data[].structures[].id``, and
    the API's ``Structures`` document has a ``cif`` field (pymatgen
    ``CifWriter`` output), which keyless reads return empty, so the CIF
    read itself is unexercised.
    """
    from dataclasses import replace
    from urllib.parse import quote

    from ...utils import safe_fetch
    from ...utils.http import http_client, policy_for, polite_user_agent, with_backoff

    # Interactive lookup: bounded, not the 5-try / 60 s default.
    policy = replace(
        policy_for("materialsproject"), attempts=2, cap=10.0, limiter_wait_s=5.0
    )
    contributions_url = f"{url}/contributions/"
    headers = {"x-api-key": api_key}
    with http_client(
        timeout=30.0, headers=headers, user_agent=polite_user_agent()
    ) as client:
        resp = with_backoff(
            lambda: safe_fetch.safe_get(
                client,
                contributions_url,
                params={
                    "project": QMOF_DATASET,
                    "identifier": qmof_id,
                    "_fields": "id,identifier,structures",
                },
            ),
            service="materialsproject",
            policy=policy,
            url=contributions_url,
        )
        resp.raise_for_status()
        rows = (resp.json() or {}).get("data") or []
        if not rows:
            return None
        structures = rows[0].get("structures") or []
        if not structures:
            raise ValueError(f"MPContribs qmof record {qmof_id!r} has no structure")
        structure_id = structures[0].get("id")
        structure_url = f"{url}/structures/{quote(str(structure_id), safe='')}/"
        sresp = with_backoff(
            lambda: safe_fetch.safe_get(
                client, structure_url, params={"_fields": "cif"}
            ),
            service="materialsproject",
            policy=policy,
            url=structure_url,
        )
        sresp.raise_for_status()
        cif_text = (sresp.json() or {}).get("cif")
    if not isinstance(cif_text, str):
        raise ValueError(f"MPContribs structure {structure_id!r} returned no CIF")
    return {
        "id": qmof_id,
        "cif_text": cif_text,
        "energy": None,
        "method": {"fetched_from": "mpcontribs"},
    }


register_adapter(QMOF_DATASET, qmof_adapter)
register_adapter(CORE_MOF_DATASET, core_mof_adapter)

__all__ = [
    "CIF_ADAPTERS",
    "CORE_MOF_DATASET",
    "CORE_MOF_METHOD",
    "QMOF_DATASET",
    "QMOF_METHOD",
    "core_mof_adapter",
    "fetch_qmof_mpcontribs",
    "qmof_adapter",
]
