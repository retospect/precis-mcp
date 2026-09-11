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
These tests pin that shape directly in the YAML so a future edit can't
silently drop the bound or the diagnostics capture.
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
    """The build task itself must NOT natively fail the host (``failed_when:
    false``) — failure is deferred to an explicit trailing assert so the
    diagnostics + existing cleanup tasks between them still run on a build
    that never converged, instead of the host halting mid-block with
    nothing captured and a stale build dir/tarball left behind."""
    block_tasks = _build_block_tasks(_load_tasks())
    names = [t.get("name", "") for t in block_tasks]

    build_task = _find(block_tasks, "docker build --target agent")
    assert build_task["failed_when"] is False

    assert "async" in build_task
    assert build_task["async"] == "{{ _agent_build_timeout_sec }}"

    build_idx = names.index(build_task["name"])
    assert any(
        "Diagnostics" in n and "build failure" in n for n in names[build_idx:]
    ), "no diagnostics task follows the build task"
    assert any(
        "Clean up old precis-agent build dirs" in n for n in names[build_idx:]
    ), "cleanup must still run after a failed build, not be skipped"

    final_assert = _find(
        block_tasks, "Fail this host if the agent-image build did not converge"
    )
    assert names.index(final_assert["name"]) == len(names) - 1, (
        "the fatal build assert must be the LAST task in the block, after "
        "diagnostics capture and cleanup"
    )
    assert final_assert["ansible.builtin.assert"]["that"] == (
        "(agent_build.rc | default(1)) == 0"
    )


def test_diagnostics_tasks_capture_buildx_inspect_and_builder_logs() -> None:
    """Both failure sites (pre-pull + build) must capture ``buildx inspect``
    and any buildx builder container logs — gr335099's "logs without a live
    operator" requirement."""
    tasks = _load_tasks()
    block_tasks = _build_block_tasks(tasks)

    for site, pool in (("pre-pull", tasks), ("build", block_tasks)):
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
