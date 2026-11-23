"""Deploy lint: the mcp-public restart drains instead of dropping the listener (gr660009)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
_DRAINING = _DEPLOY / "tasks/reload_launchd_draining.yml"


def _tasks() -> list[dict[str, Any]]:
    return yaml.safe_load(_DRAINING.read_text(encoding="utf-8"))


def test_draining_reload_signals_the_supervisor_instead_of_booting_out() -> None:
    script = _tasks()[1]["ansible.builtin.shell"]
    assert "kill -HUP" in script
    # The two fallback codes that hand over to the full bounce.
    assert "exit 42" in script and "exit 43" in script
    # Health check is the install's own shape: 401 + resource_metadata.
    assert "resource_metadata=" in script
    # The force path reaps the socket holder's inherited-socket child too.
    assert "lsof -nP -iTCP" in script


def test_draining_reload_falls_back_to_the_full_bounce() -> None:
    _, draining, fallback = _tasks()
    assert (
        draining["failed_when"] == "_precis_draining_reload.rc not in [0, 42, 43, 44]"
    )
    assert fallback["when"] == "_precis_draining_reload.rc in [42, 43, 44]"
    _, bootstrap, _ = fallback["block"]
    assert bootstrap["ansible.builtin.include_tasks"].endswith("reload_launchd.yml")
    assert bootstrap["vars"] == {"reload_persistent": True}


def test_changed_plist_bootstraps_before_recording_the_new_checksum() -> None:
    plist, draining, fallback = _tasks()
    assert (
        plist["ansible.builtin.stat"]["path"]
        == "/Library/LaunchDaemons/{{ reload_unit }}.plist"
    )
    assert plist["ansible.builtin.stat"]["checksum_algorithm"] == "sha256"
    assert plist["register"] == "_precis_reload_plist"
    script = draining["ansible.builtin.shell"]
    assert 'cat "/var/db/${unit}.plist.sha256"' in script
    mismatch = (
        'if [ "${loaded}" != {{ _precis_reload_plist.stat.checksum | quote }} ]; then'
    )
    assert script.index(mismatch) < script.index("exit 44") < script.index("kill -HUP")
    assert (
        fallback["vars"]["_precis_reload_receipt"]
        == "/var/db/{{ reload_unit }}.plist.sha256"
    )
    invalidate, bootstrap, record = fallback["block"]
    assert invalidate["ansible.builtin.file"] == {
        "path": "{{ _precis_reload_receipt }}",
        "state": "absent",
    }
    assert bootstrap["ansible.builtin.include_tasks"].endswith("reload_launchd.yml")
    assert record["ansible.builtin.copy"]["dest"] == "{{ _precis_reload_receipt }}"
    assert record["ansible.builtin.copy"]["content"].strip() == (
        "{{ _precis_reload_plist.stat.checksum }}"
    )


def test_public_mcp_handler_drains_and_web_keeps_the_full_bounce() -> None:
    handlers = yaml.safe_load(
        (_DEPLOY / "roles/precis_web/handlers/main.yml").read_text(encoding="utf-8")
    )
    by_name = {h["name"]: h for h in handlers}
    public = by_name["restart precis-mcp-public"]
    assert public["ansible.builtin.include_tasks"].endswith(
        "reload_launchd_draining.yml"
    )
    assert public["vars"]["reload_unit"] == "com.precis.mcp-public"
    assert public["vars"]["reload_port"] == "{{ precis_mcp_public_port }}"
    web = by_name["restart precis-web"]
    assert web["ansible.builtin.include_tasks"].endswith("reload_launchd.yml")
