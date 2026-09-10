"""Regression gate for gr333210: a redeploy must re-converge the inbox
watcher's unit definition, not just the venv it runs out of.

Background: commit c6c386a3 (v8.33.0) renamed the watcher CLI `precis watch`
-> `precis ingest --watch` and fixed `deploy/roles/precis_watch/templates/*`
to match, but `deploy/redeploy-precis.yml` never imported
`playbooks/28-precis-watch.yml` — only the standalone-run role re-rendered
the LaunchDaemon/systemd unit. Every ordinary redeploy kept upgrading
`/opt/precis/venv` while the on-disk unit still invoked the old subcommand,
crash-looping paper ingest for 10 days before anyone ran 28 by hand.

This is a pure text walk (no ansible needed), mirroring
test_deploy_lockfile_constraints.py's approach for the same class of
"a playbook exists but nothing imports it" gap.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEPLOY = _REPO_ROOT / "deploy"
_REDEPLOY = _DEPLOY / "redeploy-precis.yml"
_WATCH_PLAYBOOK = "playbooks/28-precis-watch.yml"
_WORKER_VENV_PLAYBOOK = "playbooks/20b-precis-worker-collapsed.yml"


def _redeploy_text() -> str:
    return _REDEPLOY.read_text(encoding="utf-8")


def test_watch_playbook_exists() -> None:
    assert (_DEPLOY / _WATCH_PLAYBOOK).is_file(), (
        f"deploy/{_WATCH_PLAYBOOK} not found — the inbox watcher's role "
        "(precis_watch) has no playbook entry point"
    )


def test_redeploy_imports_the_watch_unit_playbook() -> None:
    """gr333210: without this import, a redeploy upgrades /opt/precis/venv
    while leaving the watcher's LaunchDaemon/systemd unit on whatever CLI
    invocation it was last rendered with — silently stale, and crash-looping
    when the two drift (as they did for 10 days)."""
    text = _redeploy_text()
    assert _WATCH_PLAYBOOK in text, (
        f"deploy/redeploy-precis.yml does not import {_WATCH_PLAYBOOK} — "
        "the watcher unit definition can silently drift from the venv it "
        "runs out of (gr333210)"
    )


def test_watch_import_follows_the_worker_venv_provisioning_import() -> None:
    """precis_watch reuses /opt/precis/venv and fails hard if it's missing
    (see roles/precis_watch/tasks/main.yml) — its playbook must be imported
    AFTER whatever import provisions that venv, not before."""
    text = _redeploy_text()
    venv_idx = text.index(_WORKER_VENV_PLAYBOOK)
    watch_idx = text.index(_WATCH_PLAYBOOK)
    assert watch_idx > venv_idx, (
        f"{_WATCH_PLAYBOOK} is imported before {_WORKER_VENV_PLAYBOOK} in "
        "deploy/redeploy-precis.yml — precis_watch reuses /opt/precis/venv "
        "and fails if it doesn't exist yet"
    )
