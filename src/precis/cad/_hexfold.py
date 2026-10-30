"""Fail-soft binding of the ``hexfold.wrap`` names the wrap/relief kernel uses.

``precis.cad`` sits on every store import path — ``precis.store._cad_ops``
pulls in ``hmap``/``primitives``/``mold``, so anything that opens a
``Store`` (``precis ingest``/``watch``, ``precis worker``, ``serve``)
imports this package. A deployment whose vendored hexfold predates the
``hexfold.wrap`` module therefore used to crash-loop those processes at
import (``missing dependency 'hexfold.wrap'``, gr524633) although none
of their work touches the wrap mapping.

The cad modules bind ``WrapSpec``/``seam_error`` through here instead:
the real names when ``hexfold.wrap`` is importable, otherwise a stub
that refuses at call time with :class:`~precis.errors.Unsupported` —
"hexfold too old or missing … rebuild the image / uv sync". That refusal
is also the hard failure cad-facing entry points surface; the laziness
exists only so importers that never call the mapping keep working.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from precis.errors import Unsupported

if TYPE_CHECKING:
    from hexfold.wrap import WrapSpec, seam_error
else:
    try:
        from hexfold.wrap import WrapSpec, seam_error
    except ImportError:
        # A stale or absent vendored hexfold (gr524633): every name this
        # module re-exports refuses the same way, at the first call.
        def _stale(*args: Any, **kwargs: Any) -> Any:
            raise Unsupported(
                "hexfold too old or missing: the installed hexfold has no "
                "wrap module — cad wrap/relief features need hexfold.wrap",
                next="rebuild the image / uv sync against a hexfold that "
                "ships hexfold.wrap",
            )

        WrapSpec = _stale
        seam_error = _stale

__all__ = ["WrapSpec", "seam_error"]
