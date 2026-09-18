"""precis-dft — the DFT-for-catalysis code, folded into this monorepo.

**Why it lives here.** This was a standalone repo
(``~/work/projects/code/precis-dft``, MIT, *no git remote at all* — one
unbacked disk copy). precis-mcp never imported it and never declared it as a
dependency, yet its ``_container/`` subset is baked into the ``precis-dft:cpu``
image that every ``gpaw`` rung of ``struct_relax`` executes, built from that
unbacked checkout. Folding it in makes the image source deployable from the
same tree as the host side that invokes it; see ``docker/precis-dft/``.

The standalone checkout is kept as an archive and is **not** the source of
truth any more — do not build from it.

**What was dropped on the way in**, as superseded by precis-mcp's own job
lane: ``job_types/``, ``campaign/``, ``retry/``, ``workers/``, ``migrations/``,
``jobs/gpaw_relax.py``, and the tests that covered them. The dispatch,
retry, node-pinning and campaign logic all have precis-mcp equivalents
(``precis.workers.job_types.struct_relax``, ``precis.workers.retry``); keeping
a second copy would mean two lanes drifting apart.

**What is live.** ``_container/`` (``cli`` + ``gpaw_relax``) and
``structures/`` are what the compute image runs. ``ops/``, ``annotator/``,
``reactions/``, ``structures/`` and ``data/`` are imported by nothing in
precis-mcp yet and exist as the library half — pure functions with their own
tests under ``tests/precis_dft/``. ``handlers/`` and ``_test_store.py`` are
inert: registered nowhere, kept because they type-check clean and keep the
volcano-plot tests alive.

**Licensing.** This subtree is MIT (``LICENSE-MIT`` alongside this file);
precis-mcp is GPL-3.0-or-later. MIT into GPL is compatible — the combined work
is GPL — and the MIT notice must travel with these files.

**Re-spinning it out.** Nothing here imports precis-mcp, so the subtree is
self-contained: ``git subtree split -P src/precis_dft`` (plus
``tests/precis_dft`` and ``docker/precis-dft``) reconstitutes a standalone
repo. The container's own build metadata already exists separately at
``docker/precis-dft/pyproject.toml``.

Not to be confused with **catpath** (PyPI ``autocatpath``, GPLv3): that is the
published MLIP-only pathway engine and is deliberately precis-free. GPAW and
anything precis-shaped do not belong there.
"""

__version__ = "0.1.0"
