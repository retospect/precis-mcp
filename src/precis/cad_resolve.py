"""Store-backed slug → :class:`~precis.cad.scene.SceneSpec` resolver.

:mod:`precis.cad` is the analytic kernel and deliberately imports nothing
from the DB, so its instance expander (``use <slug> as <name>``) takes an
*injected* resolver. This module is the one production implementation, and
lives outside ``precis.cad`` precisely so that boundary holds.

It is deliberately a plain ``Store`` lookup rather than
``resolve_live_slug_ref``: the kernel raises
:class:`~precis.cad.scene.SceneError`, which every caller already funnels
into its own ``BadInput``/dry-run reporting, and importing a handler
private from here would tangle the handler package into the web and worker
import graphs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from precis.cad.scene import Resolver, SceneError, SceneSpec

if TYPE_CHECKING:  # pragma: no cover - typing only
    from precis.store import Store


def design_resolver(store: Store) -> Resolver:
    """A resolver bound to ``store`` for :func:`~precis.cad.scene.expand_instances`.

    A missing *or retired* design is a hard :class:`SceneError`
    (``Store.get_ref`` already filters soft-deleted rows): a sub-assembly
    that silently expanded to nothing would be the worst available failure
    mode — the design would still build, just without the part.
    """

    def _resolve(slug: str) -> SceneSpec:
        ref = store.get_ref(kind="cad", id=slug)
        if ref is None:
            raise SceneError(
                f"instanced design {slug!r} not found (or retired) — "
                "put it before the design that uses it"
            )
        spec, _handles = store.cad_load(ref.id)
        return spec

    return _resolve
