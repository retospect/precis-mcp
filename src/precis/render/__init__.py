"""Figure rendering — execute a chunk's render recipe and capture an image.

A `figure` chunk (ADR 0035 §2) carries author-supplied Python in
``meta.render``; this package runs it and returns the rendered PNG. The
security model is **phased isolation** (ADR 0035 §3): :mod:`precis.render.sandbox`
is the phase-1 form — a stripped subprocess (scrubbed env, rlimits, wall-clock
timeout, throwaway CWD), run on a single render lane, never in-process on the
credential-bearing worker. The phase-2 Docker jail is a refinement of the same
seam. The prohibited form is in-process ``exec`` on the every-node worker.
"""

from precis.render.sandbox import RenderResult, render_python

__all__ = ["RenderResult", "render_python"]
