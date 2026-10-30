"""Module-level child targets for ``precis.workers.child_compute`` and the
``hexfold_relax`` dispatch tests. ``spawn`` pickles a target by reference,
so a closure or a monkeypatch inside a test never reaches the child; a
stub the child needs (an EMT stand-in for tblite) is installed here, in the
child, before the real compute runs."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import types
from typing import Any, cast

from precis.workers.child_compute import emit


def _sleep() -> None:
    while True:
        time.sleep(0.05)


def add(a: int, b: int) -> int:
    return a + b


def raise_value_error() -> None:
    raise ValueError("boom-child-compute")


def exit_hard() -> None:
    os._exit(3)


def sleep_forever(*_args: Any) -> None:
    _sleep()


def ignore_sigterm_and_sleep() -> None:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    emit("ready", os.getpid())
    _sleep()


def spawn_grandchild_and_sleep() -> None:
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    emit("grandchild", proc.pid)
    _sleep()


def relax_child_emt_xtb(*args: Any) -> Any:
    """The real ``hexfold_relax`` child compute with tblite stubbed by EMT
    (the wheel is absent on the Mac), the same stub the in-process tests
    used."""
    from ase.calculators.emt import EMT

    from precis_se import hexfold_relax_job

    fake = types.ModuleType("tblite.ase")
    setattr(fake, "TBLite", lambda **kw: EMT())  # ModuleType has no attr
    sys.modules["tblite.ase"] = fake
    return hexfold_relax_job._relax_child(*args)


def relax_child_no_tblite(*args: Any) -> Any:
    """The real child compute on a host without tblite."""
    from precis_se import hexfold_relax_job

    cast(Any, sys.modules)["tblite.ase"] = None
    return hexfold_relax_job._relax_child(*args)


def emit_checkpoint_then_sleep(*args: Any) -> Any:
    """Write one mid-polish checkpoint FILE (gr508493 round 2), then
    never return — exercises the checkpoint→version write-back."""
    from precis_se import hexfold_relax_job

    scene = args[0]
    ckpt = args[5]
    writer = hexfold_relax_job._CkptWriter(
        ckpt["dir"],
        job_id=int(ckpt["job_id"]),
        base_version=int(ckpt["base_version"]),
    )
    writer.write(
        {
            "v": 1,
            "stage": "polish",
            "fragment": None,
            "stage_steps": 7,
            "curve": [0.9, 0.8, 0.7, 0.6, 0.55, 0.52, 0.51],
            "positions": {
                la: [round(float(x), 6) for x in a.frac]
                for la, a in scene.atoms.items()
            },
            "polish": None,
            "fragments": [],
            "before": [],
        }
    )
    _sleep()
