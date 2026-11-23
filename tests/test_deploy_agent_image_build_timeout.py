"""Regression for gr335099: the precis-agent image play's daemon-side
pre-pull preflight (gr307314) was a plain ``ansible.builtin.shell`` task with
only ``retries``/``until`` and no ``async``/timeout bound. ``until`` only
re-evaluates once an attempt actually *returns* — a ``docker pull`` wedged
inside a dead buildkitd (0% CPU, no registry connections) never returns, so
the retry ladder never gets a turn. This hung TWICE in production, each
holding the deploy lock ~4h until a human SIGTERM'd the wedged process by
hand, at which point ansible's own next retry succeeded unaided.

``deploy/playbooks/33-precis-agent-image.yml`` now bounds the pre-pull the
same way the trailing ``docker build`` task already bounded itself
(``async``/``poll``, portable — no reliance on a ``timeout(1)`` binary,
which stock macOS doesn't ship), captures buildx diagnostics on a terminal
failure of either step, and defers the build task's failure past those
diagnostics + the existing cleanup tasks to an explicit trailing assert.
gr690411 moved everything after the build task into the block's ``always:``
(a controller-side raise — a FileNotFoundError on an async poll — aborts
the block and would skip anything inline), with a bounded wait for the
host wrapper to exit first: the wrapper's stall record lands in the trail
only when the wrapper exits, up to ~stall-ceiling seconds after the
controller dies. These tests pin that shape directly in the YAML so a
future edit can't silently drop the bound, the diagnostics capture, the
wait, or the deferred assert.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PLAYBOOK = _REPO_ROOT / "deploy" / "playbooks" / "33-precis-agent-image.yml"


def _load_tasks() -> list[dict[str, Any]]:
    plays = yaml.safe_load(_PLAYBOOK.read_text(encoding="utf-8"))
    assert len(plays) == 1
    return plays[0]["tasks"]


def _find(tasks: list[dict[str, Any]], name_fragment: str) -> dict[str, Any]:
    matches = [t for t in tasks if name_fragment in t.get("name", "")]
    assert len(matches) == 1, (
        f"expected exactly one task matching {name_fragment!r}, found {len(matches)}"
    )
    return matches[0]


def _build_block_tasks(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rebuild = _find(tasks, "(Re)build the precis-agent image on the pinned sha")
    return rebuild["block"]


def _build_always_tasks(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The block's ``always:`` section (gr690411): the progress-trail read and
    the failure diagnostics live there so they run even when a build-block task
    RAISES on the controller instead of failing."""
    rebuild = _find(tasks, "(Re)build the precis-agent image on the pinned sha")
    return rebuild["always"]


def test_trail_read_and_diagnostics_run_in_always() -> None:
    """gr690411: a controller-side raise (FileNotFoundError mid-poll) aborts
    the block, so the host-side stall trail must be read from ``always:``."""
    always = _build_always_tasks(_load_tasks())
    _find(always, "Read the build's progress trail from the host")
    _find(always, "Report agent-image build timing + progress tail")
    block_names = [t.get("name", "") for t in _build_block_tasks(_load_tasks())]
    assert not any("progress trail" in n for n in block_names)


def test_always_waits_for_wrapper_exit_before_reading_trail() -> None:
    """gr690411: the controller can die up to ~_agent_build_stall_sec BEFORE
    the host wrapper writes its stall record (the 2026-09-27 gap was 78 s),
    so a trail read taken at abort time succeeds yet still misses the record.
    ``always:`` must therefore wait for the wrapper process to exit before
    slurping — bounded by the stall ceiling and best-effort, so an expiring
    wait never aborts the chain before the read."""
    always = _build_always_tasks(_load_tasks())
    always_names = [t.get("name", "") for t in always]
    wait = _find(always, "Wait for the build wrapper")
    slurp = _find(always, "Read the build's progress trail from the host")
    assert always_names.index(wait["name"]) < always_names.index(slurp["name"])
    assert wait["failed_when"] is False, (
        "an expiring wait must not redden the host — the trail read and "
        "diagnostics after it are the point"
    )
    assert "until" in wait, "the wait must poll until the wrapper is gone"
    assert "retries" in wait and "delay" in wait
    assert "_agent_build_stall_sec" in str(wait["retries"]), (
        "the wait's bound must scale with the stall ceiling — the watchdog "
        "record can land that long after the controller aborts"
    )
    # td771340: ansible runs the probe as `bash -c '<text>'`, so a bare
    # wrapper-path/log-path pattern appears verbatim in the checker's own
    # cmdline and pgrep -f self-matches — `until` never sees rc 0 and the
    # wait burns its whole bound on every run. The probe must be a
    # bracketed pattern the checker's cmdline cannot match.
    probe = wait["ansible.builtin.shell"]
    assert "_agent_build_wrapper" not in probe, (
        "pgrep -f on the wrapper path self-matches the checker's own "
        "bash -c cmdline — use a bracketed probe like [p]recis-agent-build"
    )
    assert "[p]recis-agent-build" in probe, (
        "the wait probe must match the wrapper process without matching "
        "itself — the [p] bracket keeps the literal pattern text out of "
        "the regex's own match set"
    )


def test_pre_pull_preflight_is_bounded_by_async() -> None:
    """The daemon-side pre-pull (gr307314) must carry an ``async``/``poll``
    ceiling — the gr335099 gap: a plain shell task with only retries/until
    never gets retried at all when a single attempt hangs forever."""
    tasks = _load_tasks()
    pre_pull = _find(
        tasks, "Preflight: pre-pull build images through the docker daemon"
    )
    assert "async" in pre_pull, (
        "pre-pull preflight has no async ceiling — a daemon-side wedge "
        "(gr335099) can hang this task forever; retries/until never fire "
        "because they only re-evaluate once an attempt returns."
    )
    assert pre_pull["async"] == "{{ _agent_pull_timeout_sec }}"
    assert "poll" in pre_pull
    # A timed-out async result has no rc — the until-expression must guard
    # with default(), same pitfall the build task's own comment documents.
    assert "default(1)" in pre_pull["until"]


def test_pre_pull_timeout_var_is_bounded_and_overridable() -> None:
    """The pre-pull ceiling defaults to a bounded value derived from
    ``precis_agent_pull_timeout_sec`` (overridable), not a hardcoded literal
    with no escape hatch."""
    play_src = yaml.safe_load(_PLAYBOOK.read_text(encoding="utf-8"))[0]
    expr = play_src["vars"]["_agent_pull_timeout_sec"]
    assert "precis_agent_pull_timeout_sec" in expr
    assert "default(600)" in expr, (
        "the pre-pull ceiling should default to minutes, not hours — a "
        "syntax-frontend manifest + one base-image layer normally pulls in "
        "well under a minute even cold."
    )


def test_build_task_defers_failure_past_diagnostics_and_cleanup() -> None:
    """The build task must NOT natively fail the host (``failed_when:
    false``) — failure is deferred to an explicit trailing assert in the
    block's ``always:`` (gr690411), after the diagnostics and cleanup, so
    EVERY path — an ordinary failed build and a controller-side RAISE that
    aborts the block — captures diagnostics and cleans up before the host
    halts, instead of the host halting mid-block with nothing captured and
    a stale build dir/tarball left behind."""
    block_tasks = _build_block_tasks(_load_tasks())
    build_task = _find(block_tasks, "docker build --target agent")
    assert build_task["failed_when"] is False

    assert "async" in build_task
    assert build_task["async"] == "{{ _agent_build_timeout_sec }}"

    always = _build_always_tasks(_load_tasks())
    always_names = [t.get("name", "") for t in always]

    final_assert = _find(
        always, "Fail this host if the agent-image build did not converge"
    )
    assert always_names.index(final_assert["name"]) == len(always_names) - 1, (
        "the fatal build assert must be the LAST task of the always "
        "section, after diagnostics capture and cleanup"
    )
    assert final_assert["ansible.builtin.assert"]["that"] == (
        "(agent_build.rc | default(1)) == 0"
    )
    for fragment in (
        "Diagnostics: docker buildx inspect",
        "Clean up old precis-agent build dirs",
    ):
        matches = [i for i, n in enumerate(always_names) if fragment in n]
        assert matches, f"no {fragment!r} task in the always section"
        assert matches[0] < always_names.index(final_assert["name"]), (
            f"{fragment!r} must run before the fatal assert, not be skipped "
            "by it (gr335099: diagnostics + cleanup precede the assert)"
        )


def test_build_report_diagnostics_and_assert_gated_on_agent_build() -> None:
    """td771340: the report, buildx diagnostics and convergence assert in
    ``always:`` fire on ANY block failure — but when an EARLIER block task
    fails (tarball copy/untar/wheel stage) ``agent_build`` was never
    registered, and ``default(1)`` reads would mask that task's real error
    behind a "did not converge / rc=absent / gr690411" message. All must be
    gated on ``agent_build is defined``; the controller-raise path still
    registers a failed result, so gr690411 coverage is preserved."""
    always = _build_always_tasks(_load_tasks())
    for fragment in (
        "Report agent-image build timing + progress tail",
        "Diagnostics: docker buildx inspect",
        "Diagnostics: buildx/buildkitd builder logs",
        "Report build failure diagnostics",
        "Fail this host if the agent-image build did not converge",
    ):
        task = _find(always, fragment)
        assert "agent_build is defined" in str(task.get("when", "")), (
            f"{fragment!r} must be gated on `agent_build is defined` — "
            "ungated, its default(1) read masks an earlier block failure "
            "behind a bogus build-failure/gr690411 message"
        )


def test_diagnostics_tasks_capture_buildx_inspect_and_builder_logs() -> None:
    """Both failure sites (pre-pull + build) must capture ``buildx inspect``
    and any buildx builder container logs — gr335099's "logs without a live
    operator" requirement."""
    tasks = _load_tasks()
    always_tasks = _build_always_tasks(tasks)

    for site, pool in (("pre-pull", tasks), ("build", always_tasks)):
        inspect_tasks = [
            t
            for t in pool
            if "Diagnostics" in t.get("name", "")
            and "buildx inspect" in t.get("name", "")
        ]
        log_tasks = [
            t
            for t in pool
            if "Diagnostics" in t.get("name", "")
            and "builder logs" in t.get("name", "")
        ]
        assert len(inspect_tasks) == 1, (
            f"{site}: expected one buildx-inspect diagnostics task"
        )
        assert len(log_tasks) == 1, (
            f"{site}: expected one builder-logs diagnostics task"
        )

        inspect_cmd = inspect_tasks[0]["ansible.builtin.command"]
        assert "buildx inspect" in inspect_cmd

        log_cmd = log_tasks[0]["ansible.builtin.shell"]
        assert "buildx_buildkit" in log_cmd
        assert ["docker", "logs"][
            0
        ] in log_cmd or "{{ _agent_container_bin }} logs" in log_cmd

        # Diagnostics must never redden the host themselves — they're
        # best-effort capture, not an assertion.
        assert inspect_tasks[0]["failed_when"] is False
        assert log_tasks[0]["failed_when"] is False
