"""precis-estimate — the millisecond chemistry-workup panel (the `estimate`
kind).

The catalyst sibling of ``precis_pathway``: a first-party **plugin** on the
precis substrate, snapping in through the ``precis.handlers`` /
``precis.handle_codes`` entry-point groups declared in the precis-mcp
``pyproject.toml`` (ADR: `docs/backlog/estimate-kind-ms-chemistry-workup.md`).

**What it is.** An `estimate` ref is a citable, cache-backed semi-empirical
workup — undergrad-ish non-ML chemistry (element-property lookups, tight-
binding/Newns-Anderson-style d-band arithmetic, Hume-Rothery-style alloying
heuristics) computed in milliseconds, *in order to set up* the slow stuff
(MLIP relax, NEB, QE/VASP DFT). It exists so a quest agent arguing mechanism
("d-band shift", "strain-dominated alloying") has an actual in-system
observable to cite instead of an unfalsifiable inference from element
identity + energies.

**The tier model.** Slice 1 (this package, today) ships the **composition
tier**: `get(kind='estimate', q='Pd Zr H')` needs no geometry at all — it's
pure element-property lookup + pairwise alloying heuristics
(`compute/composition.py`). Later slices layer on a **structure tier** (full
workup of a held `structure` ref — coordination, strain, adsorbate height,
d-band via a vendored extended-Hückel table), an **ops what-if** tier
(mutate a structure in-call, reusing the quest candidate-ops vocabulary),
and a **compare** view (doped-vs-pristine delta — the core argument form).
The seams for those live in this handler's `views=` gap and the (currently
empty) `compute/` package; slice 1 does not build them.

**Epistemic grade — read before citing.** Every `estimate` row is a
*hypothesis-generator*, **inadmissible for rulings**. The ladder: estimate
(ms) → MLIP sim (min) → QE autopsy (h) → literature. An `[es…]` cite is
visibly estimate-branded so a reader downstream never mistakes a d-band
heuristic for a measured barrier. Validate the semi-empirical layer against
knowns the campaign already measured (Au-vs-Pt d-band ordering, the d¹⁰
weak-interaction pattern) before trusting it in an argument — that
validation is itself a citable finding, not an assumption.

Results cache by ``hash(canonicalised composition)`` (deterministic — same
composition, same panel, pinned TTL) and mint an `es` universal handle
(`handles.py`) so a panel row is directly citable in a dossier and
link-able `derived-from → st…`.

**Deps are optional, on purpose.** `mendeleev` / `pymatgen` / `tblite`
(the `[estimate]` extra) are not installed in every venv — a fresh prod
deploy needs the extra explicitly. `handler.py` never imports them at
module scope; the import happens inside `_fetch`, so an `[estimate]`-less
venv boots every other kind cleanly and `get(kind='estimate', ...)` fails
with a clean "install the extra" error instead of taking the whole
handler-registry load down.
"""

from __future__ import annotations

from precis_estimate.handler import EstimateHandler

__all__ = ["EstimateHandler"]
