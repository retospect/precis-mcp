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
it differs. **Fallback** (Reto: "if this process may not write to nas,
keep them locally and move them in the job's cleanup step"): a root that
does not exist — the mount is absent, an empty shadow dir must never be
created over it — or that refuses the write lands the file under
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
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return dest


def _root_writable(root: Path) -> bool:
    """The root must EXIST before we write under it: ``mkdir`` on an
    absent NAS mountpoint would create a shadow dir on the local disk and
    silently eat the artifacts the mount was meant to hold (hidden again
    once the share remounts). Subdirs beneath an existing root are fine
    to create."""
    return root.is_dir()


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
    conn.execute(
        """
        UPDATE refs
           SET meta = jsonb_set(
                   meta, '{artifacts}',
                   (CASE WHEN jsonb_typeof(meta->'artifacts') = 'array'
                         THEN meta->'artifacts'
                         ELSE '[]'::jsonb END) || %s::jsonb,
                   true)
         WHERE ref_id = %s
        """,
        (Jsonb(entry), ref_id),
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
    for base in (root, spool):
        cand = base / rel
        if cand.is_file():
            data = cand.read_bytes()
            sha = entry.get("sha256")
            if sha and hashlib.sha256(data).hexdigest() != sha:
                raise ValueError(
                    f"artifact {entry.get('name')}: sha256 mismatch at {cand}"
                )
            return data
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
                # Same name, different content — keep both rather than
                # lose a recorded byte stream.
                dest = dest.with_name(f"{dest.stem}-moved{dest.suffix}")
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
