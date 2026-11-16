"""Figure rendering — execute a chunk's render recipe and capture an image.

A `figure` chunk carries author-supplied Python in
``meta.render``; this package runs it and returns the rendered PNG. The
security model is **phased isolation**: :mod:`precis.render.sandbox`
is the phase-1 form — a stripped subprocess (scrubbed env, rlimits, wall-clock
timeout, throwaway CWD), run on a single render lane, never in-process on the
credential-bearing worker. The phase-2 Docker jail is a refinement of the same
seam. The prohibited form is in-process ``exec`` on the every-node worker.

Stored pathway recipes use the trusted server renderer instead: the recipe
contains only validated selection options, never executable code. They read
stored kinetics without solving; source metadata and recipe hashes invalidate
the PNG and its frozen data package together. Explicit web refresh dispatches
through this same recipe so graph/profile and kinetics choices survive.
"""

from precis.render.sandbox import RenderResult, render_python

__all__ = ["RenderResult", "render_python"]
