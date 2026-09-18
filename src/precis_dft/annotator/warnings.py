"""Quality warnings emitted as part of the views.

The annotator catches the most common slab / structure mistakes and
surfaces them as a flat list of short strings. The LLM (and any
human reviewing) reads these immediately, before the rest of the
views, so a structure that's about to produce a bad calculation
gets caught at the workbench stage rather than after the DFT run.

Warnings are heuristic, not assertions — the agent can decide to
proceed in spite of them (a deliberately thin slab for a study of
finite-size effects is legitimate). They surface concerns; they
don't block.
"""

from __future__ import annotations

from typing import Any

from ase import Atoms

from .header import detect_dimensionality, detect_slab_layers

#: Vacuum below this (Å) on a slab triggers a warning.
_VACUUM_FLOOR = 15.0

#: Slab thickness below this (layers) triggers a warning.
_LAYER_FLOOR = 4

#: Elements that commonly need explicit magnetic seeding.
_MAGNETIC_ELEMENTS = frozenset(
    {"Cr", "Mn", "Fe", "Co", "Ni", "Cu", "Mo", "Ru", "Rh", "Gd"}
)

#: Elements that commonly need spin-orbit coupling enabled.
_SOC_RECOMMENDED = frozenset(
    {"Hf", "Ta", "W", "Re", "Os", "Ir", "Pt", "Au", "Hg", "Bi"}
)


def quality_warnings(
    atoms: Atoms, *, dft_settings: dict[str, Any] | None = None
) -> list[str]:
    """Return a list of short warning strings."""
    warnings: list[str] = []
    dft_settings = dft_settings or {}

    if len(atoms) == 0:
        warnings.append("structure has zero atoms")
        return warnings

    dim = detect_dimensionality(atoms)
    species = set(atoms.get_chemical_symbols())

    if dim == "slab":
        warnings.extend(_slab_warnings(atoms, dft_settings))

    if species & _MAGNETIC_ELEMENTS:
        scheme = dft_settings.get("magnetic_init", {}).get("scheme", "auto")
        if scheme == "nm":
            warnings.append(
                "magnetic elements present (Ni/Co/Fe/...) but magnetic_init.scheme='nm' — "
                "consider 'auto' or 'fm'"
            )

    if species & _SOC_RECOMMENDED:
        if not dft_settings.get("soc", False):
            soc_elements = sorted(species & _SOC_RECOMMENDED)
            warnings.append(
                f"5d/heavy elements present ({', '.join(soc_elements)}) — "
                "consider soc=True for accurate d-band centre"
            )

    # Placeholder for an xc-vs-adsorbate check (PBE/RPBE/BEEF-vdW
    # notes); currently silent because the implementation requires
    # the adsorbate detection from sites.py and we don't want to
    # double-import here.

    return warnings


def _slab_warnings(atoms: Atoms, dft_settings: dict[str, Any]) -> list[str]:
    """Slab-specific quality warnings."""
    warnings: list[str] = []
    cell_lengths = atoms.cell.lengths()
    import numpy as _np

    axis = int(_np.argmax(cell_lengths))
    from .header import _vacuum_along_axis

    vacuum = _vacuum_along_axis(atoms, axis)
    if vacuum < _VACUUM_FLOOR:
        warnings.append(
            f"vacuum gap {vacuum:.1f} Å along axis {axis} is below the {_VACUUM_FLOOR} Å floor — "
            "double-layer interactions across the periodic image are likely"
        )

    layers = detect_slab_layers(atoms, axis)
    if len(layers) < _LAYER_FLOOR:
        warnings.append(
            f"slab has only {len(layers)} layers (floor {_LAYER_FLOOR}) — "
            "surface and bottom may not be electronically decoupled"
        )

    if not dft_settings.get("dipole_correction", True):
        warnings.append(
            "dipole_correction is off — asymmetric slabs (adsorbates on one face only) "
            "develop a spurious field across the vacuum"
        )

    return warnings


__all__ = ["quality_warnings"]
