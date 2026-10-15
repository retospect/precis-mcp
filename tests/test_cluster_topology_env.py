"""`scripts/lib/cluster-topology.sh` — the session-MCP container's kind-surface
env, rendered from the gitignored overlay's ``precis_capabilities``
(gr489468: the container had no PRECIS_CHEM_ROUTE_NODE, so
``put(kind='route', engine='aizynth')`` was refused on the session surface
while the gateway's plist-rendered env worked).

Runs the real lib against a fixture inventory (placeholder names, RFC-5737
addresses) via ``CLUSTER_INVENTORY``, the same resolver pgb-host.sh uses, and
checks the rendered ``KEY=VAL`` lines mirror
``roles/precis_web/templates/precis-web.plist.j2`` — including the "declared
plugin, no compute node" case, which un-darks the kind but leaves
``put()`` reporting "needs a route node", exactly like the gateway.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only: sources a bash lib"
)

REPO_ROOT = Path(__file__).resolve().parents[1]
_LIB = REPO_ROOT / "scripts" / "lib" / "cluster-topology.sh"
_ENSURE = REPO_ROOT / "deploy" / "mcp-http" / "precis-mcp-http-ensure.sh"

_HOSTS = """\
all:
  vars:
    postgres_host: node-data
  children:
    macos:
      hosts:
        node-gateway:
          ansible_host: 203.0.113.10
    linux:
      hosts:
        node-gpu:
          ansible_host: 203.0.113.20
        node-gpu2:
          ansible_host: 203.0.113.21
"""

_TOPOLOGY_FULL = """\
precis_capabilities:
  web_mcp:         [node-gateway]
  autocatpath:         [node-gpu, node-gpu2]
  autocatpath_plugin:  [node-gateway]
  chem_route:      [node-gpu]
  chem_plugin:     [node-gateway]
  bio_fold:        [node-gpu2]
  bio_plugin:      [node-gateway]
  dft:             [node-gpu2]
"""

_TOPOLOGY_NO_CHEM = """\
precis_capabilities:
  web_mcp:         [node-gateway]
  bio_fold:        [node-gpu2]
  bio_plugin:      [node-gateway]
"""

_TOPOLOGY_CHEM_NO_ROUTE = """\
precis_capabilities:
  chem_plugin:     [node-gateway]
  chem_route:      []
"""

_TOPOLOGY_BLOCK_LIST = """\
precis_capabilities:
  chem_plugin:
    - node-gateway
  chem_route:
    - node-gpu
"""


def _overlay(tmp_path: Path, topology: str) -> Path:
    inv = tmp_path / "inventory"
    gvars = inv / "group_vars" / "all"
    gvars.mkdir(parents=True)
    (inv / "hosts.yml").write_text(_HOSTS, encoding="utf-8")
    (gvars / "topology.yml").write_text(topology, encoding="utf-8")
    return inv / "hosts.yml"


def _env_lines(
    tmp_path: Path, topology: str | None
) -> subprocess.CompletedProcess[str]:
    env = {**os.environ}
    if topology is not None:
        env["CLUSTER_INVENTORY"] = str(_overlay(tmp_path, topology))
    else:
        env["CLUSTER_INVENTORY"] = str(tmp_path / "missing-hosts.yml")
    return subprocess.run(
        ["bash", "-c", f'. "{_LIB}" && topology_surface_env'],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )


def test_full_topology_renders_the_web_plist_surface(tmp_path: Path) -> None:
    proc = _env_lines(tmp_path, _TOPOLOGY_FULL)
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.splitlines()
    assert "PRECIS_CHEM_ENABLED=1" in lines
    # first of chem_route, like the plist's `| first`
    assert "PRECIS_CHEM_ROUTE_NODE=node-gpu" in lines
    assert "PRECIS_BIO_ENABLED=1" in lines
    assert "PRECIS_FOLD_NODE=node-gpu2" in lines
    # autocatpath keeps the WHOLE list (the plist `join(',')`), and carries
    # the optional tunables from tracked deploy/group_vars/all.yml
    assert "PRECIS_AUTOCATPATH_ROUTE_NODE=node-gpu,node-gpu2" in lines
    assert "PRECIS_AUTOCATPATH_VERIFY_WALL_SECONDS=64800" in lines
    assert "PRECIS_AUTOCATPATH_SEED_CPUSET=0-4,10-14" in lines
    assert "PRECIS_DFT_NODE=node-gpu2" in lines


def test_missing_caps_omit_their_env_cleanly(tmp_path: Path) -> None:
    proc = _env_lines(tmp_path, _TOPOLOGY_NO_CHEM)
    assert proc.returncode == 0, proc.stderr
    assert "PRECIS_BIO_ENABLED=1" in proc.stdout.splitlines()
    assert "PRECIS_CHEM_ENABLED" not in proc.stdout
    assert "PRECIS_CHEM_ROUTE_NODE" not in proc.stdout
    assert "PRECIS_AUTOCATPATH_ROUTE_NODE" not in proc.stdout
    assert "PRECIS_DFT_NODE" not in proc.stdout


def test_plugin_without_route_node_still_undarks(tmp_path: Path) -> None:
    proc = _env_lines(tmp_path, _TOPOLOGY_CHEM_NO_ROUTE)
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.splitlines()
    assert "PRECIS_CHEM_ENABLED=1" in lines
    assert "PRECIS_CHEM_ROUTE_NODE" not in proc.stdout


def test_block_list_yaml_form(tmp_path: Path) -> None:
    proc = _env_lines(tmp_path, _TOPOLOGY_BLOCK_LIST)
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.splitlines()
    assert "PRECIS_CHEM_ENABLED=1" in lines
    assert "PRECIS_CHEM_ROUTE_NODE=node-gpu" in lines


def test_no_overlay_fails_so_the_caller_can_degrade(tmp_path: Path) -> None:
    proc = _env_lines(tmp_path, None)
    assert proc.returncode != 0
    assert proc.stdout == ""


def test_ensure_script_consumes_the_rendered_env() -> None:
    """The wiring the lib exists for: the ensure script sources the lib from
    the resolved REPO and feeds the rendered lines into the container ENVS."""
    text = _ENSURE.read_text(encoding="utf-8")
    assert "scripts/lib/cluster-topology.sh" in text
    assert "topology_surface_env" in text
    assert 'TOPO_ENVS+=(-e "$kv")' in text
    envs_block = text[text.index("ENVS=(") : text.index("\n)", text.index("ENVS=("))]
    assert 'TOPO_ENVS[@]+"${TOPO_ENVS[@]}"' in envs_block
