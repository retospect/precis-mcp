"""The scratch database: one Postgres container in its own Compose project.

Route chosen (the cheapest safe one): the ``pgvector/pgvector:pg17`` image
the dev/test stack already pulls, started from our own
``scripts/rehearsal/compose.yaml`` with a published loopback port. The
``scripts/test`` stack cannot serve (its database has no published port, it
is reached over a Compose network by the gate container), and nothing else
in the repo gives a host-reachable database. The project name is
``precis-rehearsal-<checkout id>-<run id>``: the checkout id (a hash of this
checkout's path) keeps a ``--clean`` or a stray check from touching another
checkout's live rehearsal. This module refuses to run Compose under any other
name, refuses unless the container engine is the local one (a unix socket, no
``DOCKER_HOST`` pointing elsewhere), never uses ``prune`` and starts at most
one container.

Migrations are applied by the *host checkout's* interpreter
(``precis migrate``), so the schema is the one the code under test ships.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from . import proc
from .safety import DB_NAME, Refused, assert_scratch_dsn, pin_port

PROJECT_PREFIX = "precis-rehearsal"
#: The container client, by name (found on PATH).
DOCKER_CLIENT = "docker"
COMPOSE_FILE = Path(__file__).resolve().parent / "compose.yaml"

_engine_checked = False


def checkout_id(repo_root: Path) -> str:
    """Eight hex characters naming this checkout (its resolved path)."""
    return hashlib.sha1(str(repo_root.resolve()).encode()).hexdigest()[:8]


def project_prefix(repo_root: Path) -> str:
    """The Compose project name prefix of this checkout's rehearsals."""
    return f"{PROJECT_PREFIX}-{checkout_id(repo_root)}-"


def check_local_engine() -> None:
    """Refuse unless the container engine this process would use is the local
    one: ``DOCKER_HOST`` unset or a unix socket, and the current Docker
    context's endpoint a unix socket. A remote engine would put the scratch
    database (and the compose project) somewhere else."""
    global _engine_checked
    if _engine_checked:
        return
    host = os.environ.get("DOCKER_HOST", "")
    if host and not host.startswith("unix://"):
        raise Refused(
            f"refusing to start: DOCKER_HOST={host!r} is not a local unix socket"
        )
    out = proc.run([DOCKER_CLIENT, "context", "inspect"], timeout=30)
    if out.returncode != 0:
        raise Refused(f"docker is not usable: {out.stderr.strip()[:200]}")
    try:
        endpoint = json.loads(out.stdout)[0]["Endpoints"]["docker"]["Host"]
    except (ValueError, LookupError, TypeError) as exc:
        raise Refused(f"cannot read the docker context endpoint ({exc})") from exc
    if not str(endpoint).startswith("unix://"):
        raise Refused(
            f"refusing to start: the docker context's engine {endpoint!r} is not "
            "a local unix socket"
        )
    _engine_checked = True


def _compose(
    project: str, *args: str, timeout: float = 180.0
) -> subprocess.CompletedProcess[str]:
    if not project.startswith(PROJECT_PREFIX):
        raise Refused(
            f"refusing Compose project {project!r}: must start {PROJECT_PREFIX!r}"
        )
    check_local_engine()
    return proc.run(
        [DOCKER_CLIENT, "compose", "-p", project, "-f", str(COMPOSE_FILE), *args],
        timeout=timeout,
    )


def other_projects(prefix: str = f"{PROJECT_PREFIX}-") -> list[str]:
    """Names of every Compose project on the local engine whose name starts
    with ``prefix`` (running or stopped); pass :func:`project_prefix` to see
    only this checkout's."""
    if not prefix.startswith(PROJECT_PREFIX):
        raise Refused(f"refusing prefix {prefix!r}: must start {PROJECT_PREFIX!r}")
    check_local_engine()
    ls = proc.run(
        [DOCKER_CLIENT, "compose", "ls", "--all", "--format", "json"], timeout=60
    )
    if ls.returncode != 0:
        raise Refused(f"docker is not usable: {ls.stderr.strip()[:200]}")
    try:
        rows = json.loads(ls.stdout or "[]")
    except ValueError:
        rows = []
    return sorted(
        str(r.get("Name")) for r in rows if str(r.get("Name", "")).startswith(prefix)
    )


def remove_project(project: str) -> str:
    """``compose down -v`` for one rehearsal project; a note, never raises."""
    try:
        done = _compose(project, "down", "-v", "--remove-orphans", timeout=120)
    except Refused as exc:
        return f"not removed: {exc}"
    except (OSError, subprocess.SubprocessError) as exc:
        return f"{project}: down failed ({exc})"
    return (
        f"{project}: removed"
        if done.returncode == 0
        else (f"{project}: down failed ({(done.stderr or '').strip()[:160]})")
    )


@dataclass
class ScratchDb:
    """One running scratch database."""

    project: str
    dsn: str

    @classmethod
    def up(cls, run_id: str, repo_root: Path) -> ScratchDb:
        """Start the container and wait for health. Refuses when another of
        this checkout's rehearsal projects already exists (one container at
        most; ``--clean`` removes strays). Anything that goes wrong after the
        container was started removes it again before the error propagates."""
        prefix = project_prefix(repo_root)
        project = f"{prefix}{run_id}"
        strays = [p for p in other_projects(prefix) if p != project]
        if strays:
            raise Refused(
                "another rehearsal database project of this checkout exists ("
                + ", ".join(strays)
                + "); run `scripts/factory-rehearsal --clean` first"
            )
        try:
            started = _compose(project, "up", "-d", "--wait", timeout=300)
            if started.returncode != 0:
                tail = (started.stderr or started.stdout or "").strip()[-400:]
                raise RuntimeError(f"scratch database did not start: {tail}")
            port_proc = _compose(project, "port", "db", "5432")
            line = (port_proc.stdout or "").strip().splitlines()
            if port_proc.returncode != 0 or not line:
                raise RuntimeError("could not read the scratch database port")
            host, _, port = line[0].rpartition(":")
            host = host.strip("[]")
            pin_port(int(port))
            dsn = f"postgresql://postgres@{'[::1]' if ':' in host else host}:{port}/{DB_NAME}"
            assert_scratch_dsn(dsn)
            return cls(project=project, dsn=dsn)
        except BaseException:
            remove_project(project)
            raise

    def migrate(self, python: Path, *, env: dict[str, str], cwd: Path) -> str:
        """Apply the migrations with the host checkout's interpreter. Returns
        the tail of its output."""
        assert_scratch_dsn(self.dsn)
        started = time.monotonic()
        done = proc.run(
            [str(python), "-m", "precis", "migrate", "--database-url", self.dsn],
            cwd=cwd,
            env=env,
            timeout=900,
        )
        if done.returncode != 0:
            raise RuntimeError(
                "precis migrate failed: "
                + ((done.stderr or done.stdout or "").strip()[-600:])
            )
        return f"migrated in {time.monotonic() - started:.0f}s"

    def down(self) -> str:
        return remove_project(self.project)
