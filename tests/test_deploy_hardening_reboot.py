"""The macOS nightly update + reboot cron is optional per host: its state must
follow ``hardening_macos_nightly_reboot`` (default true, so other Macs keep it)."""

from __future__ import annotations

from pathlib import Path

import yaml

_ROLE = Path(__file__).resolve().parent.parent / "deploy" / "roles" / "hardening"


def _cron_task() -> dict:
    tasks = yaml.safe_load((_ROLE / "tasks" / "main.yml").read_text(encoding="utf-8"))
    return next(
        t
        for t in tasks
        if t.get("when") == 'os_family == "darwin"'
        and "ansible.builtin.cron" in t
        and t["ansible.builtin.cron"].get("job", "").endswith("daily-update-reboot.sh")
    )


def test_cron_state_depends_on_nightly_reboot_flag() -> None:
    state = _cron_task()["ansible.builtin.cron"]["state"]
    assert "hardening_macos_nightly_reboot" in state
    assert "'absent'" in state


def test_nightly_reboot_defaults_on() -> None:
    defaults = yaml.safe_load(
        (_ROLE / "defaults" / "main.yml").read_text(encoding="utf-8")
    )
    assert defaults["hardening_macos_nightly_reboot"] is True
