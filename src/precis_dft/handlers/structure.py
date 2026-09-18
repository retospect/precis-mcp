"""``kind='structure'`` — frozen, content-addressed atomic structure.

Stores the canonical POSCAR in ``meta.poscar`` and a small summary
(``meta.n_atoms``, ``meta.formula``, ``meta.composition``,
``meta.dimensionality``). The id (``structure:<sha>``) is the
content address of the POSCAR; identical structures dedupe on put.

Implementation is a thin shim over :mod:`precis_dft.workbench` —
the workbench owns the science; this module owns the store
plumbing.
"""

from __future__ import annotations

from typing import Any

from precis.protocol import Handler, KindSpec
from precis.response import Response
from precis_dft import workbench

#: ``meta.poscar`` is the canonical POSCAR text. Always present on
#: ``kind='structure'`` refs; the views are recomputed on get from
#: this single source of truth.
_META_POSCAR = "poscar"


class StructureHandler(Handler):
    spec = KindSpec(
        kind="structure",
        title="Atomic structure",
        description=(
            "Frozen, content-addressed atomic structure (bulk, slab, cluster, "
            "with or without adsorbates). Carries the canonical POSCAR plus "
            "cached annotation views: header, sites, graph, special_sites, "
            "symmetry, ASCII renderings."
        ),
        supports_get=True,
        supports_search=True,
        supports_put=True,
        supports_tag=True,
        supports_link=True,
        supports_search_hits=True,
        is_numeric=False,
        id_required=True,
        views=(
            "toc",
            "header",
            "sites",
            "graph",
            "special_sites",
            "symmetry",
            "ascii_top",
            "ascii_side",
            "neighborhood",
            "compare",
            "path",
            "interstitials",
        ),
    )

    def __init__(self, *, hub: Any) -> None:
        self.hub = hub

    # ── put ──────────────────────────────────────────────────────

    def put(self, **kw: Any) -> Response:
        """Register a frozen structure.

        Accepts:
        - ``poscar=<str>`` — canonical POSCAR text (preferred)

        Returns a Response carrying the structure's id. Two
        identical POSCARs produce the same id and the existing ref
        is returned without re-inserting.
        """
        poscar = kw.get("poscar") or kw.get("text")
        if not poscar:
            return Response(
                body=("structure put requires poscar=<text> (canonical POSCAR string)")
            )

        try:
            frozen = workbench.register_structure(poscar)
        except Exception as exc:
            return Response(body=f"failed to parse POSCAR: {exc!r}")

        store = self._store()
        existing = store.fetch_ref_by_slug("structure", frozen.id)
        if existing is not None:
            return Response(
                body=(
                    f"structure already exists: id={frozen.id} "
                    f"(formula={frozen.formula}, n_atoms={frozen.n_atoms})"
                )
            )

        ref = store.insert_ref(
            kind="structure",
            slug=frozen.id,
            title=frozen.formula,
            meta={
                _META_POSCAR: frozen.poscar,
                "sha": frozen.sha,
                "n_atoms": frozen.n_atoms,
                "formula": frozen.formula,
                "composition": frozen.composition,
                "dimensionality": frozen.dimensionality,
            },
        )
        return Response(
            body=(
                f"created structure id={frozen.id} "
                f"(formula={frozen.formula}, n_atoms={frozen.n_atoms}, "
                f"ref_id={ref.ref_id})"
            )
        )

    # ── get ──────────────────────────────────────────────────────

    def get(self, **kw: Any) -> Response:
        """Read a structure + optionally compute views.

        ``id=`` must be ``structure:<sha>``. ``view=`` picks a view
        name (defaults to a compact header + composition summary).
        ``view='toc'`` returns the LLM's table of contents.
        """
        id_ = kw.get("id")
        if not id_:
            return Response(body="structure get requires id='structure:<sha>'")
        store = self._store()
        ref = store.fetch_ref_by_slug("structure", id_)
        if ref is None:
            return Response(body=f"structure not found: {id_}")
        poscar = ref.meta.get(_META_POSCAR, "")

        view = kw.get("view")
        if view in (None, "summary"):
            return Response(
                body=_format_summary(ref.meta, id_),
            )
        if view == "toc":
            return Response(body=str(workbench.view_toc(poscar)))
        if view == "header":
            views = workbench.views_for(poscar)
            return Response(body=str(views["header"]))
        if view in (
            "sites",
            "graph",
            "special_sites",
            "symmetry",
            "ascii_top",
            "ascii_side",
        ):
            views = workbench.views_for(poscar)
            return Response(body=str(views[view]))
        if view == "poscar":
            return Response(body=poscar)
        return Response(body=f"unknown view {view!r} for kind='structure'")

    # ── tag / link / search ──────────────────────────────────────

    def search(self, **kw: Any) -> Response:
        raise NotImplementedError(
            "StructureHandler.search — wiring not landed; use the "
            "store's search_refs_lexical directly for now"
        )

    def tag(self, **kw: Any) -> Response:
        raise NotImplementedError(
            "StructureHandler.tag — handled by NumericRefHandler in a future refactor"
        )

    def link(self, **kw: Any) -> Response:
        raise NotImplementedError(
            "StructureHandler.link — handled by NumericRefHandler in a future refactor"
        )

    # ── helpers ──────────────────────────────────────────────────

    def _store(self) -> Any:
        """Return the store. Hub-derived in production; tests
        attach a mock store directly on the handler."""
        if self.hub is not None and hasattr(self.hub, "store"):
            return self.hub.store
        # Test-only: handler instantiated with hub=None and ``.store``
        # attribute set directly.
        return self.store  # type: ignore[attr-defined]


def _format_summary(meta: dict[str, Any], id_: str) -> str:
    """One-screen text summary of a structure."""
    return (
        f"# {id_}\n"
        f"formula:        {meta.get('formula', '?')}\n"
        f"n_atoms:        {meta.get('n_atoms', '?')}\n"
        f"composition:    {meta.get('composition', '?')}\n"
        f"dimensionality: {meta.get('dimensionality', '?')}\n"
    )
