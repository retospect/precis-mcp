"""The controller provisions a clean fix-lane overlay on every deploy."""

from pathlib import Path
from typing import Any

import pytest
import yaml

_DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
_PLAYBOOK = _DEPLOY / "playbooks" / "20k-precis-fix-lane-overlay.yml"

_20B = "playbooks/20b-precis-worker-collapsed.yml"
_20J = "playbooks/20j-deploy-colima.yml"
_20K = "playbooks/20k-precis-fix-lane-overlay.yml"


def _yaml(path: Path) -> list[dict[str, Any]]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _imports(entrypoint: str) -> list[str]:
    return [
        play["ansible.builtin.import_playbook"]
        for play in _yaml(_DEPLOY / entrypoint)
        if "ansible.builtin.import_playbook" in play
    ]


def _play() -> dict[str, Any]:
    (play,) = _yaml(_PLAYBOOK)
    return play


def _find_block(tasks: list[dict[str, Any]], label: str) -> dict[str, Any]:
    """The named block task, wherever it nests in the task list."""

    def walk(tasks: list[dict[str, Any]]) -> dict[str, Any] | None:
        for task in tasks:
            if task.get("name") == label and "block" in task:
                return task
            if "block" in task:
                found = walk(task["block"])
                if found:
                    return found
        return None

    block = walk(tasks)
    assert block is not None, f"no block named {label!r}"
    return block


@pytest.mark.parametrize("entrypoint", ["site.yml", "redeploy-precis.yml"])
def test_imported_by_site_and_redeploy_immediately_after_20j(entrypoint: str) -> None:
    order = _imports(entrypoint)
    assert order.count(_20K) == 1, entrypoint
    # Right after 20j keeps it behind 20b (whose file copy it replaces) and
    # clear of the 20e → 20g → 20h adjacency the lane tests pin.
    assert order.index(_20K) == order.index(_20J) + 1, entrypoint
    assert order.index(_20K) > order.index(_20B), entrypoint


def test_play_targets_the_gateway_and_stays_untagged() -> None:
    play = _play()
    assert play["hosts"] == "gateway"
    assert "tags" not in play


def test_clone_gates_on_the_same_fix_lane_switch_as_the_copy() -> None:
    (gate,) = _play()["tasks"]
    assert "block" in gate
    when = gate["when"]
    assert 'os_family == "darwin"' in when
    assert any("precis_fix_lane_enabled" in cond for cond in when)
    assert any("precis_fix_lane_deploys" in cond for cond in when)


def test_mirror_is_seeded_and_refreshed_from_controller_git() -> None:
    play = _play()
    (gate,) = play["tasks"]
    seed = _find_block(
        gate["block"], "Keep the private overlay mirror current from the controller"
    )
    # The mirror is the only clone source — the play never reaches for a
    # shared helper in another login's home: git's dubious-ownership check
    # would refuse it for the deploy user, on the clone and on every
    # release-time overlay-sync fetch.
    assert "when" not in seed
    assert "_deploy_helper" not in play["vars"]
    _, bundle, transfer, init, restrict, fetch = seed["block"]
    assert bundle["delegate_to"] == "localhost"
    assert bundle["ansible.builtin.command"]["argv"][2:] == [
        "{{ precis_overlay_dir }}",
        "--git-dir=.git",
        "bundle",
        "create",
        "{{ fix_lane_overlay_bundle.path }}",
        "refs/heads/main",
    ]
    assert transfer["ansible.builtin.copy"]["mode"] == "0600"
    assert "--bare" in init["ansible.builtin.command"]["argv"]
    assert restrict["ansible.builtin.file"]["mode"] == "0700"
    # Forced: a rewritten controller main must not wedge later deploys.
    assert (
        fetch["ansible.builtin.command"]["argv"][-1]
        == "+refs/heads/main:refs/heads/main"
    )
    # Git tasks run as deploy with deploy's HOME, not root's gitconfig.
    assert init["environment"]["HOME"] == "/Users/deploy"
    assert fetch["environment"]["HOME"] == "/Users/deploy"


def test_repeat_deploy_replaces_the_entire_copy_after_cloning() -> None:
    play = _play()
    (gate,) = play["tasks"]
    _, _, clone, verify, remove, move = gate["block"]
    git = clone["ansible.builtin.git"]
    assert git["repo"] == "file://{{ _fix_overlay_mirror }}"
    assert git["remote"] == "deploy-helper"
    assert git["version"] == "main"
    assert git["umask"] == "0077"
    assert git["dest"] == "{{ fix_lane_overlay_stage.path }}/inventory"
    assert clone["environment"]["HOME"] == "/Users/deploy"
    assert verify["ansible.builtin.stat"]["path"] == f"{git['dest']}/hosts.yml"
    assert remove["ansible.builtin.file"] == {
        "path": "{{ _fix_overlay_dir }}",
        "state": "absent",
    }
    assert move["ansible.builtin.command"]["argv"] == [
        "mv",
        git["dest"],
        "{{ _fix_overlay_dir }}",
    ]
    assert play["vars"]["_fix_overlay_dir"] == "{{ _fix_repo_dir }}/deploy/inventory"
