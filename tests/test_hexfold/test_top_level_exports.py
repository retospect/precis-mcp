"""``from hexfold import build`` resolves the verb — the callable module.

The verb submodules (``hexfold.build``, ``hexfold.check``,
``hexfold.stick``) are :class:`hexfold._VerbModule` instances: the module
is the callable, so ``import hexfold.build as m`` keeps binding the
module for callers that read (and monkeypatch) module attributes off it
(``test_geom_clash`` uses ``hb._volterra`` / ``hb._cut_jump`` /
``hb._laplace_solve``; ``test_seam_k3`` does ``import hexfold.stick as
stick``) *and* ``from hexfold import build`` binds the same object —
no import order can rebind the name to a plain module (gr514180).
"""

from __future__ import annotations

import subprocess
import sys
import types

import pytest


def test_from_hexfold_import_verbs_in_a_fresh_process() -> None:
    code = (
        "import sys\n"
        "from hexfold import build, canonical_json, check, stick\n"
        "assert all(callable(f) for f in (build, check, stick, canonical_json))\n"
        "assert build is sys.modules['hexfold.build']\n"
        "assert stick is sys.modules['hexfold.stick']\n"
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


def test_import_order_cannot_rebind_the_verbs() -> None:
    """gr514180: a plain ``import hexfold.<verb>`` followed by ``from
    hexfold import <verb>`` binds the same object as the reversed order —
    the (callable) submodule, in both cases."""
    import hexfold
    import hexfold.build  # noqa: F401 — binds the submodule attribute
    import hexfold.stick  # noqa: F401
    from hexfold import build, check, stick

    for name, verb in (("build", build), ("check", check), ("stick", stick)):
        module = sys.modules[f"hexfold.{name}"]
        assert verb is module
        assert callable(verb)
        assert getattr(hexfold, name) is module


def test_lazy_getattr_refuses_unknown_names() -> None:
    import hexfold

    with pytest.raises(AttributeError, match="has no attribute"):
        hexfold.__getattr__("nosuchverb")
