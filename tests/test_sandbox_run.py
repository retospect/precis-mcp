"""sandbox_run slice 1 — job_type + claude_docker executor.

Covers the buildable substrate against a **stub podman** (no live host):
the fail-closed submit gate, dispatch mint, node-pinned claim + lease,
the launch argv invariants, poll/reap by name (exit 0 → succeeded,
exit 1 / empty → failed + bubble, deadline → kill + swept:wall-timeout),
and the boot orphan reconcile.
"""

from __future__ import annotations

import os
import stat
import time
from pathlib import Path
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.handlers.todo import TodoHandler
from precis.store import Store
from precis.store.types import Tag
from precis.workers.dispatch import run_dispatch_pass
from precis.workers.executors import EXECUTOR_PROVIDES, claude_docker
from precis.workers.job_types import (
    get_job_type,
    known_job_types,
    sandbox_run,
)
from tests.conftest import id_of

# The DB-backed tests dominate; the pure unit tests (registry /
# validate_submit / argv) run fine under the same mark.
pytestmark = pytest.mark.db

# ── stub podman ────────────────────────────────────────────────────

_STUB = """#!/usr/bin/env python3
import os, sys
d = os.environ["SANDBOX_STUB_DIR"]
args = sys.argv[1:]
cmd = args[0] if args else ""
def sf(name): return os.path.join(d, name + ".state")
if cmd == "run":
    name = args[args.index("--name") + 1]
    with open(sf(name), "w") as f:
        f.write("running 0")
    print("ctr-" + name)
    sys.exit(0)
if cmd == "inspect":
    name = args[-1]
    p = sf(name)
    if not os.path.exists(p):
        sys.exit(1)
    sys.stdout.write(open(p).read().strip() + "\\n")
    sys.exit(0)
if cmd == "logs":
    print("stub log for " + args[-1])
    sys.exit(0)
if cmd == "kill":
    sys.exit(0)
if cmd == "rm":
    name = args[-1]
    p = sf(name)
    if os.path.exists(p):
        os.remove(p)
    sys.exit(0)
if cmd == "ps":
    for fn in sorted(os.listdir(d)):
        if fn.endswith(".state"):
            print(fn[: -len(".state")])
    sys.exit(0)
sys.exit(0)
"""


@pytest.fixture
def sandbox_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Wire a stub podman + the sandbox env, and return the stub state dir.

    A ``<name>.state`` file per container holds ``"<status> <exit>"``;
    ``inspect`` reads it, ``run`` seeds ``"running 0"``, ``rm`` deletes
    it, ``ps`` lists them. Tests set a terminal state by writing the file.
    """
    stub = tmp_path / "podman-stub"
    stub.write_text(_STUB, encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    monkeypatch.setenv("PRECIS_PODMAN_BIN", str(stub))
    monkeypatch.setenv("SANDBOX_STUB_DIR", str(state_dir))
    monkeypatch.setenv("PRECIS_SANDBOX_WORK_DIR", str(tmp_path / "work"))
    monkeypatch.setenv("PRECIS_SANDBOX_HOSTS", "balthazar spark")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-test-token")
    monkeypatch.setenv("PRECIS_NODE", "balthazar")
    # Skip the once-per-process boot reconcile unless a test opts in.
    monkeypatch.setattr(claude_docker, "_reconciled", True)
    return state_dir


# ── helpers ────────────────────────────────────────────────────────


def _valid_params(**over: Any) -> dict[str, Any]:
    p = {
        "prompt": "write a python script that prints hello",
        "target_node": "balthazar",
        "resources": {"wall_seconds": 1800},
    }
    p.update(over)
    return p


def _valid_run_params(*, artifact: int, **over: Any) -> dict[str, Any]:
    """mode:run params — no ``prompt`` (mode:build's field), ``artifact``
    is the prior build's harvested ``folder`` ref id."""
    p: dict[str, Any] = {
        "mode": "run",
        "artifact": artifact,
        "target_node": "balthazar",
        "resources": {"wall_seconds": 1800},
    }
    p.update(over)
    return p


def _mk_queued_job(
    store: Store, *, params: dict[str, Any], parent_id: int | None = None
) -> int:
    """Insert a queued claude_docker/sandbox_run job (as dispatch would)."""
    ref = store.insert_ref(
        kind="job",
        slug=None,
        title="sandbox_run job",
        meta={
            "executor": "claude_docker",
            "job_type": "sandbox_run",
            "params": params,
        },
        parent_id=parent_id,
    )
    store.add_tag(ref.id, Tag.parse_strict("STATUS:queued"), set_by="agent")
    return int(ref.id)


def _status(store: Store, ref_id: int) -> str | None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT t.value FROM ref_tags rt JOIN tags t USING (tag_id) "
            "WHERE rt.ref_id = %s AND t.namespace = 'STATUS'",
            (ref_id,),
        ).fetchone()
    return row[0] if row else None


def _meta(store: Store, ref_id: int) -> dict[str, Any]:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    assert row is not None
    return dict(row[0] or {})


def _tags(store: Store, ref_id: int) -> set[str]:
    return {str(t) for t in store.tags_for(ref_id)}


def _job_summary_texts(store: Store, ref_id: int) -> list[str]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT text FROM chunks WHERE ref_id = %s AND chunk_kind = 'job_summary' "
            "ORDER BY ord",
            (ref_id,),
        ).fetchall()
    return [r[0] for r in rows]


# ── registry / metadata (no DB) ────────────────────────────────────


def test_registered_as_builtin() -> None:
    assert "sandbox_run" in known_job_types()
    spec = get_job_type("sandbox_run")
    assert spec is not None
    assert spec.compatible_executors == frozenset({"claude_docker"})
    assert spec.validate_submit is sandbox_run.validate_submit


def test_executor_provides() -> None:
    assert "claude_docker" in EXECUTOR_PROVIDES
    assert EXECUTOR_PROVIDES["claude_docker"] >= sandbox_run.REQUIRES


def test_resolve_model_uses_frontier(monkeypatch: pytest.MonkeyPatch) -> None:
    from precis.utils.llm.router import Tier, resolve_model

    monkeypatch.delenv("PRECIS_SANDBOX_MODEL", raising=False)
    assert sandbox_run.resolve_sandbox_model() == resolve_model(Tier.FRONTIER)
    monkeypatch.setenv("PRECIS_SANDBOX_MODEL", "claude-custom-9")
    assert sandbox_run.resolve_sandbox_model() == "claude-custom-9"


def test_compose_prompt_has_task_and_harvest_contract() -> None:
    body = sandbox_run.compose_prompt("do the thing")
    assert "do the thing" in body
    assert "/work/out" in body
    assert "uv.lock" in body


def test_resolve_wall_seconds_prefers_nested_falls_back_to_legacy_flat() -> None:
    """The shared job wall-clock budget key: current writers nest it under
    ``resources`` (matching ssh_node/coordinator/quest.compute); a job row
    minted before that migration carried it flat at ``params.wall_seconds``
    — the read-both shim must still honor that in-flight shape."""
    assert sandbox_run.resolve_wall_seconds({"resources": {"wall_seconds": 900}}) == 900
    assert sandbox_run.resolve_wall_seconds({"wall_seconds": 900}) == 900
    # nested wins when (implausibly) both are present.
    assert (
        sandbox_run.resolve_wall_seconds(
            {"resources": {"wall_seconds": 900}, "wall_seconds": 1}
        )
        == 900
    )
    assert sandbox_run.resolve_wall_seconds({}) is None


# ── validate_submit fail-closed gate ───────────────────────────────


class TestValidateSubmit:
    """Each fail-closed case is rejected with a clear message; a fully
    valid submit passes."""

    def _env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PRECIS_SANDBOX_HOSTS", "balthazar spark")
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")

    def test_valid_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._env(monkeypatch)
        assert sandbox_run.validate_submit(None, params=_valid_params()) is None

    def test_rejects_mode_run_without_artifact(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # mode:run is supported, but needs params.artifact (a prior
        # build's harvested folder ref id) — reject without one.
        self._env(monkeypatch)
        err = sandbox_run.validate_submit(None, params=_valid_params(mode="run"))
        assert err is not None and "artifact" in err and "mode:run" in err

    def test_mode_run_with_artifact_passes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._env(monkeypatch)
        params = _valid_run_params(artifact=42)
        err = sandbox_run.validate_submit(None, params=params)
        assert err is None

    def test_rejects_mode_build_without_prompt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._env(monkeypatch)
        params = _valid_params()
        del params["prompt"]
        err = sandbox_run.validate_submit(None, params=params)
        assert err is not None and "prompt" in err

    def test_rejects_unsupported_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._env(monkeypatch)
        err = sandbox_run.validate_submit(None, params=_valid_params(mode="parallel"))
        assert err is not None and "mode" in err

    def test_rejects_precis_access_read_without_read_mcp_flag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._env(monkeypatch)
        monkeypatch.delenv("PRECIS_SANDBOX_READ_MCP", raising=False)
        err = sandbox_run.validate_submit(
            None, params=_valid_params(precis_access="read")
        )
        assert err is not None and "PRECIS_SANDBOX_READ_MCP" in err

    def test_accepts_precis_access_read_with_read_mcp_flag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._env(monkeypatch)
        monkeypatch.setenv("PRECIS_SANDBOX_READ_MCP", "1")
        err = sandbox_run.validate_submit(
            None, params=_valid_params(precis_access="read")
        )
        assert err is None

    def test_rejects_unsupported_precis_access_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._env(monkeypatch)
        err = sandbox_run.validate_submit(
            None, params=_valid_params(precis_access="write")
        )
        assert err is not None and "precis_access" in err

    def test_rejects_secrets(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._env(monkeypatch)
        err = sandbox_run.validate_submit(
            None, params=_valid_params(secrets=["OPENAI_KEY"])
        )
        assert err is not None and "secrets" in err

    def test_rejects_flag_like_image(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # gr179503: an ``image`` beginning with ``-`` would be parsed by
        # podman's cobra parser as a flag, not the IMAGE positional.
        self._env(monkeypatch)
        for bad in ("--privileged", "-v/etc:/etc", "code task", "a;b", "", "img\n"):
            err = sandbox_run.validate_submit(None, params=_valid_params(image=bad))
            assert err is not None and "image" in err, f"{bad!r} should be rejected"

    def test_accepts_valid_image_refs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._env(monkeypatch)
        for ok in (
            "code-task:latest",
            "localhost:5000/ns/name:tag",
            "repo@sha256:" + "a" * 64,
        ):
            assert (
                sandbox_run.validate_submit(None, params=_valid_params(image=ok))
                is None
            ), f"{ok!r} should pass"

    def test_rejects_melchior(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._env(monkeypatch)
        # Even if an operator mistakenly allowlists it.
        monkeypatch.setenv("PRECIS_SANDBOX_HOSTS", "melchior balthazar")
        err = sandbox_run.validate_submit(
            None, params=_valid_params(target_node="melchior")
        )
        assert err is not None and "melchior" in err

    def test_rejects_non_allowlisted_node(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._env(monkeypatch)
        err = sandbox_run.validate_submit(
            None, params=_valid_params(target_node="randombox")
        )
        assert err is not None and "agent_sandbox_host" in err

    def test_rejects_missing_oauth_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PRECIS_SANDBOX_HOSTS", "balthazar")
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        err = sandbox_run.validate_submit(None, params=_valid_params())
        assert err is not None and "CLAUDE_CODE_OAUTH_TOKEN" in err


# ── launch argv invariants (pure) ──────────────────────────────────


def test_build_run_argv_invariants() -> None:
    argv = claude_docker.build_run_argv(
        podman_bin="podman",
        job_id=42,
        image="code-task:abc",
        work_dir="/tmp/precis-sandbox/sandbox-42",
        model="claude-opus-4-7",
        memory="8g",
        cpus="2",
        pids_limit=512,
        network="bridge",
    )
    joined = " ".join(argv)
    # detached + deterministic name
    assert "-d" in argv
    assert argv[argv.index("--name") + 1] == "sandbox-42"
    # OAuth token by KEY only (value inherited from env; never in argv)
    assert "CLAUDE_CODE_OAUTH_TOKEN" in argv
    assert not any(a.startswith("CLAUDE_CODE_OAUTH_TOKEN=") for a in argv)
    # no --bare, no ANTHROPIC_API_KEY
    assert "--bare" not in argv
    assert "ANTHROPIC_API_KEY" not in joined
    # cgroup caps present
    assert "--memory" in argv and "--cpus" in argv and "--pids-limit" in argv
    # never a GPU
    assert "--device" not in argv
    # the image is the final token, pinned as the IMAGE positional by a
    # ``--`` end-of-options sentinel (gr179503)
    assert argv[-1] == "code-task:abc"
    assert argv[-2] == "--"


def test_build_rerun_argv_invariants() -> None:
    argv = claude_docker.build_rerun_argv(
        podman_bin="podman",
        job_id=42,
        image="code-task:abc",
        work_dir="/tmp/precis-sandbox/sandbox-42",
        cmd="python main.py",
        memory="8g",
        cpus="2",
        pids_limit=512,
        network="bridge",
    )
    joined = " ".join(argv)
    assert "-d" in argv
    assert argv[argv.index("--name") + 1] == "sandbox-42"
    # No claude / OAuth / API-key env at all — mode:run spawns no claude.
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in joined
    assert "ANTHROPIC_API_KEY" not in joined
    assert "--env" not in argv
    # cgroup caps present, no GPU.
    assert "--memory" in argv and "--cpus" in argv and "--pids-limit" in argv
    assert "--device" not in argv
    # explicit CMD override: uv sync then the RUN.json.cmd, from /work.
    assert argv[-3:] == ["sh", "-c", "cd /work && uv sync && python main.py"]
    assert argv[argv.index("--") + 1] == "code-task:abc"


# ── dispatch mint (acceptance #1) ──────────────────────────────────


@pytest.fixture
def handler(hub: Hub) -> TodoHandler:
    return TodoHandler(hub=hub)


def test_dispatch_mints_node_pinned_queued_job(
    handler: TodoHandler, store: Store, sandbox_env: Path
) -> None:
    r = handler.put(
        text="run a coding task in the sandbox",
        meta={
            "executor": "claude_docker",
            "job_type": "sandbox_run",
            "params": _valid_params(),
        },
    )
    rid = id_of(r.body)
    result = run_dispatch_pass(store)
    assert result.claimed == 1

    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT ref_id, meta FROM refs WHERE parent_id = %s AND kind = 'job'",
            (rid,),
        ).fetchone()
    assert row is not None
    job_id, meta = int(row[0]), dict(row[1])
    assert meta["job_type"] == "sandbox_run"
    assert meta["executor"] == "claude_docker"
    assert meta["params"]["target_node"] == "balthazar"
    assert "STATUS:queued" in _tags(store, job_id)
    # Not self-resolving → dispatch injects child_job_succeeded so the
    # parent todo closes on a clean run.
    assert _meta(store, rid).get("auto_check", {}).get("type") == "child_job_succeeded"


def test_put_time_validate_rejects_mode_run_without_artifact(
    hub: Hub, store: Store, sandbox_env: Path
) -> None:
    """A direct job put with a fail-closed param is rejected at put time."""
    from precis.errors import BadInput
    from precis.handlers.job import JobHandler

    parent = store.insert_ref(kind="todo", slug=None, title="owner", meta={})
    with pytest.raises(BadInput, match="artifact"):
        JobHandler(hub=hub).put(
            job_type="sandbox_run",
            parent_id=parent.id,
            params=_valid_params(mode="run"),
        )


def test_put_time_validate_accepts_mode_run_with_artifact(
    hub: Hub, store: Store, sandbox_env: Path
) -> None:
    from precis.handlers.job import JobHandler

    parent = store.insert_ref(kind="todo", slug=None, title="owner", meta={})
    r = JobHandler(hub=hub).put(
        job_type="sandbox_run",
        parent_id=parent.id,
        params=_valid_run_params(artifact=42),
    )
    assert r is not None


# ── claim + node gate + lease (acceptance #3) ──────────────────────


def test_claim_is_node_pinned_and_leased(
    store: Store, sandbox_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jid = _mk_queued_job(store, params=_valid_params(target_node="spark"))
    # This worker is balthazar — must not claim a spark-pinned job.
    monkeypatch.setenv("PRECIS_NODE", "balthazar")
    claude_docker.run_claude_docker_pass(store, limit=4)
    assert _status(store, jid) == "queued"

    # spark's worker claims + launches it, writing a wall-sized lease.
    monkeypatch.setenv("PRECIS_NODE", "spark")
    claude_docker.run_claude_docker_pass(store, limit=4)
    assert _status(store, jid) == "running"
    meta = _meta(store, jid)
    assert meta["container"] == f"sandbox-{jid}"
    assert "lease_until" in meta
    assert meta["run_host"] == "spark"


def test_legacy_flat_wall_seconds_row_still_claims_and_launches(
    store: Store, sandbox_env: Path
) -> None:
    """A job row minted before the nested-``resources`` migration (flat
    ``params.wall_seconds``) — never re-submitted, so never re-validated
    against the current schema — must still lease + launch correctly via
    the read-both shim (:func:`sandbox_run.resolve_wall_seconds`)."""
    legacy_params = _valid_params()
    legacy_params["wall_seconds"] = legacy_params.pop("resources")["wall_seconds"]
    jid = _mk_queued_job(store, params=legacy_params)
    claude_docker.run_claude_docker_pass(store, limit=4)
    assert _status(store, jid) == "running"
    meta = _meta(store, jid)
    assert meta["container"] == f"sandbox-{jid}"
    assert meta["deadline"] > 0
    assert "lease_until" in meta


# ── Worker boot epoch: reclaim + re-adopt (the lease-epoch reclaim arm) ──


def _mk_running_job_with_container(
    store: Store,
    *,
    jid_params: dict[str, Any],
    lease_boot_id: str,
    lease_process: str,
    lease_host: str,
) -> int:
    """A STATUS:running claude_docker job that already has a launched
    container stamped (as if a PRIOR worker generation's ``_launch``
    completed, then that generation died before the job reached a
    terminal STATUS) — stands in for the narrow race the epoch arm
    closes for this executor."""
    ref = store.insert_ref(
        kind="job",
        slug=None,
        title="sandbox_run job (orphaned mid-run)",
        meta={
            "executor": "claude_docker",
            "job_type": "sandbox_run",
            "params": jid_params,
        },
    )
    jid = int(ref.id)
    name = claude_docker.container_name(jid)
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET meta = meta || jsonb_build_object("
            "  'container', %s::text,"
            "  'container_id', 'ctr-prior'::text,"
            "  'run_host', %s::text,"
            "  'deadline', %s::float,"
            "  'lease_until', (now() + interval '1 hour')::text,"
            "  'lease_boot_id', %s::text,"
            "  'lease_process', %s::text,"
            "  'lease_host', %s::text"
            ") WHERE ref_id = %s",
            (
                name,
                jid_params.get("target_node") or "",
                time.time() + 3600,
                lease_boot_id,
                lease_process,
                lease_host,
                jid,
            ),
        )
        conn.commit()
    store.add_tag(ref.id, Tag.parse_strict("STATUS:running"), set_by="agent")
    return jid


def test_epoch_reclaim_re_adopts_live_container_without_relaunch(
    store: Store, sandbox_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A running job whose stamped generation has been replaced (deploy
    bounce) is reclaimed via the epoch arm — but since it already has a
    launched, still-alive container, the pass must RE-ADOPT (resume
    polling) rather than relaunch under the same name (this branch is
    REQUIRED — see claude_docker._claim's docstring)."""
    store.record_heartbeat(
        "spark-docker-epoch", meta={"boot_ids": {"claude_docker": "new-gen"}}
    )
    jid = _mk_running_job_with_container(
        store,
        jid_params=_valid_params(target_node="spark"),
        lease_boot_id="dead-gen",
        lease_process="claude_docker",
        lease_host="spark-docker-epoch",
    )
    # Seed the stub container as still running under this job's name.
    (sandbox_env / f"{claude_docker.container_name(jid)}.state").write_text(
        "running 0", encoding="utf-8"
    )
    monkeypatch.setenv("PRECIS_NODE", "spark")

    launch_calls: list[int] = []

    def _fake_launch(store_: Any, ref_id: int, meta: Any, node: Any) -> bool:
        launch_calls.append(ref_id)
        return True

    monkeypatch.setattr(claude_docker, "_launch_safe", _fake_launch)

    result = claude_docker.run_claude_docker_pass(store, limit=4)

    assert launch_calls == []  # never relaunched under the reclaimed name
    assert result["claimed"] == 1
    assert result["ok"] == 1
    assert _status(store, jid) == "running"  # still tracked, not terminalized
    summaries_and_events = [
        c.text
        for c in store.chunks.list_chunks_for_ref(jid)
        if getattr(c, "chunk_kind", None) == "job_event"
    ]
    assert any("re-adopted" in t for t in summaries_and_events)
    # The claim re-stamped THIS worker's own lease identity (host, at least)
    # over the dead generation's stamp.
    assert _meta(store, jid)["lease_host"] != "spark-docker-epoch"


def test_poison_guard_fails_past_max_attempts(
    store: Store, sandbox_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A container-launch job re-claimed past the shared attempt cap is
    failed (bubbled), never (re-)launched — §H piece 3, generalized from
    ssh_node's original guard, now closing claude_docker's own gap."""
    from precis.workers.executors._common import MAX_ATTEMPTS

    ref = store.insert_ref(
        kind="job",
        slug=None,
        title="sandbox_run job (crash-looping)",
        meta={
            "executor": "claude_docker",
            "job_type": "sandbox_run",
            "params": _valid_params(),
            "attempts": MAX_ATTEMPTS,
        },
    )
    jid = int(ref.id)
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET meta = meta || jsonb_build_object("
            "  'lease_until', (now() - interval '1 hour')::text"
            ") WHERE ref_id = %s",
            (jid,),
        )
        conn.commit()
    store.add_tag(ref.id, Tag.parse_strict("STATUS:running"), set_by="agent")

    launch_calls: list[int] = []

    def _fake_launch(store_: Any, ref_id: int, meta: Any, node: Any) -> bool:
        launch_calls.append(ref_id)
        return True

    monkeypatch.setattr(claude_docker, "_launch_safe", _fake_launch)

    result = claude_docker.run_claude_docker_pass(store, limit=4)

    assert launch_calls == []
    assert result["claimed"] == 1
    assert result["ok"] == 0
    assert result["failed"] == 1
    assert _status(store, jid) == "failed"
    assert _meta(store, jid)["failure_class"] == "infra"


def test_concurrency_cap(
    store: Store, sandbox_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_SANDBOX_CONCURRENCY", "1")
    j1 = _mk_queued_job(store, params=_valid_params())
    j2 = _mk_queued_job(store, params=_valid_params())
    claude_docker.run_claude_docker_pass(store, limit=4)
    running = [j for j in (j1, j2) if _status(store, j) == "running"]
    queued = [j for j in (j1, j2) if _status(store, j) == "queued"]
    assert len(running) == 1 and len(queued) == 1


# ── launch argv actually used + poll/reap (acceptance #4/#5) ───────


def test_launch_records_container_and_deadline(store: Store, sandbox_env: Path) -> None:
    jid = _mk_queued_job(store, params=_valid_params())
    claude_docker.run_claude_docker_pass(store, limit=4)
    meta = _meta(store, jid)
    assert meta["container"] == f"sandbox-{jid}"
    assert meta["deadline"] > 0
    # PROMPT.md staged into the /work dir.
    work = Path(os.environ["PRECIS_SANDBOX_WORK_DIR"]) / f"sandbox-{jid}"
    assert (work / "PROMPT.md").exists()


# ── GLM/OpenRouter fleet-flip safety gate (Part 3) ─────────────────
#
# claude_docker._launch spawns a raw `claude` CLI in the container whose
# --model comes from resolve_sandbox_model() (-> resolve_model(Tier.FRONTIER))
# — under backend=openai that's an OSS slug the claude CLI can't run. The
# gate must skip *before* `podman run` (no container, no subprocess).


def test_launch_skips_under_openai_backend(
    store: Store, sandbox_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from precis.utils.llm.router import Backend

    monkeypatch.setattr(claude_docker, "resolve_backend", lambda: Backend.OPENAI)
    parent = store.insert_ref(kind="todo", slug=None, title="owner", meta={})
    jid = _mk_queued_job(store, params=_valid_params(), parent_id=parent.id)
    claude_docker.run_claude_docker_pass(store, limit=4)
    # Cleanly cancelled, not left queued and not failed — no podman run,
    # no container ever recorded.
    assert _status(store, jid) == "cancelled"
    assert "container" not in _meta(store, jid)
    work = Path(os.environ["PRECIS_SANDBOX_WORK_DIR"]) / f"sandbox-{jid}"
    assert not (work / "PROMPT.md").exists()
    # A config-mismatch skip is not a job failure — no bubble to the parent.
    assert not any(t.startswith("child-failed:") for t in _tags(store, parent.id))


def test_launch_proceeds_under_default_anthropic_backend(
    store: Store, sandbox_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bookend of the skip test above: with the gate explicitly resolved to
    the default Anthropic backend, launch proceeds exactly as before the
    gate existed (mirrors test_launch_records_container_and_deadline)."""
    from precis.utils.llm.router import Backend

    monkeypatch.setattr(claude_docker, "resolve_backend", lambda: Backend.ANTHROPIC)
    jid = _mk_queued_job(store, params=_valid_params())
    claude_docker.run_claude_docker_pass(store, limit=4)
    meta = _meta(store, jid)
    assert meta["container"] == f"sandbox-{jid}"
    assert _status(store, jid) == "running"


def test_poll_exit_zero_succeeds(store: Store, sandbox_env: Path) -> None:
    parent = store.insert_ref(kind="todo", slug=None, title="owner", meta={})
    jid = _mk_queued_job(store, params=_valid_params(), parent_id=parent.id)
    claude_docker.run_claude_docker_pass(store, limit=4)  # launch
    # Container reports a clean exit.
    (sandbox_env / f"sandbox-{jid}.state").write_text("exited 0", encoding="utf-8")
    claude_docker.run_claude_docker_pass(store, limit=4)  # poll → reap
    assert _status(store, jid) == "succeeded"
    # Parent not bubbled.
    assert not any(t.startswith("child-failed:") for t in _tags(store, parent.id))


# ── per-unit pinned image provenance ────────────────────────────────


def test_custom_image_lands_in_argv_meta_summary_and_harvest_folder(
    store: Store, sandbox_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_ROOT", str(sandbox_env.parent / "PRECIS_ROOT"))
    monkeypatch.setenv(
        "PRECIS_SANDBOX_ARTIFACT_ROOT", str(sandbox_env.parent / "artifacts")
    )
    custom_image = "code-task:deadbeef01"
    captured: dict[str, Any] = {}
    real_build_run_argv = claude_docker.build_run_argv

    def _spy(**kw: Any) -> list[str]:
        captured["image"] = kw["image"]
        return real_build_run_argv(**kw)

    monkeypatch.setattr(claude_docker, "build_run_argv", _spy)

    jid = _mk_queued_job(store, params=_valid_params(image=custom_image))
    claude_docker.run_claude_docker_pass(store, limit=4)  # launch
    assert captured["image"] == custom_image
    assert _meta(store, jid)["image"] == custom_image

    # Leave a file in out/ so harvest actually mints a folder.
    work = Path(os.environ["PRECIS_SANDBOX_WORK_DIR"]) / f"sandbox-{jid}"
    (work / "out" / "main.py").write_text("print('hi')\n", encoding="utf-8")

    (sandbox_env / f"sandbox-{jid}.state").write_text("exited 0", encoding="utf-8")
    claude_docker.run_claude_docker_pass(store, limit=4)  # poll → harvest → reap

    assert _status(store, jid) == "succeeded"
    summaries = _job_summary_texts(store, jid)
    assert any(f"image={custom_image}" in s for s in summaries)

    folder_id = _meta(store, jid)["harvest_folder_id"]
    assert _meta(store, folder_id)["image"] == custom_image


def test_poll_exit_one_fails_and_bubbles(store: Store, sandbox_env: Path) -> None:
    parent = store.insert_ref(kind="todo", slug=None, title="owner", meta={})
    jid = _mk_queued_job(store, params=_valid_params(), parent_id=parent.id)
    claude_docker.run_claude_docker_pass(store, limit=4)  # launch
    (sandbox_env / f"sandbox-{jid}.state").write_text("exited 1", encoding="utf-8")
    claude_docker.run_claude_docker_pass(store, limit=4)  # poll → reap
    assert _status(store, jid) == "failed"
    assert any(t.startswith("child-failed:") for t in _tags(store, parent.id))


def test_poll_deadline_kills_and_sweeps(store: Store, sandbox_env: Path) -> None:
    jid = _mk_queued_job(store, params=_valid_params())
    claude_docker.run_claude_docker_pass(store, limit=4)  # launch (still running)
    # Force the deadline into the past; container is still "running".
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET meta = meta || jsonb_build_object('deadline', 1::float) "
            "WHERE ref_id = %s",
            (jid,),
        )
        conn.commit()
    claude_docker.run_claude_docker_pass(store, limit=4)  # poll → wall-timeout
    assert _status(store, jid) == "failed"
    assert "swept:wall-timeout" in _tags(store, jid)


def test_poll_kill_requested_kills_and_sweeps(store: Store, sandbox_env: Path) -> None:
    """§B-2 operator kill backstop, claude_docker's smaller shape (no
    ctx/spec.kill — it just ``_terminate``s the container): a container
    still ``running`` (i.e. would never trip the deadline) is still
    force-terminated once ``meta.kill_requested`` is stamped, ahead of the
    deadline check."""
    parent = store.insert_ref(kind="todo", slug=None, title="owner", meta={})
    jid = _mk_queued_job(store, params=_valid_params(), parent_id=parent.id)
    claude_docker.run_claude_docker_pass(store, limit=4)  # launch (still running)
    assert _status(store, jid) == "running"
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET meta = meta || jsonb_build_object("
            "'kill_requested', jsonb_build_object("
            "'at', '2026-01-01T00:00:00+00:00', 'actor', 'operator', "
            "'note', 'stuck sandbox run')) WHERE ref_id = %s",
            (jid,),
        )
        conn.commit()
    claude_docker.run_claude_docker_pass(store, limit=4)  # poll → operator kill
    assert _status(store, jid) == "failed"
    assert "swept:killed-by-operator" in _tags(store, jid)
    summaries = _job_summary_texts(store, jid)
    assert any(
        "killed by operator" in s and "stuck sandbox run" in s for s in summaries
    )
    assert any(t.startswith("child-failed:") for t in _tags(store, parent.id))
    # Container reaped (state file gone).
    assert not (sandbox_env / f"sandbox-{jid}.state").exists()


def test_poll_missing_container_fails(store: Store, sandbox_env: Path) -> None:
    jid = _mk_queued_job(store, params=_valid_params())
    claude_docker.run_claude_docker_pass(store, limit=4)  # launch
    # Container vanished out from under us.
    (sandbox_env / f"sandbox-{jid}.state").unlink()
    claude_docker.run_claude_docker_pass(store, limit=4)  # poll
    assert _status(store, jid) == "failed"


# ── boot reconcile (acceptance #6) ─────────────────────────────────


def test_reap_bin_falls_back_to_docker_when_no_podman(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mirrors ``test_capability_probe.py``'s docker-fallback case, scoped to
    the claude_docker reap path specifically: on a docker-only host (no
    podman on PATH — e.g. spark) polling/reaping must resolve to docker
    instead of the boot ``reconcile_orphans`` throwing
    ``FileNotFoundError('podman')`` every worker restart. The launch path
    (``_podman_bin``) stays hardcoded podman regardless — rootless podman is
    a deliberate security choice for the sandbox's untrusted compute, not
    just a default."""
    for v in ("PRECIS_CONTAINER_BIN", "PRECIS_PODMAN_BIN", "PRECIS_PODMAN_SLOTS"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(
        "precis.workers.capability_probe.shutil.which",
        lambda name: "/usr/bin/docker" if name == "docker" else None,
    )
    assert claude_docker._reap_bin() == "docker"
    assert claude_docker._podman_bin() == "podman"


def test_reap_bin_prefers_podman_when_both_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for v in ("PRECIS_CONTAINER_BIN", "PRECIS_PODMAN_BIN", "PRECIS_PODMAN_SLOTS"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(
        "precis.workers.capability_probe.shutil.which",
        lambda name: f"/usr/bin/{name}" if name in {"podman", "docker"} else None,
    )
    assert claude_docker._reap_bin() == "podman"


def test_reconcile_reaps_orphan(store: Store, sandbox_env: Path) -> None:
    # A sandbox-* container with no owning job at all.
    (sandbox_env / "sandbox-999999.state").write_text("running 0", encoding="utf-8")
    reaped = claude_docker.reconcile_orphans(store)
    assert reaped == 1
    assert not (sandbox_env / "sandbox-999999.state").exists()


def test_reconcile_keeps_live_job(store: Store, sandbox_env: Path) -> None:
    jid = _mk_queued_job(store, params=_valid_params())
    (sandbox_env / f"sandbox-{jid}.state").write_text("running 0", encoding="utf-8")
    reaped = claude_docker.reconcile_orphans(store)
    assert reaped == 0
    assert (sandbox_env / f"sandbox-{jid}.state").exists()


# ── launch-time fail-closed (defence in depth for dispatch path) ───


def test_launch_rejects_bad_params_without_container(
    store: Store, sandbox_env: Path
) -> None:
    # A job minted with mode:run but no params.artifact (bypassing
    # put-time validate) must be failed at launch, never started.
    jid = _mk_queued_job(store, params=_valid_params(mode="run"))
    claude_docker.run_claude_docker_pass(store, limit=4)
    assert _status(store, jid) == "failed"
    assert "container" not in _meta(store, jid)


# ── mode:run — stage a prior build's tarball, launch, harvest result ──
#
# Design §"Re-run + operationalize": same substrate, claude swapped out.
# These tests harvest a fake build directly (no container needed — the
# harvest module never shells out) to get a real ``folder`` ref with a
# ``RUN.json`` recipe + tarball to stage from, then drive the executor
# exactly like the mode:build tests above.


@pytest.fixture
def sandbox_run_env(
    sandbox_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Extends ``sandbox_env`` with ``PRECIS_ROOT`` +
    ``PRECIS_SANDBOX_ARTIFACT_ROOT`` so a build harvest and a run's
    staging round-trip through the same stores."""
    precis_root = tmp_path / "PRECIS_ROOT"
    precis_root.mkdir()
    art_root = tmp_path / "artifacts"
    monkeypatch.setenv("PRECIS_ROOT", str(precis_root))
    monkeypatch.setenv("PRECIS_SANDBOX_ARTIFACT_ROOT", str(art_root))
    return sandbox_env


def _mk_build_folder(store: Store, *, with_run_json: bool = True) -> int:
    """Harvest a fake ``mode:build`` output directly (no container
    needed) — the harvested ``folder`` ref ``mode:run`` stages from."""
    from precis.workers.executors import _sandbox_harvest as harvest

    job = store.insert_ref(
        kind="job",
        slug=None,
        title="build job",
        meta={"executor": "claude_docker", "job_type": "sandbox_run"},
    )
    work_dir = Path(os.environ["PRECIS_SANDBOX_WORK_DIR"]) / "sandbox-build-1"
    out_dir = work_dir / "out"
    out_dir.mkdir(parents=True)
    (out_dir / "main.py").write_text(
        "print('hello from the build')\n", encoding="utf-8"
    )
    if with_run_json:
        (out_dir / "RUN.json").write_text(
            '{"cmd": "python main.py", "inputs": [], "outputs": [], '
            '"image": "code-task:abc"}',
            encoding="utf-8",
        )
    result = harvest.harvest_out(
        store,
        job_ref_id=job.id,
        container_name="sandbox-build-1",
        work_dir=work_dir,
        image="code-task:abc",
        model="claude-opus-4-7",
    )
    assert result.folder_ref_id is not None
    return result.folder_ref_id


def test_launch_run_stages_artifact_no_prompt_no_oauth_env(
    store: Store, sandbox_run_env: Path
) -> None:
    build_folder_id = _mk_build_folder(store)
    jid = _mk_queued_job(store, params=_valid_run_params(artifact=build_folder_id))
    claude_docker.run_claude_docker_pass(store, limit=4)  # launch

    meta = _meta(store, jid)
    assert meta["container"] == f"sandbox-{jid}"
    assert meta["run_of_folder_id"] == build_folder_id
    assert _status(store, jid) == "running"

    work = Path(os.environ["PRECIS_SANDBOX_WORK_DIR"]) / f"sandbox-{jid}"
    # mode:run never gets a prompt.
    assert not (work / "PROMPT.md").exists()
    # The staged tree is the FAITHFUL tarball copy (original names) at
    # the /work root, not the renamed plaintext projection.
    assert (work / "main.py").read_text(
        encoding="utf-8"
    ) == "print('hello from the build')\n"
    assert (work / "RUN.json").is_file()


def test_launch_run_missing_folder_fails(store: Store, sandbox_run_env: Path) -> None:
    jid = _mk_queued_job(store, params=_valid_run_params(artifact=999999))
    claude_docker.run_claude_docker_pass(store, limit=4)
    assert _status(store, jid) == "failed"
    assert "container" not in _meta(store, jid)


def test_launch_run_folder_without_run_json_fails(
    store: Store, sandbox_run_env: Path
) -> None:
    build_folder_id = _mk_build_folder(store, with_run_json=False)
    jid = _mk_queued_job(store, params=_valid_run_params(artifact=build_folder_id))
    claude_docker.run_claude_docker_pass(store, limit=4)
    assert _status(store, jid) == "failed"
    assert "container" not in _meta(store, jid)


def test_poll_run_exit_zero_harvests_result_and_links_run_of(
    store: Store, sandbox_run_env: Path
) -> None:
    build_folder_id = _mk_build_folder(store)
    jid = _mk_queued_job(store, params=_valid_run_params(artifact=build_folder_id))
    claude_docker.run_claude_docker_pass(store, limit=4)  # launch

    work = Path(os.environ["PRECIS_SANDBOX_WORK_DIR"]) / f"sandbox-{jid}"
    (work / "out" / "RESULT.md").write_text("42\n", encoding="utf-8")
    (sandbox_run_env / f"sandbox-{jid}.state").write_text("exited 0", encoding="utf-8")
    claude_docker.run_claude_docker_pass(store, limit=4)  # poll -> harvest -> reap

    assert _status(store, jid) == "succeeded"
    jmeta = _meta(store, jid)
    run_folder_id = jmeta["harvest_folder_id"]
    assert run_folder_id != build_folder_id

    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT dst_ref_id FROM links "
            "WHERE relation = 'derived-from' AND src_ref_id = %s",
            (run_folder_id,),
        ).fetchall()
    dsts = {r[0] for r in rows}
    assert jid in dsts  # derived-from the run job itself
    assert build_folder_id in dsts  # AND derived-from the build it re-ran


# ── recurring mode:run via meta.schedule (mint path only) ─────────────


def test_recurring_mints_mode_run_child_with_same_params(
    handler: TodoHandler, store: Store, sandbox_run_env: Path
) -> None:
    """A ``mode:run`` recurring under ``meta.schedule`` mints successive
    run jobs — this only exercises the existing generic schedule
    spawner's mint path (``worker._mint_child_conn`` already carries
    ``executor``/``job_type``/``params`` onto the spawned child; no new
    scheduler logic is added for sandbox_run)."""
    from datetime import UTC, datetime, timedelta

    from precis.workers.schedule import run_schedule_pass

    build_folder_id = _mk_build_folder(store)
    resp = handler.put(
        text="nightly re-run",
        meta={
            "schedule": {"cron": "0 0 * * *"},
            "executor": "claude_docker",
            "job_type": "sandbox_run",
            "params": _valid_run_params(
                artifact=build_folder_id, target_node="balthazar"
            ),
        },
    )
    rec_id = id_of(resp.body)
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO ref_events (ref_id, source, event, ts, payload) "
            "VALUES (%s, 'schedule', 'spawn', %s, '{}'::jsonb)",
            (rec_id, datetime.now(UTC) - timedelta(hours=25)),
        )
        conn.commit()

    result = run_schedule_pass(store, limit=50)
    assert result.claimed >= 1
    assert result.ok >= 1

    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id, meta FROM refs WHERE parent_id = %s AND deleted_at IS NULL",
            (rec_id,),
        ).fetchall()
    assert len(rows) == 1
    child_meta = dict(rows[0][1])
    assert child_meta["executor"] == "claude_docker"
    assert child_meta["job_type"] == "sandbox_run"
    assert child_meta["params"]["mode"] == "run"
    assert child_meta["params"]["artifact"] == build_folder_id

    # dispatch then mints a real queued job from the spawned child todo.
    dispatch_result = run_dispatch_pass(store)
    assert dispatch_result.claimed == 1
    with store.pool.connection() as conn:
        job_row = conn.execute(
            "SELECT meta FROM refs WHERE parent_id = %s AND kind = 'job'",
            (int(rows[0][0]),),
        ).fetchone()
    assert job_row is not None
    assert dict(job_row[0])["params"]["mode"] == "run"


# ── precis_access:read — per-run MCP callback wiring ───────────────
#
# Design §"Precis access": a per-run, token'd, read-only MCP callback.
# ``_sandbox_read_mcp.spawn_read_mcp`` itself (subprocess.Popen of a real
# `python -m precis serve`, real DSN derivation) is unit-tested in
# tests/test_sandbox_read_mcp.py; here we only cover claude_docker's
# WIRING — spawn called (or not) at the right time, the network mode
# swap, and teardown on every terminal path — by faking the spawn with a
# REAL stand-in child process (a plain `sleep`), so "test with fake
# PIDs" (spec) means a real process whose liveness we can actually
# assert, not an arbitrary integer `os.kill` on would be meaningless.


def _fake_spawn_read_mcp(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch ``claude_docker._sandbox_read_mcp.spawn_read_mcp`` with a fake
    that spawns a real ``sleep`` (the teardown stand-in), writes a real
    ``mcp.json`` (so "mcp.json written only for precis_access:read" is
    genuinely assertable), and records every call. Returns the shared
    ``calls`` dict; ``calls["proc"]`` is the spawned ``Popen`` handle."""
    import subprocess as _subprocess

    from precis.workers.executors import _sandbox_read_mcp as read_mcp

    calls: dict[str, Any] = {"n": 0}

    def _fake(store: Any, *, work_dir: Path, **_kw: Any) -> Any:
        calls["n"] += 1
        proc = _subprocess.Popen(["sleep", "300"])
        calls["proc"] = proc
        read_mcp.write_mcp_json(work_dir, port=6543, token="fake-token")
        return read_mcp.ReadMcpHandle(pid=proc.pid, port=6543, token="fake-token")

    monkeypatch.setattr(claude_docker._sandbox_read_mcp, "spawn_read_mcp", _fake)
    # The stand-in is a bare `sleep`, not a real `precis serve` — bypass
    # the Finding-3 pid-recycle identity check (asserted directly in
    # tests/test_sandbox_read_mcp.py) so these WIRING tests can focus on
    # "teardown called on every terminal path", not identity matching.
    monkeypatch.setattr(
        claude_docker._sandbox_read_mcp, "_read_mcp_identity_ok", lambda pid: True
    )
    return calls


def _is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _cleanup_proc(calls: dict[str, Any]) -> None:
    proc = calls.get("proc")
    if proc is not None and _is_alive(proc.pid):  # pragma: no cover - safety net
        proc.kill()
        proc.wait(timeout=2)


def test_launch_build_read_access_spawns_mcp_writes_json_and_swaps_network(
    store: Store, sandbox_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_SANDBOX_READ_MCP", "1")
    calls = _fake_spawn_read_mcp(monkeypatch)

    captured: dict[str, Any] = {}
    real_build_run_argv = claude_docker.build_run_argv

    def _spy(**kw: Any) -> list[str]:
        captured["network"] = kw["network"]
        return real_build_run_argv(**kw)

    monkeypatch.setattr(claude_docker, "build_run_argv", _spy)

    try:
        jid = _mk_queued_job(store, params=_valid_params(precis_access="read"))
        claude_docker.run_claude_docker_pass(store, limit=4)

        assert calls["n"] == 1
        assert captured["network"] == claude_docker._sandbox_read_mcp.READ_MCP_NETWORK
        meta = _meta(store, jid)
        assert meta["read_mcp_pid"] == calls["proc"].pid
        work = Path(os.environ["PRECIS_SANDBOX_WORK_DIR"]) / f"sandbox-{jid}"
        assert (work / "mcp.json").is_file()
    finally:
        _cleanup_proc(calls)


def test_launch_build_no_read_access_no_mcp_json_default_network(
    store: Store, sandbox_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _fake_spawn_read_mcp(monkeypatch)

    jid = _mk_queued_job(store, params=_valid_params())
    claude_docker.run_claude_docker_pass(store, limit=4)

    assert calls["n"] == 0  # never called for precis_access:none
    meta = _meta(store, jid)
    assert "read_mcp_pid" not in meta
    work = Path(os.environ["PRECIS_SANDBOX_WORK_DIR"]) / f"sandbox-{jid}"
    assert not (work / "mcp.json").exists()


def test_poll_exit_zero_read_access_reaps_mcp_child(
    store: Store, sandbox_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_SANDBOX_READ_MCP", "1")
    calls = _fake_spawn_read_mcp(monkeypatch)

    try:
        jid = _mk_queued_job(store, params=_valid_params(precis_access="read"))
        claude_docker.run_claude_docker_pass(store, limit=4)  # launch
        assert _is_alive(calls["proc"].pid)

        (sandbox_env / f"sandbox-{jid}.state").write_text("exited 0", encoding="utf-8")
        claude_docker.run_claude_docker_pass(store, limit=4)  # poll -> terminate

        assert _status(store, jid) == "succeeded"
        calls["proc"].wait(timeout=2)
        assert not _is_alive(calls["proc"].pid)
    finally:
        _cleanup_proc(calls)


def test_poll_deadline_kill_read_access_reaps_mcp_child(
    store: Store, sandbox_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deadline-kill routes through the same ``_terminate`` as a normal
    exit, so the teardown wiring is shared — this is the second of the
    spec's three required paths (normal, deadline, reconcile)."""
    monkeypatch.setenv("PRECIS_SANDBOX_READ_MCP", "1")
    calls = _fake_spawn_read_mcp(monkeypatch)

    try:
        jid = _mk_queued_job(store, params=_valid_params(precis_access="read"))
        claude_docker.run_claude_docker_pass(store, limit=4)  # launch
        assert _is_alive(calls["proc"].pid)

        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE refs SET meta = meta || jsonb_build_object("
                "'deadline', 1::float) WHERE ref_id = %s",
                (jid,),
            )
            conn.commit()
        claude_docker.run_claude_docker_pass(store, limit=4)  # poll -> wall-timeout

        assert _status(store, jid) == "failed"
        assert "swept:wall-timeout" in _tags(store, jid)
        calls["proc"].wait(timeout=2)
        assert not _is_alive(calls["proc"].pid)
    finally:
        _cleanup_proc(calls)


def test_reconcile_reaps_orphan_read_mcp_child(
    store: Store, sandbox_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Boot reconcile — the third required path: a worker crash between
    the container exiting and ``_terminate`` running would otherwise
    orphan the read-mcp child forever."""
    monkeypatch.setenv("PRECIS_SANDBOX_READ_MCP", "1")
    calls = _fake_spawn_read_mcp(monkeypatch)

    try:
        jid = _mk_queued_job(store, params=_valid_params(precis_access="read"))
        claude_docker.run_claude_docker_pass(store, limit=4)  # launch
        assert _is_alive(calls["proc"].pid)

        # Simulate a worker crash: the job never reaches _terminate, but
        # something (a human, a sweeper) marks it terminal directly while
        # the container (per the stub's state file) is still "running".
        from precis.workers.executors._common import set_status as _set_status_helper

        _set_status_helper(store, jid, "failed")

        reaped = claude_docker.reconcile_orphans(store)

        assert reaped == 1
        calls["proc"].wait(timeout=2)
        assert not _is_alive(calls["proc"].pid)
    finally:
        _cleanup_proc(calls)
