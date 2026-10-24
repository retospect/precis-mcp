"""Package-class maximum body heights — pure data, mm, for the pcb→se
rough box (``docs/backlog/pcb-se-binding.md`` §Rough box v0).

The table and :func:`height_mm` now live in
:mod:`precis_geom.pcb_shape` — the one derivation the workspace
container also runs (pcb-workspace-code slice 0b); ``precis_geom`` is
stdlib-only, so this module is a re-export shim keeping the
``precis.pcb.package_heights`` path the spec named. The ordering rule
unchanged: an instance's own ``height_mm`` wins; absent, the table's
package-class maximum (``origin: proposed``, source noted) with the
table's largest value as the fallback — an over-tall keep-out is a
conservative clearance answer, an under-tall one a lie.
"""

from __future__ import annotations

from precis_geom.pcb_shape import HEIGHT_TABLE, height_mm

__all__ = ["HEIGHT_TABLE", "height_mm"]
