"""``kind='dft_calculation'`` — finished GPAW run record.

A calc record links to the structure it was computed on, carries
scalars (E_tot, max_force, converged, fermi, magmoms), settings
(functional, kpts, basis, cutoff), provenance (job_id, GPAW
version, PAW dataset hash), and a ``derived`` subkey populated by
the ``derive_*`` job_types.

For v1, the handler is minimal: it accepts ``put`` with the calc
fields wired into ``meta`` and exposes ``get`` for inspection.
The actual production path creates calc records via a post-success
hook on ``gpaw_relax`` / ``gpaw_scf`` jobs (Phase 0 PR 3 follow-up);
here we let the test harness write them directly.
"""

from __future__ import annotations

from typing import Any

from precis.protocol import Handler, KindSpec
from precis.response import Response


class DftCalculationHandler(Handler):
    spec = KindSpec(
        kind="dft_calculation",
        title="Finished DFT calculation",
        description=(
            "A completed GPAW run. Carries scalars, settings, provenance, "
            "and a derived subkey for E_ads / free energies / d-band / "
            "Bader / work function. Created by a post-success hook on the "
            "producing gpaw_relax / gpaw_scf job."
        ),
        supports_get=True,
        supports_search=True,
        supports_put=True,
        supports_edit=True,
        supports_tag=True,
        supports_link=True,
        supports_search_hits=True,
        is_numeric=False,
        id_required=True,
        views=("toc", "scalars", "derived", "settings", "provenance"),
    )

    def __init__(self, *, hub: Any) -> None:
        self.hub = hub

    def put(self, **kw: Any) -> Response:
        """Register a calc record.

        Required:
        - ``id='calc:<job_id>'`` — the slug.
        - ``structure='structure:<sha>'`` — the calculated structure.
        - ``scalars={...}`` — E_tot, max_force, converged, etc.

        Optional:
        - ``settings={...}`` — functional, kpts, basis, dispersion.
        - ``provenance={...}`` — job_id, GPAW version, PAW hash.
        - ``species={...}`` — adsorbed species id (for adsorption
          calculations; lets ``reaction_evaluate`` look up the right
          calc per species).
        """
        id_ = kw.get("id")
        if not id_ or not id_.startswith("calc:"):
            return Response(
                body=(
                    "dft_calculation put requires id='calc:<slug>' (e.g. "
                    "'calc:job_201' or 'calc:Pt111_OH')"
                )
            )
        structure = kw.get("structure")
        if not structure or not structure.startswith("structure:"):
            return Response(
                body=(
                    "dft_calculation put requires structure='structure:<sha>' "
                    "(the structure that was calculated)"
                )
            )
        scalars = kw.get("scalars")
        if not isinstance(scalars, dict):
            return Response(
                body="dft_calculation put requires scalars={...} with E_tot etc."
            )

        store = self._store()
        if store.fetch_ref_by_slug("dft_calculation", id_) is not None:
            return Response(body=f"calc already exists: {id_}")

        meta = {
            "structure": structure,
            "scalars": scalars,
            "settings": kw.get("settings", {}),
            "provenance": kw.get("provenance", {}),
            "species": kw.get("species"),
            "derived": kw.get("derived", {}),
        }
        ref = store.insert_ref(
            kind="dft_calculation",
            slug=id_,
            title=id_,
            meta=meta,
        )
        return Response(
            body=(
                f"created calc id={id_} on {structure} "
                f"(E_tot={scalars.get('E_tot', '?')}, ref_id={ref.ref_id})"
            )
        )

    def get(self, **kw: Any) -> Response:
        id_ = kw.get("id")
        if not id_:
            return Response(body="dft_calculation get requires id='calc:<slug>'")
        store = self._store()
        ref = store.fetch_ref_by_slug("dft_calculation", id_)
        if ref is None:
            return Response(body=f"calc not found: {id_}")

        view = kw.get("view")
        if view in (None, "summary", "toc"):
            return Response(body=_format_summary(ref.meta, id_))
        if view in ("scalars", "settings", "provenance", "derived"):
            return Response(body=str(ref.meta.get(view, {})))
        return Response(body=f"unknown view {view!r}")

    def edit(self, **kw: Any) -> Response:
        """Merge derived properties into the calc's ``meta.derived``.

        The ``derive_*`` job_types call this with ``derived={...}``;
        each top-level key (``eads`` / ``G`` / ``dband`` / ``bader`` /
        ``workfunction``) is itself usually a dict keyed by
        ``"<scheme>:<hash>"`` or a conditions hash, so we deep-merge
        one level: a new ``eads`` entry adds to the existing eads map
        rather than replacing it.
        """
        id_ = kw.get("id")
        if not id_:
            return Response(body="dft_calculation edit requires id='calc:<slug>'")
        store = self._store()
        ref = store.fetch_ref_by_slug("dft_calculation", id_)
        if ref is None:
            return Response(body=f"calc not found: {id_}")

        derived_update = kw.get("derived")
        if not isinstance(derived_update, dict) or not derived_update:
            return Response(body="dft_calculation edit requires derived={...}")

        derived = dict(ref.meta.get("derived", {}) or {})
        for key, value in derived_update.items():
            if isinstance(value, dict) and isinstance(derived.get(key), dict):
                merged = dict(derived[key])
                merged.update(value)
                derived[key] = merged
            else:
                derived[key] = value
        store.set_meta(ref.ref_id, derived=derived)
        return Response(body=f"updated calc {id_} derived: {sorted(derived_update)}")

    def search(self, **kw: Any) -> Response:
        raise NotImplementedError("dft_calculation search — wiring not landed")

    def tag(self, **kw: Any) -> Response:
        raise NotImplementedError("dft_calculation tag — wiring not landed")

    def link(self, **kw: Any) -> Response:
        raise NotImplementedError("dft_calculation link — wiring not landed")

    def _store(self) -> Any:
        if self.hub is not None and hasattr(self.hub, "store"):
            return self.hub.store
        return self.store  # type: ignore[attr-defined]


def _format_summary(meta: dict[str, Any], id_: str) -> str:
    scalars = meta.get("scalars", {})
    derived = meta.get("derived", {})
    return (
        f"# {id_}\n"
        f"structure: {meta.get('structure', '?')}\n"
        f"species:   {meta.get('species', '-')}\n"
        f"E_tot:     {scalars.get('E_tot', '?')}\n"
        f"converged: {scalars.get('converged', '?')}\n"
        f"max_force: {scalars.get('max_force', '?')}\n"
        f"derived keys: {sorted(derived)}\n"
    )
