# Generalize the fisheye discovery affordance beyond draft chunk reads

The `→ view='fisheye'` footer exists only in `DraftHandler._render_chunk`;
paper/patent/web/datasheet/cfp/memory/finding chunk reads also have fisheye
eyes (`src/precis/utils/eye_render.py::render_eye`) but never advertise it —
an agent reading those kinds unprompted can't discover fisheye. Generalize
the teach-at-render affordance; optional: a session damper if it proves noisy
in read loops, and a one-line mention in the server-instructions string
(`src/precis/server.py`). Mechanical.

test: per-kind assertion that a plain single-chunk get carries the affordance
line (parallel to tests/test_draft_handler.py's).
