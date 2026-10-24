"""gr514051 — ``from hexfold import build`` resolves the verb callable.

Each verb name also names its submodule (``hexfold.build`` is
``build.py``), so the re-export is a PEP 562 ``__getattr__``: that keeps
``import hexfold.build as m`` binding the module for the callers that
read module attributes off it (``test_geom_clash`` uses ``hb._volterra``
/ ``hb._cut_jump`` / ``hb._laplace_solve``; ``test_seam_k3`` does
``import hexfold.stick as stick``).  The price is import-order
dependence — once ``hexfold.build`` is imported, ``from hexfold import
build`` binds the module, not the function — so the ``from hexfold
import`` half is asserted in a fresh subprocess.
"""

from __future__ import annotations

import subprocess
import sys
import types

import pytest


def test_from_hexfold_import_verbs_in_a_fresh_process() -> None:
    code = (
        "from hexfold import build, canonical_json, check, stick\n"
        "assert all(callable(f) for f in (build, check, stick, canonical_json))\n"
        "net = build('hexfold 0.2\\na: tube(5,5,len=4)\\n')\n"
        "assert net.report.ok\n"
        "assert canonical_json(net)\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert proc.returncode == 0, proc.stderr


def test_submodule_form_still_binds_modules() -> None:
    import hexfold.build as hb
    import hexfold.canon as hcanon
    import hexfold.check as hc
    import hexfold.stick as hs

    for mod in (hb, hc, hs, hcanon):
        assert isinstance(mod, types.ModuleType)
    assert callable(hb.build) and callable(hc.check)
    assert callable(hs.stick) and callable(hcanon.canonical_json)


def test_lazy_getattr_refuses_unknown_names() -> None:
    import hexfold

    with pytest.raises(AttributeError, match="has no attribute"):
        hexfold.__getattr__("nosuchverb")
