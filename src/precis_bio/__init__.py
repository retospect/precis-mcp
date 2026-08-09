"""precis-bio — the protein / structure-prediction tool-pack (ADR 0056).

The bio sibling of ``precis_chem``: a first-party **plugin** on the precis
substrate (ADR 0056, slice 4 — kind = legible IR, job_type = the heavy
engine on the ADR 0044 compute lane). It snaps in through the three plugin entry-point groups
(``precis.handlers`` / ``precis.job_types`` / ``precis.migrations``) declared
in the precis-mcp ``pyproject.toml``, so ``dispatch.py`` and the core kind
catalogue stay untouched. It rides the two seams shipped for exactly this
(``KindSpec.can_own_jobs`` + the derived-job ``requested`` relation).

Slice 4 (this package) is the **structure-prediction `protein` kind** + a
``fold`` job that predicts a structure from a sequence. It ships **dark**
behind ``PRECIS_BIO_ENABLED`` (the ``protein`` kind's ``requires_env``) so the
merge is inert until the flag is set. The heavy predictor (AlphaFold3) runs in
a **container** on the GPU fold node — jax/CUDA + the model weights live in the
image + a mounted models dir, never on the always-on workers; a deterministic
in-process ``stub`` engine proves the compute-lane round-trip + the
content-addressed cache without a GPU or the image (grounded on the real
AlphaFold3 v3.0.1 install on spark — memory: alphafold-spark-facts).

See ADR 0056 for the canonical-kind decision and the transport split
(reused from ``precis_chem``); container plumbing details live in
:mod:`precis_bio.alphafold`. Later slices (deferred): ``structure``
convergence via ``Scene.from_ase`` (ADR 0043) and a ColabFold MSA-mode
engine; a ``sequence`` design kind (ProteinMPNN/RFdiffusion) is the
next sibling.
"""

from __future__ import annotations

from precis_bio.protein import ProteinHandler

__all__ = ["ProteinHandler"]
