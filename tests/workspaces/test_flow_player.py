"""End to end through the stand-in (se-flow-kernel slice 4a §End to end):
a workspace script builds the drilled hex chamber and states its
``evaluate`` outcome with no DB and no se, the run reaches the design
through the tar door, and the Flow tab plays the imported shape.

The stand-in is :func:`_run_through_tar_door`: a host run of the script
plus the real ``workspace_upload_link`` → ``POST /files/upload_tree`` →
``import_workspace`` chain, test code only (ruled 2026-11-04).
``tests/test_se_flow.py::test_flow_workspace_run_to_player`` goes red when
the real ``run`` lands, and this helper goes with it.
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

import precis_se
from precis_se import persist
from precis_se.flow import flow_pockets
from precis_se.handler import SeHandler
from precis_web.app import create_app
from precis_web.config import WebConfig
from tests._flow_hex import seed_flow_measurands
from tests.workspaces import flow_chamber

_SE_MIGRATIONS = Path(precis_se.__file__).parent / "migrations"
_SRC = Path(__file__).resolve().parents[2] / "src"

#: What the script step may not reach (structure-workspaces §Core design
#: 7): the server package, the DB driver, the store.
_RUNNER = """
import importlib.abc, runpy, sys

BLOCKED = ("precis_se", "psycopg", "psycopg_pool", "precis.store")

class Blocked(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if any(name == b or name.startswith(b + ".") for b in BLOCKED):
            raise ImportError(f"offline script imported {name}")
        return None

sys.meta_path.insert(0, Blocked())
script, out_dir = sys.argv[1:3]
sys.argv = [script, out_dir]
runpy.run_path(script, run_name="__main__")
loaded = sorted(
    m for m in sys.modules
    if any(m == b or m.startswith(b + ".") for b in BLOCKED)
)
assert not loaded, loaded
print("offline ok")
"""


def _run_script_offline(out_dir: Path) -> None:
    """The fixture ``main.py`` in a fresh interpreter: the blocked imports
    raise, and no database coordinate reaches its environment."""
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("PRECIS_", "PG", "DATABASE"))
    }
    env["PYTHONPATH"] = str(_SRC)
    proc = subprocess.run(
        [sys.executable, "-c", _RUNNER, flow_chamber.__file__, str(out_dir)],
        cwd=out_dir.parent,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=300,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "offline ok" in proc.stdout


def _tar_of(out_dir: Path) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for path in sorted(out_dir.rglob("*")):
            if path.is_file():
                tf.add(path, arcname=path.relative_to(out_dir).as_posix())
    return buf.getvalue()


def _run_through_tar_door(
    client: TestClient, se: SeHandler, design: str, out_dir: Path
) -> str:
    """The stand-in: the run's ``out/`` tree goes up as one tar through
    the signed upload link and imports by the sha the POST answered.
    Returns the import echo."""
    minted = se.edit(id=design, ops=[{"op": "workspace_upload_link"}]).body
    match = re.search(r"/files/upload_tree/\S+", minted)
    assert match is not None, minted
    posted = client.post(
        match.group(0).rstrip("'"),
        content=_tar_of(out_dir),
        headers={"content-type": "application/x-tar"},
    )
    assert posted.status_code == 200, posted.text
    return se.edit(
        id=design,
        ops=[
            {
                "op": "import_workspace",
                "workspace": "flow-player",
                "staged": posted.json()["sha256"],
            }
        ],
    ).body


@pytest.fixture
def flow_client(
    store: Any, runtime_with_store: Any, tmp_path: Path, monkeypatch: Any
) -> TestClient:
    staging = tmp_path / "staging"
    staging.mkdir()
    monkeypatch.delenv("PRECIS_WATCH_INBOX", raising=False)
    monkeypatch.setenv("PRECIS_UPLOAD_STAGING", str(staging))
    monkeypatch.setenv("PRECIS_FILE_LINK_KEY", "flow-player-test-key")
    with store.pool.connection() as c:
        for sql in sorted(_SE_MIGRATIONS.glob("*.sql")):
            body = sql.read_text(encoding="utf-8")
            c.execute(body.replace("BEGIN;", "").replace("COMMIT;", ""))
    seed_flow_measurands()
    return TestClient(
        create_app(
            runtime=runtime_with_store, web_config=WebConfig(corpus_dir=tmp_path)
        )
    )


@pytest.fixture
def imported(
    flow_client: TestClient, runtime_with_store: Any, tmp_path: Path
) -> tuple[SeHandler, Path, str]:
    """The script run offline and imported into ``flow-player``."""
    se = SeHandler(hub=runtime_with_store.hub)
    se.put(id="flow-player", text="{}")
    out_dir = tmp_path / "out"
    _run_script_offline(out_dir)
    echo = _run_through_tar_door(flow_client, se, "flow-player", out_dir)
    return se, out_dir, echo


def test_script_writes_the_run_with_no_db_and_no_se(tmp_path: Path) -> None:
    """Offline: the script step alone, and what it states."""
    out_dir = tmp_path / "out"
    _run_script_offline(out_dir)
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    (row,) = manifest["physics"]
    assert row["block"] == "c" and row["spec"]["inlet"].startswith("rect:")
    assert row["outcome"]["measures"]["breakthrough-fraction"] > 0.0


def test_imported_run_plays_in_the_flow_tab(
    flow_client: TestClient, imported: tuple[SeHandler, Path, str], store: Any
) -> None:
    _se, _out, echo = imported
    assert "script_mismatch" not in echo and "uncomputable" not in echo
    ref = store.get_ref(kind="se", id="flow-player")
    assert ref is not None
    tree = persist.load_tree(store, ref.id)
    assert [(f.block, f.pocket) for f in flow_pockets(tree)] == [("c", "flow")]

    page = flow_client.get("/se/flow-player/flow")
    assert page.status_code == 200
    assert "/static/se-flow-player.js" in page.text
    assert "/se/flow-player/flow.json?block=c" in page.text

    got = flow_client.get(
        "/se/flow-player/flow.json",
        params={"n": 60, "frames": 8, "nx": 100, "u0": 0.01, "mu": 1e-3, "rho": 1000},
    )
    assert got.status_code == 200, got.text
    payload = got.json()
    assert payload["mesh"]["outline"]
    assert payload["mesh"]["ports"]["inlet_b64"]
    assert payload["frames"]["shape"] == [8, 60, 3]
    assert payload["scale"]["L"] == pytest.approx(flow_chamber.L)
    card = payload["card"]
    assert card is not None and card["ok"]
    assert "thinness" in {line["name"] for line in card["lines"]}


def test_editing_the_bound_cad_design_misses_the_memo(
    flow_client: TestClient, imported: tuple[SeHandler, Path, str], store: Any
) -> None:
    """The chamber's solid lives in the bound cad ref: a save there
    leaves the se ref's ``updated_at`` alone and must still rebuild."""
    from precis.cad.scene import parse_source

    params = {"n": 40, "frames": 4, "nx": 60}
    before = flow_client.get("/se/flow-player/flow.json", params=params).json()
    assert before["scale"]["L"] == pytest.approx(0.1)
    ref = store.get_ref(kind="se", id="flow-player")
    assert ref is not None
    bound = persist.load_tree(store, ref.id).blocks["c"].bound
    wider = flow_chamber.CHAMBER_DSL.replace("w70mm", "w90mm").replace("35mm", "45mm")
    store.cad_save(slug=bound, title="wider", spec=parse_source(wider), card_text="w")
    after = flow_client.get("/se/flow-player/flow.json", params=params).json()
    assert after["scale"]["L"] == pytest.approx(0.12)
