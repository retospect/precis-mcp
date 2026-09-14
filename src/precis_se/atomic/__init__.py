"""``se`` **atomic mode** — the molecular-machine domain layer.

The landing zone for the ``nm`` kind's domain code as it folds into ``se``
(docs/backlog/nm-se-merge.md). se and nm were built as siblings on the
symmetry ``se : cad :: nm : structure``; the units-policy cutover removed
the last substantive difference (both kinds store SI metres), leaving
duplicated scaffold and a which-kind-do-I-use decision point for every
agent. The merge keeps *one* design kind (``se``) whose ``atomic``
manufacturing mode (:mod:`precis_se.modes`) carries what ``nm`` owned:
blocks whose realized content is chemistry rather than solids, bound to
``structure`` designs at L3/L5.

This subpackage is the carve that keeps the merge from producing one
enormous file — the handlers are already ~77 KB (se) and ~85 KB (nm), so
nm's domain modules land here as submodules rather than inline:

- :mod:`precis_se.atomic.generators` — parametric block factories (the
  IC-design PCell: ``params → block``, the deterministic fill path).
  Å-native by design: generator math stays in ångström (the atomistic
  enclave convention, ``docs/backlog/structure-unit-enclave.md``), and
  the m↔Å crossing happens once, at the envelope's ingest boundary.
- :mod:`precis_se.atomic.mechanics` — the L4 closed-form mechanics
  ceilings (Euler buckling, rupture force, bend stiffness). Signatures
  keep Å/nN/eV deliberately: these are cost terms *over* the geometry,
  not lengths *in* it (nm-se-merge.md "Explicitly NOT in scope").
- :mod:`precis_se.atomic.vocab` — the L2 statements only an atomic block
  makes (declared dof, threading, the bond capability gate), vetted for
  :mod:`precis_se.ops` to apply.
- :mod:`precis_se.atomic.validate` — the read-time chemistry findings
  (bond capability, structure bindings, bond geometry sanity, the
  ``envelope_fit`` L1↔L5 agreement check).
- :mod:`precis_se.atomic.bind` / :mod:`precis_se.atomic.generate` — the
  **store-aware** ops (``bind_structure``/``unbind_structure`` and
  ``generate``'s prepare/finish pair), and
  :mod:`precis_se.atomic.apply` — the walker that intercepts them for
  ``put``/``edit``.
- :mod:`precis_se.atomic.render` — ``view='mechanics'``/
  ``view='literature'`` plus the atomic filled-fraction line.
- :mod:`precis_se.atomic.propose` — the ``se_propose_atomic`` job type
  (nm's one ``nm_propose`` ``JobTypeSpec``, renamed for its se home):
  a tool-less LLM call that PROPOSES one block's chemistry as a
  dry-run-validated ``job_result``, and applies nothing.

Everything here is pure except :mod:`~precis_se.atomic.bind`,
:mod:`~precis_se.atomic.generate`, :mod:`~precis_se.atomic.render` and
:mod:`~precis_se.atomic.propose`, which take a store explicitly (the
last one via its job ``ctx``) — the ``ops.py`` discipline holds for the
op table itself, and the three ops that genuinely need the store are
intercepted before it.
"""

from __future__ import annotations

__all__: list[str] = []
