"""Boot-path regression for gr345269: constructing :class:`PathwayHandler`
must never pull matplotlib into ``sys.modules``.

``PathwayHandler.__init__`` used to guard the autocatpath dependency by
doing ``from . import runner`` — but ``precis_pathway.runner`` module-level
imports ``autocatpath.pipeline``, which in turn module-level imports
``render``/``viz`` for matplotlib figure export. That forced a ~2.5s
font-cache regeneration on every fresh container's serve handshake just to
answer "is autocatpath installed". The fix probes the base ``autocatpath``
package directly (ase/rdkit/networkx are unconditional deps of it, so a
clean bare import already proves the whole catalyst extra is present); the
real ``runner`` import — and matplotlib with it — still happens lazily on
first ``put()``.
"""

from __future__ import annotations

import sys

import pytest

from precis.dispatch import Hub
from precis.store import Store


def test_construct_does_not_import_matplotlib(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The probe under test is "is autocatpath installed"; without the
    # catalyst extra (the CI lint/test lanes run --no-extra catalyst) the
    # handler refuses to construct and there is no boot path to measure.
    pytest.importorskip("autocatpath")
    # Purge every module the boot-time chain could re-pull, plus any prior
    # matplotlib import from an earlier test in this worker — otherwise a
    # regression would hide behind "already imported by someone else".
    for name in list(sys.modules):
        if name == "matplotlib" or name.startswith("matplotlib."):
            monkeypatch.delitem(sys.modules, name, raising=False)
    for name in (
        "precis_pathway.handler",
        "precis_pathway.runner",
        "autocatpath",
        "autocatpath.pipeline",
        "autocatpath.render",
        "autocatpath.viz",
    ):
        monkeypatch.delitem(sys.modules, name, raising=False)

    import precis_pathway.handler as handler_mod

    handler_mod.PathwayHandler(hub=Hub(store=store))

    leaked = sorted(
        m for m in sys.modules if m == "matplotlib" or m.startswith("matplotlib.")
    )
    assert leaked == [], f"PathwayHandler() init pulled in matplotlib: {leaked}"
