"""gr516063: ``tests._script.load_script`` must compile from source, not
go through the loader's ``__pycache__``. A test that rewrites a script
with the same byte size inside the same timestamp tick produces a pyc
whose (mtime-s, size) stamp still matches — SourceFileLoader then serves
the STALE bytecode. CI writes bytecode; the dev container doesn't, so
this only reddened the gate (a8d90e685).
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import py_compile
from pathlib import Path
from types import ModuleType

from tests._script import load_script

_OLD = "MARKER = 'aaa'\n"
_NEW = "MARKER = 'bbb'\n"
assert len(_OLD) == len(_NEW)


def _via_source_file_loader(name: str, path: Path) -> ModuleType:
    """The pre-fix load path, kept here as the control: it must return the
    STALE value, proving the planted pyc is genuinely loadable."""
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_load_script_reads_source_not_a_stale_pyc(tmp_path: Path) -> None:
    script = tmp_path / "worker"
    script.write_text(_OLD, encoding="utf-8")
    # Plant the pyc a same-(mtime, size) rewrite would wrongly validate.
    pyc = Path(importlib.util.cache_from_source(str(script)))
    py_compile.compile(str(script), cfile=str(pyc), doraise=True)
    stamp = script.stat()
    script.write_text(_NEW, encoding="utf-8")
    os.utime(script, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))

    control = _via_source_file_loader("worker_control", script)
    assert control.MARKER == "aaa", (
        "control: the planted pyc was not served — the stale-cache trap "
        "this test guards isn't armed on this platform"
    )

    mod = load_script("worker_fresh", script)
    assert mod.MARKER == "bbb"


def test_load_script_registers_for_dataclasses(tmp_path: Path) -> None:
    script = tmp_path / "with_dc"
    script.write_text(
        "from dataclasses import dataclass\n\n"
        "@dataclass(frozen=True, slots=True)\n"
        "class P:\n"
        "    x: int\n\n"
        "p = P(1)\n",
        encoding="utf-8",
    )
    mod = load_script("with_dc", script, register=True)
    assert mod.p.x == 1
