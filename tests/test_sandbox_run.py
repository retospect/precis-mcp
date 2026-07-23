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
        "wall_seconds": 1800,
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
    return dict(row[0] or {})


def _tags(store: Store, ref_id: int) -> set[str]:
    return {str(t) for t in store.tags_for(ref_id)}


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


def test_resolve_model_uses_cloud_super(monkeypatch: pytest.MonkeyPatch) -> None:
    from precis.utils.llm.router import Tier, resolve_model

    monkeypatch.delenv("PRECIS_SANDBOX_MODEL", raising=False)
    assert sandbox_run.resolve_sandbox_model() == resolve_model(Tier.CLOUD_SUPER)
    monkeypatch.setenv("PRECIS_SANDBOX_MODEL", "claude-custom-9")
    assert sandbox_run.resolve_sandbox_model() == "claude-custom-9"


def test_compose_prompt_has_task_and_harvest_contract() -> None:
    body = sandbox_run.compose_prompt("do the thing")
    assert "do the thing" in body
    assert "/work/out" in body
    assert "uv.lock" in body


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

    def test_rejects_mode_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._env(monkeypatch)
        err = sandbox_run.validate_submit(None, params=_valid_params(mode="run"))
        assert err is not None and "mode" in err

    def test_rejects_precis_access_read(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._env(monkeypatch)
        err = sandbox_run.validate_submit(
            None, params=_valid_params(precis_access="read")
        )
        assert err is not None and "precis_access" in err

    def test_rejects_secrets(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._env(monkeypatch)
        err = sandbox_run.validate_submit(
            None, params=_valid_params(secrets=["OPENAI_KEY"])
        )
        assert err is not None and "secrets" in err

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
    # the image is the final token
    assert argv[-1] == "code-task:abc"


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


def test_put_time_validate_rejects_mode_run(
    hub: Hub, store: Store, sandbox_env: Path
) -> None:
    """A direct job put with a fail-closed param is rejected at put time."""
    from precis.errors import BadInput
    from precis.handlers.job import JobHandler

    parent = store.insert_ref(kind="todo", slug=None, title="owner", meta={})
    with pytest.raises(BadInput, match="mode"):
        JobHandler(hub=hub).put(
            job_type="sandbox_run",
            parent_id=parent.id,
            params=_valid_params(mode="run"),
        )


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


def test_poll_exit_zero_succeeds(store: Store, sandbox_env: Path) -> None:
    parent = store.insert_ref(kind="todo", slug=None, title="owner", meta={})
    jid = _mk_queued_job(store, params=_valid_params(), parent_id=parent.id)
    claude_docker.run_claude_docker_pass(store, limit=4)  # launch
    # Container reports a clean exit.
    (sandbox_env / f"sandbox-{jid}.state").write_text("exited 0")
    claude_docker.run_claude_docker_pass(store, limit=4)  # poll → reap
    assert _status(store, jid) == "succeeded"
    # Parent not bubbled.
    assert not any(t.startswith("child-failed:") for t in _tags(store, parent.id))


def test_poll_exit_one_fails_and_bubbles(store: Store, sandbox_env: Path) -> None:
    parent = store.insert_ref(kind="todo", slug=None, title="owner", meta={})
    jid = _mk_queued_job(store, params=_valid_params(), parent_id=parent.id)
    claude_docker.run_claude_docker_pass(store, limit=4)  # launch
    (sandbox_env / f"sandbox-{jid}.state").write_text("exited 1")
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
    (sandbox_env / "sandbox-999999.state").write_text("running 0")
    reaped = claude_docker.reconcile_orphans(store)
    assert reaped == 1
    assert not (sandbox_env / "sandbox-999999.state").exists()


def test_reconcile_keeps_live_job(store: Store, sandbox_env: Path) -> None:
    jid = _mk_queued_job(store, params=_valid_params())
    (sandbox_env / f"sandbox-{jid}.state").write_text("running 0")
    reaped = claude_docker.reconcile_orphans(store)
    assert reaped == 0
    assert (sandbox_env / f"sandbox-{jid}.state").exists()


# ── launch-time fail-closed (defence in depth for dispatch path) ───


def test_launch_rejects_bad_params_without_container(
    store: Store, sandbox_env: Path
) -> None:
    # A job minted with mode:run (bypassing put-time validate) must be
    # failed at launch, never started.
    jid = _mk_queued_job(store, params=_valid_params(mode="run"))
    claude_docker.run_claude_docker_pass(store, limit=4)
    assert _status(store, jid) == "failed"
    assert "container" not in _meta(store, jid)
