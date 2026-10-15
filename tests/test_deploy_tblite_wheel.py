"""The linux-aarch64 tblite wheel reaches the hosts xtb jobs pin to (gr488907).

PyPI's tblite has no linux-aarch64 wheel and ``[estimate]`` marker-skips it
there, so the only way the ``xtb`` relax rung's backend lands on the aarch64
compute hosts is the wheelhouse: ``scripts/build-tblite-wheel`` builds it,
``-e tblite_wheel=<path>`` pushes it, and the shared ``wheelhouse.yml``
re-apply installs and smokes it on every deploy. ``struct_relax`` pins xtb
jobs to ``mlip_nodes()`` — ``PRECIS_AUTOCATPATH_ROUTE_NODE``, i.e.
``precis_capabilities.autocatpath`` — so the install must run on that same
group, or each xtb job fails as infra on a host without the wheel.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[1]
ROLE = REPO / "deploy" / "roles" / "autocatpath"


def _tasks(path: Path) -> list[dict[str, Any]]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _named(tasks: list[dict[str, Any]], needle: str) -> dict[str, Any]:
    hits = [t for t in tasks if needle in t.get("name", "")]
    assert len(hits) == 1, f"expected one task naming {needle!r}, got {len(hits)}"
    return hits[0]


def test_defaults_declare_the_push_and_the_required_switch() -> None:
    defaults = yaml.safe_load(
        (ROLE / "defaults" / "main.yml").read_text(encoding="utf-8")
    )
    assert defaults["tblite_wheel"] == ""
    assert defaults["tblite_required"] is False


def test_seed_pushes_the_controller_wheel_on_linux_only() -> None:
    task = _named(_tasks(ROLE / "tasks" / "wheelhouse_seed.yml"), "tblite wheel")
    assert task["ansible.builtin.copy"]["src"] == "{{ tblite_wheel }}"
    assert 'os_family == "linux"' in task["when"]


def test_reapply_installs_then_smokes_gfn2() -> None:
    tasks = _tasks(ROLE / "tasks" / "wheelhouse.yml")
    find = _named(tasks, "Find tblite wheels")
    assert find["ansible.builtin.find"]["patterns"] == "tblite-*.whl"
    install = _named(tasks, "Install the newest tblite wheel")
    shell = install["ansible.builtin.shell"]
    assert "--constraints /opt/precis/constraints.txt" in shell
    assert "tblite_wheel_newest" in shell
    verify = _named(tasks, "Verify GFN2-xTB")
    cmd = verify["ansible.builtin.command"]["cmd"]
    assert "GFN2-xTB" in cmd and "electronic_temperature=1000.0" in cmd
    refuse = _named(tasks, "Refuse a compute host with no tblite wheel")
    assert "tblite_required | bool" in refuse["when"]


def test_reapply_runs_on_the_group_xtb_jobs_pin_to() -> None:
    provision = _tasks(
        REPO / "deploy" / "roles" / "precis_worker" / "tasks" / "provision.yml"
    )
    reapply = _named(provision, "Re-apply the autocatpath wheelhouse")
    assert reapply["ansible.builtin.include_role"]["tasks_from"] == "wheelhouse"
    assert "precis_capabilities.autocatpath |" in reapply["when"]

    from precis.workers.job_types import struct_relax

    assert "xtb" in struct_relax._INPROC_FIDELITIES
    assert struct_relax._AUTOCATPATH_NODES_ENV == "PRECIS_AUTOCATPATH_ROUTE_NODE"
