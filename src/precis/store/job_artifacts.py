"""Job artifacts — a compute job's large outputs on the shared mount.

gr543776 (Reto 2026-11-01 storage ruling): the pathway lane's big outputs
(NEB bands, TS geometries, failed-seed salvage) are **job artifacts on the
NAS mount**, not ``refs.meta`` rows — meta keeps only the pointer list.
This is the small helper every job_type that keeps artifacts shares:

* :func:`write_artifact` stores ``bytes`` under
  ``<root>/job-artifacts/<ref_id>/<name>`` and appends
  ``{"name", "path", "bytes", "sha256"}`` to the job's ``meta.artifacts``
  (one atomic jsonb append, same pattern as
  :func:`precis_pathway.seed_job._record_started_seed`'s union).
* :func:`read_artifact` resolves a recorded entry back to bytes and
  re-hashes it against the stamped ``sha256``.
* :func:`drain_spool` re-tries the root for files that landed in the
  local spool (a job's cleanup step calls it).

**The root** is one setting: ``PRECIS_JOB_ARTIFACT_ROOT``, default
``~/work`` — the same shared-mount default
:func:`precis.workers.executors._sandbox_harvest.artifact_root` uses, so a
host whose ``~/work`` IS the NAS mount keeps both stores under it with no
extra config; the ops play points the var at the NAS mount on nodes where
it differs. A root counts as *present* only when it is a mountpoint
(``os.path.ismount``) or carries the sentinel file
``.precis-artifact-root``: an empty unmounted mountpoint's plain dir must
never be written into — that creates a local shadow dir that silently
eats the artifacts the mount was meant to hold (hidden again once the
share remounts), which is exactly the failure the spool exists for. Ops
touches the sentinel in the shared dir when activating the lane.
**Fallback** (Reto: "if this process may not write to nas, keep them
locally and move them in the job's cleanup step"): a root that is absent,
unsentined, or refuses the write lands the file under
``PRECIS_JOB_ARTIFACT_SPOOL`` (default
``~/.cache/precis/job-artifacts-spool``) instead, records the entry with
``"spooled": true``, and logs a warning. The relative ``path`` is
identical under either root, so a later :func:`drain_spool` moves the
file into place without touching the recorded entry.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

log = logging.getLogger(__name__)

#: The fixed relative prefix every artifact lands under, inside either
#: root (mirrors ``_sandbox_harvest.ARTIFACT_SUBDIR``).
ARTIFACT_SUBDIR = "job-artifacts"

_ROOT_ENV = "PRECIS_JOB_ARTIFACT_ROOT"
_SPOOL_ENV = "PRECIS_JOB_ARTIFACT_SPOOL"
_DEFAULT_SPOOL = Path("~/.cache/precis/job-artifacts-spool")

#: Marks a directory as a real artifact root (see :func:`_root_writable`).
SENTINEL = ".precis-artifact-root"

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def artifact_root() -> Path:
    """The shared-mount artifact root: ``PRECIS_JOB_ARTIFACT_ROOT`` or
    ``~/work`` (the sandbox-harvest default — on NAS-mounted hosts that
    IS the shared mount)."""
    raw = os.environ.get(_ROOT_ENV)
    if raw:
        return Path(raw).expanduser()
    return Path.home() / "work"


def spool_root() -> Path:
    """The local fallback for an absent/unwritable artifact root:
    ``PRECIS_JOB_ARTIFACT_SPOOL`` or ``~/.cache/precis/job-artifacts-spool``
    (always local — mkdir'd on demand, unlike the root)."""
    raw = os.environ.get(_SPOOL_ENV)
    if raw:
        return Path(raw).expanduser()
    return _DEFAULT_SPOOL.expanduser()


def _safe_name(name: str) -> str:
    """``name`` as one safe path segment — a logical name never escapes
    its ``<ref_id>/`` dir."""
    out = _SAFE_NAME_RE.sub("_", name).strip("._")
    return out or "artifact"


def _dest_for(root: Path, ref_id: int, name: str, sha256: str) -> Path:
    """``<root>/job-artifacts/<ref_id>/<name>`` — content-unique: a second
    artifact under one job/name with DIFFERENT bytes (a re-run's band)
    gets a ``-<sha8>`` suffix rather than overwriting the recorded one."""
    base = root / ARTIFACT_SUBDIR / str(ref_id)
    dest = base / _safe_name(name)
    if not dest.exists():
        return dest
    try:
        if _sha256_file(dest) == sha256:
            return dest
    except OSError:
        pass
    stem, dot, ext = _safe_name(name).rpartition(".")
    if not stem:
        stem, ext = _safe_name(name), ""
    return base / f"{stem}-{sha256[:8]}{dot}{ext}"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_bytes(root: Path, rel: Path, data: bytes) -> Path:
    """Atomic write of ``data`` at ``root/rel`` (same-dir tmp + replace);
    parents created. Raises ``OSError`` when the root refuses the write."""
    dest = root / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=dest.name + ".", dir=str(dest.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        # mkstemp is 0600; artifacts are shared files other hosts read
        # over the mount — make them world-readable like every other
        # corpus sibling.
        os.chmod(tmp, 0o644)
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return dest


def _root_writable(root: Path) -> bool:
    """The root must be a real mount or sentinel-marked before we write
    under it. A bare ``is_dir`` is not enough: an unmounted NAS
    mountpoint IS a directory — just an empty local one — and writing
    into it creates a shadow dir that silently eats the artifacts the
    mount was meant to hold (and never ``mkdir`` the root itself, for
    the same reason). ``os.path.ismount`` covers mounted roots; the
    :data:`SENTINEL` file covers a shared dir that is genuinely local
    (dev hosts, ``~/work``) and opted in by ops. Subdirs beneath an
    accepted root are fine to create."""
    return root.is_dir() and (os.path.ismount(root) or (root / SENTINEL).is_file())


def write_artifact(
    conn: Any,
    ref_id: int,
    name: str,
    data: bytes,
    *,
    root: Path | None = None,
    spool: Path | None = None,
) -> dict[str, Any]:
    """Store ``data`` as one artifact of job ``ref_id`` and record it on
    the job's ``meta.artifacts``. Returns the recorded entry
    ``{"name", "path", "bytes", "sha256", ("spooled")}`` — ``path`` is
    relative to the artifact root (identical under the spool, so
    :func:`drain_spool` moves the file without rewriting meta).

    The file lands first, the meta append second: an entry whose file
    never landed can't exist (a failed write raises before the UPDATE),
    while a file whose UPDATE fails is an unrecorded stray the next
    harvest dedupes by content hash — the safer of the two orderings.
    Caller commits."""
    sha = hashlib.sha256(data).hexdigest()
    root = root if root is not None else artifact_root()
    spool = spool if spool is not None else spool_root()

    spooled = False
    try:
        if not _root_writable(root):
            raise OSError(f"artifact root {root} does not exist")
        rel = _dest_for(root, ref_id, name, sha).relative_to(root)
        _write_bytes(root, rel, data)
    except OSError as exc:
        spooled = True
        log.warning(
            "job_artifacts: root %s not writable for job %s (%s) — spooling %s",
            root,
            ref_id,
            exc,
            name,
        )
        rel = _dest_for(spool, ref_id, name, sha).relative_to(spool)
        _write_bytes(spool, rel, data)

    entry: dict[str, Any] = {
        "name": name,
        "path": str(rel),
        "bytes": len(data),
        "sha256": sha,
    }
    if spooled:
        entry["spooled"] = True
    # Append-only by (name, sha256): a retried harvest/dispatch lands the
    # same payload under the same logical name — the EXISTS guard keeps a
    # retry from duplicating the row while a same-name-different-bytes
    # write (a re-run's band) still records its own entry and path.
    conn.execute(
        """
        UPDATE refs
           SET meta = jsonb_set(
                   meta, '{artifacts}',
                   (CASE WHEN jsonb_typeof(meta->'artifacts') = 'array'
                         THEN meta->'artifacts'
                         ELSE '[]'::jsonb END)
                   || CASE WHEN EXISTS (
                           SELECT 1 FROM jsonb_array_elements(
                               CASE WHEN jsonb_typeof(meta->'artifacts') = 'array'
                                    THEN meta->'artifacts'
                                    ELSE '[]'::jsonb END) AS e
                           WHERE e.value->>'name' = %s
                             AND e.value->>'sha256' = %s)
                       THEN '[]'::jsonb ELSE %s::jsonb END,
                   true)
         WHERE ref_id = %s
        """,
        (name, sha, Jsonb(entry), ref_id),
    )
    return entry


def read_artifact(
    entry: dict[str, Any],
    *,
    root: Path | None = None,
    spool: Path | None = None,
) -> bytes:
    """Resolve a ``meta.artifacts`` entry back to its bytes, verified
    against the recorded ``sha256`` — the fetch-side integrity check the
    stamp exists for. Root first; the spool path is the fallback for an
    entry never drained (``spooled: true``) or written before the mount
    came up."""
    root = root if root is not None else artifact_root()
    spool = spool if spool is not None else spool_root()
    rel = Path(str(entry["path"]))
    saw_file = False
    for base in (root, spool):
        cand = base / rel
        if not cand.is_file():
            continue
        data = cand.read_bytes()
        sha = entry.get("sha256")
        if sha and hashlib.sha256(data).hexdigest() != sha:
            # Wrong content under this root — a same-name sibling, not
            # this artifact. Keep looking: the recorded path resolves
            # under the spool for an entry never drained.
            saw_file = True
            continue
        return data
    if saw_file:
        raise ValueError(f"artifact {entry.get('name')}: sha256 mismatch at {rel}")
    raise FileNotFoundError(f"artifact {entry.get('name')}: no {rel} under {root}")


def drain_spool(
    ref_id: int,
    *,
    root: Path | None = None,
    spool: Path | None = None,
) -> int:
    """Move one job's spooled artifacts into the shared root — the
    cleanup-step half of the unwritable-root fallback. Best-effort, moves
    only this job's ``job-artifacts/<ref_id>/`` files; returns the count
    moved. The recorded ``path`` is root-relative either way, so meta
    needs no rewrite."""
    root = root if root is not None else artifact_root()
    spool = spool if spool is not None else spool_root()
    if not _root_writable(root):
        return 0
    src_dir = spool / ARTIFACT_SUBDIR / str(ref_id)
    if not src_dir.is_dir():
        return 0
    moved = 0
    for src in sorted(src_dir.iterdir()):
        if not src.is_file() or src.is_symlink():
            continue
        dest = root / ARTIFACT_SUBDIR / str(ref_id) / src.name
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists() and _sha256_file(dest) != _sha256_file(src):
                # Same recorded path, different bytes — a move would
                # either clobber the root file or rename this one out
                # from under its meta entry. Leave it in the spool:
                # the recorded path still resolves to THIS file's
                # content through read_artifact's spool fallback.
                log.warning(
                    "job_artifacts: drain of %s kept in spool — %s already "
                    "holds different content",
                    src,
                    dest,
                )
                continue
            shutil.move(str(src), str(dest))
            moved += 1
        except OSError as exc:
            log.warning(
                "job_artifacts: drain of %s for job %s failed (%s)", src, ref_id, exc
            )
    try:
        src_dir.rmdir()  # only succeeds once empty
    except OSError:
        pass
    if moved:
        log.info(
            "job_artifacts: drained %d spooled artifact(s) for job %s", moved, ref_id
        )
    return moved


__all__ = [
    "ARTIFACT_SUBDIR",
    "artifact_root",
    "drain_spool",
    "read_artifact",
    "spool_root",
    "write_artifact",
]
