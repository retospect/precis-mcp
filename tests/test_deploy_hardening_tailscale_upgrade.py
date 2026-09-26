"""Regression for gr273966: macOS fleet nodes install Tailscale via Homebrew
with ``state: present`` (deploy/roles/tailscale/tasks/main.yml) and nothing
ever upgraded it — one node drifted to 1.96.4 against the fleet's 1.102.3,
and a manual ``brew upgrade tailscale`` leaves the running ``tailscaled``
daemon on the old version (client/daemon mismatch) until something restarts
it. The fix rides the existing nightly macOS update-and-reboot script (whose
subsequent reboot restarts tailscaled) rather than flipping the tailscale
role to ``state: latest``, which would upgrade+restart tailscaled mid-deploy
and interrupt the live web funnel. These tests pin both halves of that
decision so a future edit can't silently drop one.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_HARDENING_TASKS = _REPO_ROOT / "deploy" / "roles" / "hardening" / "tasks" / "main.yml"
_TAILSCALE_TASKS = _REPO_ROOT / "deploy" / "roles" / "tailscale" / "tasks" / "main.yml"


def test_daily_update_reboot_script_upgrades_tailscale_via_brew() -> None:
    """The macOS nightly update script must upgrade Tailscale (as the
    brew-owning user, tolerating failure) before the reboot that restarts
    tailscaled."""
    text = _HARDENING_TASKS.read_text(encoding="utf-8")
    assert "brew upgrade tailscale" in text, (
        "daily-update-reboot.sh no longer upgrades tailscale via brew — "
        "gr273966 regression: macOS nodes will drift again"
    )

    lines = text.splitlines()
    upgrade_idx = next(
        i for i, line in enumerate(lines) if "brew upgrade tailscale" in line
    )
    reboot_idx = next(i for i, line in enumerate(lines) if "shutdown -r now" in line)
    assert upgrade_idx < reboot_idx, (
        "the tailscale brew upgrade must run BEFORE the reboot, so the "
        "reboot's tailscaled restart picks up the new binary"
    )

    upgrade_line = lines[upgrade_idx]
    assert "|| true" in upgrade_line, (
        "the brew upgrade must tolerate failure — a brew hiccup must never "
        "block the nightly reboot"
    )
    assert "sudo -u deploy" in upgrade_line or "sudo -u deploy" in "\n".join(
        lines[max(0, upgrade_idx - 3) : upgrade_idx]
    ), "brew must run as the brew-owning deploy user, not root"


def test_tailscale_role_stays_state_present_not_latest() -> None:
    """The tailscale role must NOT flip to ``state: latest`` — that would
    upgrade+restart tailscaled during a live deploy, interrupting the web
    funnel. Upgrades are meant to ride the nightly script instead."""
    text = _TAILSCALE_TASKS.read_text(encoding="utf-8")
    assert "state: present" in text
    assert "state: latest" not in text
    # The role must document why, so a future editor doesn't "fix" this.
    assert "gr273966" in text
