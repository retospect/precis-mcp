"""A stale vendored hexfold must not take down non-cad importers (gr524633).

``hexfold.wrap`` is newer than some deployed images: ``precis.cad`` is on
the store import path (``precis.store._cad_ops`` pulls in
``hmap``/``primitives``/``mold``), so ``precis ingest``/``watch`` and
``precis worker`` crash-looped at import with
``missing dependency 'hexfold.wrap'`` although their work never touches
the wrap mapping.

The cad modules now bind ``WrapSpec``/``seam_error`` through the
``precis.cad._hexfold`` fail-soft shim. These tests run a fresh
interpreter with ``sys.modules["hexfold.wrap"] = None`` — the same shape
the import system sees when the installed hexfold never shipped the
module — and assert the watch and worker entry modules still import,
while the cad call refuses at call time naming the fix. The subprocess,
not in-process monkeypatching, keeps the eviction honest: anything that
still imports ``hexfold.wrap`` eagerly dies loudly in the child.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

#: A stale hexfold, for a fresh interpreter: ``import hexfold.wrap`` and
#: ``from hexfold import wrap`` both halt on the ``None`` sentinel, which
#: is exactly what ``importlib`` raises ``ImportError`` for.
_MASK = 'import sys; sys.modules["hexfold.wrap"] = None'


def _fresh(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=120,
    )


def test_watch_and_worker_import_without_hexfold_wrap() -> None:
    """The crash-loop repro: the two container entry modules must import
    on a hexfold that predates ``hexfold.wrap``."""
    run = _fresh(
        f"""
        {_MASK}
        import precis.cli.watch
        import precis.cli.worker
        import precis.store  # the cad path enters through _cad_ops
        print("IMPORTS-OK")
        """
    )
    assert run.returncode == 0, run.stderr
    assert "IMPORTS-OK" in run.stdout
    assert "hexfold" not in run.stderr


def test_cad_calls_refuse_with_rebuild_hint() -> None:
    """Same environment, cad side: every name the shim re-exports refuses
    at call time — ``Unsupported``, not an import traceback — and the
    message names the fix."""
    run = _fresh(
        f"""
        {_MASK}
        import numpy as np
        from precis.cad import hmap, mold, primitives

        for call in (
            lambda: hmap.seam_error(np.ones((4, 3))),
            lambda: hmap.WrapSpec(1).theta(0.0, 0.0),
            lambda: hmap.HeightMap(np.ones((4, 3))),
            lambda: hmap.check_seam(np.ones((4, 3))),
            lambda: mold.WrapSpec(1).theta(0.0, 0.0),
            lambda: primitives.WrapSpec(1).theta(0.0, 0.0),
        ):
            try:
                call()
            except Exception as exc:
                print(f"{{type(exc).__name__}}: {{exc}}")
            else:
                print("NO-REFUSAL")
        """
    )
    assert run.returncode == 0, run.stderr
    refusals = run.stdout.strip().splitlines()
    assert len(refusals) == 6
    for line in refusals:
        assert line.startswith("Unsupported: ")
        assert "hexfold too old or missing" in line
        assert "uv sync" in line


def test_healthy_env_binds_the_real_names() -> None:
    """No stub drift: with ``hexfold.wrap`` present the cad modules share
    its objects — ``precis.cad.hmap.seam_error`` stays the re-export
    ``tests/test_wrap.py`` pins."""
    from hexfold.wrap import WrapSpec, seam_error
    from precis.cad import hmap, mold, primitives

    assert hmap.seam_error is seam_error
    assert hmap.WrapSpec is WrapSpec
    assert mold.WrapSpec is WrapSpec
    assert primitives.WrapSpec is WrapSpec
