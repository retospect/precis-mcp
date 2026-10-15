"""Deploy play order: migrate first, per-host isolation, a completion marker.

r36 (2026-10-15, gr488897): play 43 restarted the compute lane onto new code
while `precis migrate` still sat in play 38 near the end of the run. Both
compute hosts failed (one build assert, one unreachable), ansible ended the
whole playbook because every host of that play's batch had failed, 38 never
ran, and every r36 worker exited with "database is 2 migration(s) behind
this package".

These tests read the parsed YAML and pin the three properties that close it:

  (a) migrate first — playbooks/07-precis-migrate.yml runs once, from the
      gateway, before any play that can restart a daemon onto new code, in
      both redeploy-precis.yml and site.yml; a failure there ends the run
      (any_errors_fatal), and play 38 does not migrate a second time;
  (b) per-host isolation — no play sets any_errors_fatal (except 07),
      max_fail_percentage or serial, and every play whose hosts can all be
      non-critical carries the gateway as an anchor that leaves in its first
      pre_task, so one lane failing never ends the run for the other hosts;
  (c) the last play prints PRECIS_DEPLOY_COMPLETE from the gateway, which
      scripts/deploy requires before it moves origin/prod
      (tests/test_deploy_run_markers.py covers that side).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
REDEPLOY = DEPLOY / "redeploy-precis.yml"
SITE = DEPLOY / "site.yml"
MIGRATE = DEPLOY / "playbooks" / "07-precis-migrate.yml"
WEB_TASKS = DEPLOY / "roles" / "precis_web" / "tasks"

# Groups that always contain a serving-critical host (the gateway or the DB
# node). `macos` holds the gateway: precis_web is darwin-only.
_CRITICAL_GROUPS = {"gateway", "data", "all", "macos"}

# Plays that may run ahead of the migration: none of them installs precis
# code into a venv a daemon runs from, or restarts anything.
_BEFORE_MIGRATE_REDEPLOY = {
    "Pin the deploy target to a single commit sha (freeze a moving ref)",
    "Purge retired per-user Claude credential files",
    "Pre-flight: will this deploy move any managed venv? (gate the restart)",
    "playbooks/03-precis-constraints.yml",
    "playbooks/06-pythonorg.yml",
    "playbooks/19-pgpass.yml",
}

# Plays that restart (or first start) a precis daemon onto the pinned code.
_DAEMON_PLAYBOOKS = {
    "playbooks/19a-precis-embedder.yml",
    "playbooks/20b-precis-worker-collapsed.yml",
    "playbooks/20e-precis-worker-agentlane.yml",
    "playbooks/20c-precis-heartbeat-serving.yml",
    "playbooks/28-precis-watch.yml",
    "playbooks/43-precis-worker-compute.yml",
    "playbooks/41-precis-embedder-watchdog.yml",
    "playbooks/38-precis-web.yml",
    "playbooks/32-asa-code-refresh.yml",
    "playbooks/35-precis-worker-sandbox.yml",
}

# Plays that target only lane hosts and therefore carry the gateway anchor.
_ANCHORED = {
    "playbooks/20c-precis-heartbeat-serving.yml",
    "playbooks/43-precis-worker-compute.yml",
    "playbooks/34-code-task-image.yml",
    "playbooks/35-precis-worker-sandbox.yml",
}
_ANCHORED_INLINE = "Bounce + flag orphaned precis worker lanes (no play owns them here)"


def _load(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _entries(path: Path) -> list[str]:
    """One label per top-level play: the import path, else the play name."""
    return [e.get("ansible.builtin.import_playbook") or e["name"] for e in _load(path)]


def _expand(path: Path, label: str | None) -> list[tuple[str, dict[str, Any]]]:
    out: list[tuple[str, dict[str, Any]]] = []
    for entry in _load(path):
        imp = entry.get("ansible.builtin.import_playbook")
        if imp:
            out.extend(_expand(path.parent / imp, label or imp))
        else:
            out.append((label or entry["name"], entry))
    return out


def _imported_plays() -> list[tuple[str, dict[str, Any]]]:
    """(label, play) for every play redeploy-precis.yml runs, imports expanded
    (nested ones too); an imported play is labelled with its top-level import."""
    return _expand(REDEPLOY, None)


def _role_text(play: dict[str, Any]) -> str:
    """Tasks + handlers of every role a play names, as one string."""
    names = [r if isinstance(r, str) else r["role"] for r in play.get("roles", [])]
    for task in _tasks(play):
        for key in ("ansible.builtin.import_role", "ansible.builtin.include_role"):
            if key in task:
                names.append(task[key]["name"])
    text = ""
    for name in names:
        for sub in ("tasks", "handlers"):
            for f in sorted((DEPLOY / "roles" / name / sub).glob("*.yml")):
                text += f.read_text(encoding="utf-8")
    return text


def _host_groups(play: dict[str, Any]) -> set[str]:
    hosts = play["hosts"]
    parts = hosts if isinstance(hosts, list) else str(hosts).split(":")
    return {str(p).lstrip("&!") for p in parts if p}


def _tasks(play: dict[str, Any]) -> list[dict[str, Any]]:
    return [*play.get("pre_tasks", []), *play.get("tasks", [])]


# ── (a) migrate first ───────────────────────────────────────────────────


def test_redeploy_migrates_before_any_daemon_play() -> None:
    labels = _entries(REDEPLOY)
    at = labels.index("playbooks/07-precis-migrate.yml")
    assert set(labels[:at]) == _BEFORE_MIGRATE_REDEPLOY, labels[:at]
    after = set(labels[at + 1 :])
    assert after >= _DAEMON_PLAYBOOKS, _DAEMON_PLAYBOOKS - after
    assert "Bounce all precis daemons" in after
    assert labels.count("playbooks/07-precis-migrate.yml") == 1


def test_the_plays_ahead_of_the_migration_restart_nothing() -> None:
    """A guard on the allowlist above: nothing that runs before 07 touches
    systemd or launchd, in the play itself or in a role it runs."""
    for label, play in _imported_plays():
        if label == "playbooks/07-precis-migrate.yml":
            break
        text = yaml.safe_dump(play) + _role_text(play)
        for needle in ("systemd", "launchctl", "kickstart", "notify:"):
            assert needle not in text, (label, needle)


def test_site_yml_migrates_before_any_daemon_play() -> None:
    labels = _entries(SITE)
    at = labels.index("playbooks/07-precis-migrate.yml")
    before = set(labels[:at])
    for pb in (
        "playbooks/14-mcps.yml",
        "playbooks/20b-precis-worker-collapsed.yml",
        "playbooks/20e-precis-worker-agentlane.yml",
        "playbooks/20c-precis-heartbeat-serving.yml",
        "playbooks/43-precis-worker-compute.yml",
        "playbooks/38-precis-web.yml",
        "playbooks/27-extract-watch.yml",
        "playbooks/31-asa-bot.yml",
        "playbooks/48-asa-slack.yml",
    ):
        assert pb in labels[at + 1 :], pb
        assert pb not in before, pb
    # the venv install inside 07 reads the constraints file 13a writes
    assert "playbooks/03-precis-constraints.yml" in before


def test_the_migrate_play_runs_once_on_the_gateway_and_stops_the_run() -> None:
    plays = _load(MIGRATE)
    assert len(plays) == 1
    play = plays[0]
    assert _host_groups(play) == {"gateway"}
    assert play["any_errors_fatal"] is True
    assert "always" in play["tags"]
    includes = [
        t["ansible.builtin.include_role"]
        for t in play["tasks"]
        if "ansible.builtin.include_role" in t
    ]
    assert [(i["name"], i["tasks_from"]) for i in includes] == [
        ("precis_web", "install"),
        ("precis_web", "migrate"),
    ]
    for inc in includes:
        assert "always" in inc["apply"]["tags"]
    facts = [t for t in play["tasks"] if "ansible.builtin.set_fact" in t]
    assert facts and facts[0]["ansible.builtin.set_fact"]["precis_db_migrated"] is True
    msgs = [
        t["ansible.builtin.debug"]["msg"]
        for t in play["tasks"]
        if "ansible.builtin.debug" in t
    ]
    assert len(msgs) == 1 and msgs[0].startswith("PRECIS_DEPLOY_MIGRATED {{")


def test_play_38_installs_then_skips_a_second_migrate() -> None:
    main = _load(WEB_TASKS / "main.yml")
    imports = [
        t["ansible.builtin.import_tasks"]
        for t in main
        if "ansible.builtin.import_tasks" in t
    ]
    assert imports == ["install.yml", "migrate.yml"]
    install = _load(WEB_TASKS / "install.yml")
    assert any("precis-mcp[web,pcb,estimate]" in t["name"] for t in install)
    (migrate,) = _load(WEB_TASKS / "migrate.yml")
    assert migrate["name"] == "Apply pending precis DB migrations"
    assert any("precis_db_migrated" in w for w in migrate["when"])
    assert "migrate" in migrate["ansible.builtin.command"]["argv"]


# ── (b) per-host isolation ─────────────────────────────────────────────


def test_no_play_turns_one_host_failure_into_a_run_abort() -> None:
    for label, play in _imported_plays():
        assert "max_fail_percentage" not in play, label
        assert "serial" not in play, label
        if label != "playbooks/07-precis-migrate.yml":
            assert not play.get("any_errors_fatal"), label
        for task in _tasks(play):
            assert not task.get("any_errors_fatal"), (label, task.get("name"))


def test_every_play_keeps_a_critical_host_in_its_batch() -> None:
    """ansible ends the whole playbook when every host of one play's batch
    fails (playbook_executor: len(batch) == failed). A play whose hosts are
    all lane hosts must carry the gateway, so the batch always has a
    survivor."""
    for label, play in _imported_plays():
        groups = _host_groups(play)
        assert groups & _CRITICAL_GROUPS, (label, groups)


def test_lane_plays_carry_a_gateway_anchor_that_leaves_first() -> None:
    seen = set()
    for label, play in _imported_plays():
        if label not in _ANCHORED and label != _ANCHORED_INLINE:
            continue
        seen.add(label)
        groups = _host_groups(play)
        assert "gateway" in groups, label
        first = play["pre_tasks"][0]
        assert first["ansible.builtin.meta"] == "end_host", label
        assert first["name"].startswith("Isolation anchor"), label
        # the anchor leaves on every host outside the play's own lane groups
        for group in groups - {"gateway"}:
            assert f"groups['{group}']" in first["when"], (label, group)
        assert "groups['gateway']" not in first["when"], label
    assert seen == _ANCHORED | {_ANCHORED_INLINE}, seen


# ── (c) completion marker ──────────────────────────────────────────────


def test_the_last_play_prints_the_completion_marker_from_the_gateway() -> None:
    last = _load(REDEPLOY)[-1]
    assert _host_groups(last) == {"gateway"}
    assert "always" in last["tags"]
    (task,) = last["tasks"]
    assert task["ansible.builtin.debug"]["msg"].startswith("PRECIS_DEPLOY_COMPLETE {{")
