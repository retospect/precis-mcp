"""``precis_geom`` — the script-side geometry core of a structure workspace.

The ``precis-geom`` wheel (``packaging/geom``) is what a workspace script
imports inside the ``--network none`` sandbox: ``hexfold`` (the atomic
builder, ``sheetnet`` after multicell slice 1), ``precis.cad`` /
``precis.structsolve`` / ``precis.structure`` (the macro side), and this
package — the pieces that are *the script's own surface*:

- :mod:`precis_geom.trace` — the construction trace: ``@traced`` on every
  public primitive pushes a frame; ``precis_ws.out`` dumps it to
  ``out/trace.json`` (``precis_geom.trace_schema`` is the wire format both
  the writer and the importer share).
- ``precis_geom.assembly`` (``pga``) — the one assembly implementation
  scripts and MCP ops share (slice 2a).

Stdlib only here: anything heavier lives in the wheel's other packages.
Import alias convention (spec §Core design): ``import precis_geom as pg``.
"""

__version__ = "0.1.0"
