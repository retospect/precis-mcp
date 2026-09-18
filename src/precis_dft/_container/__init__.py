"""In-container entrypoints for the ``precis-dft`` image.

These modules run *inside* the container on the compute node — they
import GPAW / heavy science stacks that aren't present in the normal
dev / test environment, so every such import is **lazy** (inside the
function that needs it). Module import itself stays safe so the rest
of the package (and `test_imports`) can load these without GPAW.

The host side that invokes them lives in
:mod:`precis_dft.jobs.gpaw_relax` (stage → ssh+docker → parse).
"""
