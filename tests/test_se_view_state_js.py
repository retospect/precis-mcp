"""Client-JS coverage for the se viewer's shared view state
(``static/se-view-state.js``, gr477845 / gr458084): one per-object eye state
every renderer consults and that survives a revision step, and the
latest-request-wins gate behind the revision scrubber.

``scripts/se_view_state_smoke.mjs`` does the asserting; this wrapper runs it
and skips when ``node`` is absent (as ``test_topology_cloud_js.py``). The
source pins below hold the wiring without a browser: renderers consult the
shared state, and the scrubber no longer drops steps while one is loading.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_STATIC = _REPO / "src" / "precis_web" / "static"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
@pytest.mark.skipif(sys.platform == "win32", reason="node ESM path quirk on Windows")
def test_shared_view_state_smoke() -> None:
    result = subprocess.run(
        ["node", str(_REPO / "scripts" / "se_view_state_smoke.mjs")],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"


def _read(name: str) -> str:
    return (_STATIC / name).read_text(encoding="utf-8")


def test_every_overlay_mesh_kind_consults_the_shared_eye_state() -> None:
    core = _read("molecule-core.js")
    assert "shown && t < 0.999" in core  # atoms and bonds
    assert "shown && t > 0.001" in core  # smoothed surface
    assert "objectShown(mesh.userData.uid)" in core  # target surface
    assert "refreshVisibility" in core
    host = _read("molecule-host.js")
    assert "visibility.shapeShown(path)" in host  # caged envelope


def test_scene_render_restores_eyes_and_loads_latest_only() -> None:
    js = _read("blocktree-3d.js")
    assert "visibility.restore(viewer);" in js
    assert "visibility.update(change.states.new)" in js
    assert "sceneLatest.begin()" in js
    assert "if (req.isStale()) return;" in js
    # a revision step during a load supersedes it instead of being dropped
    assert "if (reloading || !Number.isFinite(rev)" not in js
    # the atom payload rides with the scene: one draw, not envelopes then atoms
    assert "renderScene(shapes, { camera, refit: false, atomicPayload })" in js
