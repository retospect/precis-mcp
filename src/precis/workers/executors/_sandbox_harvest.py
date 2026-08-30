"""sandbox_run harvest — ``/work/out`` → folder + content-addressed tarball.

Slices 2+3 of ``sandbox_run`` (harvest → DB + NAS, and re-run /
operationalize). Called by
:mod:`precis.workers.executors.claude_docker` after a container exits
**0**:

* a ``kind='folder'`` ref is minted, ``derived-from`` the job and
  ``supersedes``-linked to the same todo's previous build folder, if any
  (:func:`_link_supersedes_lineage` — each build mints a new folder, the
  chain is the history);
* each ``/work/out`` file is projected as a disk-backed ``plaintext`` ref
  parented under that folder — a legible, searchable DB copy (pathological-
  content guard only: size cap, binary skip — not a faithful copy);
* the whole ``out/`` tree is tarred (gzip — stdlib ``tarfile``, no new
  dependency; the design doc's ``.tar.zst`` naming is a later, purely
  cosmetic swap, not a contract change: the harvest shape is
  ``{sha256, size, key}``, addressed by content hash either way) into a
  content-addressed store under ``PRECIS_SANDBOX_ARTIFACT_ROOT`` — the
  faithful, runnable copy (``meta.artifact = {sha256, size, key}``);
* ``/work/out/RUN.json`` (``{cmd, inputs, outputs, image}``), when present,
  is parsed onto both the folder's and the job's ``meta`` as the
  ``mode:run`` recipe.

"DB holds the legible projection, NAS holds the faithful runnable tarball" —
mirrors CAD/structure/paper-PDF (``corpus_layout.py``).

**Deliberate deviation from the design doc's literal "plaintext/python
refs" wording**: ``kind='python'`` (``handlers/python.py``) is an
in-memory, AST-only navigator with **no DB persistence** ("Backed by an
in-memory ``RepoCache`` (no DB persistence...)"), so it has no ref a
harvest could write. Every harvested file — ``.py`` included — projects as
``kind='plaintext'`` instead; still fully legible and searchable.

**Where the projection lives on disk**: mirrors
``precis.sim.ingest.ingest_sim`` (the closest existing precedent — a
compute run's outputs projected into the corpus and driven through
``PlaintextHandler.ensure_ingested``, not the create-only ``put()``):
files land under ``PRECIS_ROOT/sandbox/<container-name>/…`` so the
resulting refs are genuine, disk-round-tripping plaintext refs — never a
DB-only row a later ``get`` would silently soft-delete for "missing file"
(``PlaintextHandler.ensure_ingested`` does exactly that when the backing
file is gone). When ``PRECIS_ROOT`` isn't configured on the host, the
plaintext projection is skipped (logged, not fatal) — the tarball still
lands, so harvest degrades to "artifact only" rather than failing the job
over a missing dev-only env var.

**``mode:run`` staging** (:func:`stage_run_artifact`, called by
``claude_docker._launch_run`` before the container starts): the inverse
direction — fetch a prior build's tarball back OUT of the content-
addressed store into a fresh ``/work`` root, sha256-verified; on a miss,
reconstruct from the folder's ``plaintext`` refs using each ref's
stamped ``meta.harvest_orig_path`` to undo the lossy on-disk name
rewrite. **Deliberate deviation**: the design's "``run-of`` link to the
build folder" reuses the existing ``derived-from`` relation instead of
adding a new one — a fresh migration is out of scope for this cycle
(``agent_ro`` already covers the one migration-shaped need), and
``derived-from`` already carries the right semantics (this folder's
*result* is derived from that build's *code*); ``meta.run_of_folder_id``
on the run's own harvest folder disambiguates which ``derived-from``
edge is "the run job" vs. "the build it re-ran" without a new relation
slug.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from precis.utils.md_parse import file_slug_from_path, is_valid_file_slug

if TYPE_CHECKING:
    from precis.dispatch import Hub
    from precis.store import Store

log = logging.getLogger(__name__)

# Pathological-content guard: the DB
# projection is legible, not faithful (the tarball is faithful) — size
# cap + binary skip only, no format validation.
MAX_HARVEST_FILE_BYTES = 2_000_000

# Extensions PlaintextHandler already accepts unmodified.
_PLAINTEXT_NATIVE_EXTS = (".txt", ".log", ".bib")

#: Relative content-addressed key prefix (design: "sandbox-artifacts/
#: <sha256>.tar.zst" — built here as ``.tar.gz`` via stdlib ``tarfile``,
#: see :func:`build_tarball`'s docstring for why).
ARTIFACT_SUBDIR = "sandbox-artifacts"
ARTIFACT_EXT = ".tar.gz"


@dataclass(frozen=True, slots=True)
class HarvestResult:
    """What one harvest run produced — folded into the ``job_summary`` text.

    ``folder_ref_id is None`` means ``out/`` had no files (the "empty
    out/" taxonomy class) — nothing else on this result is meaningful.
    """

    folder_ref_id: int | None = None
    n_projected: int = 0
    n_skipped: int = 0
    artifact: dict[str, Any] | None = None
    run_recipe: dict[str, Any] | None = None
    messages: tuple[str, ...] = field(default_factory=tuple)

    @property
    def empty(self) -> bool:
        return self.folder_ref_id is None


# ── content-addressed tarball store ─────────────────────────────────


def artifact_root() -> Path:
    """Per-host root the content-addressed tarball store resolves under.

    ``PRECIS_SANDBOX_ARTIFACT_ROOT`` override; default mirrors
    ``corpus_layout.DEFAULT_CORPUS``'s shape (``~/work/corpus`` for the
    paper-PDF corpus) — ``~/work`` is the generic shared-mount default,
    with ``sandbox-artifacts/`` (the fixed relative key prefix, see
    :func:`artifact_key`) nested beneath it. A bare install still
    resolves; the ops play overrides this one env var per host, exactly
    like ``PRECIS_CORPUS_DIR``.
    """
    raw = os.environ.get("PRECIS_SANDBOX_ARTIFACT_ROOT")
    if raw:
        return Path(raw).expanduser()
    return Path.home() / "work"


def artifact_key(sha256: str) -> str:
    """Relative content-addressed key: ``sandbox-artifacts/<sha>.tar.gz``."""
    return f"{ARTIFACT_SUBDIR}/{sha256}{ARTIFACT_EXT}"


def artifact_path(sha256: str, *, root: Path | None = None) -> Path:
    """Absolute path for a tarball's content-addressed key under ``root``
    (default :func:`artifact_root`)."""
    return (root if root is not None else artifact_root()) / artifact_key(sha256)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build_tarball(out_dir: Path, *, root: Path | None = None) -> dict[str, Any]:
    """Tar ``out_dir`` into the content-addressed store.

    Gzip via stdlib ``tarfile`` — no new dependency on top of a shared
    dev image the container gate can't rebuild mid-session; the design
    doc's ``.tar.zst`` naming is a cosmetic detail of *this specific*
    compression choice, not part of the harvest contract (``{sha256,
    size, key}``, addressed by content hash regardless of codec) —
    swapping in zstd later (a real dependency, once the ops image build
    can carry it) is a one-function change, not a schema change.

    Returns ``{"sha256", "size", "key"}`` — the shape stored at
    ``meta.artifact``. Hashes the compressed bytes actually written to
    disk, so ``key`` addresses exactly what's stored (round-trip verified
    by :func:`verify_artifact`).
    """
    dest_root = root if root is not None else artifact_root()
    fd, tmp_name = tempfile.mkstemp(prefix="sandbox-artifact-", suffix=ARTIFACT_EXT)
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        with tarfile.open(tmp_path, mode="w:gz") as tar:
            tar.add(out_dir, arcname=".")
        sha256 = _sha256_file(tmp_path)
        size = tmp_path.stat().st_size
        key = artifact_key(sha256)
        dest = dest_root / key
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(tmp_path), str(dest))
        return {"sha256": sha256, "size": size, "key": key}
    finally:
        tmp_path.unlink(missing_ok=True)


def verify_artifact(sha256: str, *, root: Path | None = None) -> bool:
    """Re-hash the stored tarball and compare against ``sha256``.

    The fetch-time integrity check the design calls for ("Fetch verifies
    the sha…"). Reconstructing from the folder's ``plaintext`` refs on a
    miss is ``mode:run``'s staging concern, not harvest's.
    """
    path = artifact_path(sha256, root=root)
    if not path.is_file():
        return False
    return _sha256_file(path) == sha256


# ── RUN.json (the mode:run recipe) ─────────────────────────────────


def parse_run_json(out_dir: Path) -> dict[str, Any] | None:
    """Best-effort parse of ``/work/out/RUN.json`` (``{cmd, inputs,
    outputs, image}``). Returns ``None`` (logged, never raises) when the
    file is absent, unreadable, or not a JSON object."""
    path = out_dir / "RUN.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        log.warning("sandbox_run harvest: RUN.json parse failed: %s", exc)
        return None
    if not isinstance(data, dict):
        log.warning("sandbox_run harvest: RUN.json is not a JSON object")
        return None
    return data


# ── /work/out → DB projection (plaintext refs under PRECIS_ROOT) ───


def _is_probably_binary(data: bytes) -> bool:
    if b"\x00" in data:
        return True
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


def _dest_name(rel_name: str) -> str:
    """Destination filename under the harvest root.

    A name whose extension ``PlaintextHandler`` already accepts
    (:data:`_PLAINTEXT_NATIVE_EXTS`) is kept as-is. Anything else gets
    every embedded ``.`` collapsed to ``-`` and a single ``.txt``
    appended (``main.py`` -> ``main-py.txt``) — **not** a plain
    ``.txt`` append (``main.py`` -> ``main.py.txt``), because
    ``PlaintextHandler``'s slug<->path mapping
    (``md_parse.file_slug_from_path`` / ``path_from_file_slug``) strips
    /re-appends exactly the LAST dot-suffix; a physical name with a
    second embedded dot round-trips through a slug to the WRONG path
    (``main.py.txt`` -> slug encodes the embedded ``.`` as ``-`` too,
    losslessly on the way in but ``path_from_file_slug`` never puts it
    back), so ``ensure_ingested`` can't find the file it just wrote.
    Collapsing dots up front keeps exactly one real extension on disk,
    which is what that mapping actually supports.

    Always lowercased for the same reason: ``file_slug_from_path``
    lowercases every path segment on the way to a slug, and
    ``path_from_file_slug`` never recovers the original case — a mixed-
    case dest name (``README.txt``) round-trips to a lowercase path
    (``readme.txt``) that doesn't exist, and ``ensure_ingested`` reads
    that as "the file vanished" and returns ``None``.
    """
    lower = rel_name.lower()
    if lower.endswith(_PLAINTEXT_NATIVE_EXTS):
        return lower
    return lower.replace(".", "-") + ".txt"


def _iter_out_files(out_dir: Path) -> tuple[list[Path], int]:
    """Depth-first walk of ``out_dir`` collecting regular files, refusing
    to dereference or descend into ANY symlink (file or directory).

    Symlink exfil (CRITICAL): an untrusted build can write ``ln -s
    ~/.pgpass /work/out/leak`` — if the harvest walk dereferenced that
    host-side, the *host* file's contents would get read and projected
    into a DB-visible plaintext ref, and a symlinked directory would let
    the walk escape ``out_dir`` entirely. So a symlink entry is skipped
    outright: never opened, never recursed into, regardless of what it
    points at or whether the target even exists.

    Returns ``(files, n_symlinks_skipped)``.
    """
    if not out_dir.is_dir():
        return [], 0
    files: list[Path] = []
    n_skipped = 0
    stack = [out_dir]
    while stack:
        current = stack.pop()
        try:
            entries = list(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_symlink():
                n_skipped += 1
                continue
            if entry.is_dir():
                stack.append(entry)
            elif entry.is_file():
                files.append(entry)
    return sorted(files), n_skipped


def project_out(
    hub: Hub,
    *,
    out_dir: Path,
    dest_root: Path,
    harvest_subdir: str,
) -> tuple[list[int], int, list[str]]:
    """Project every ``out_dir`` file as a disk-backed ``plaintext`` ref
    under ``dest_root/harvest_subdir/`` — drives the same
    ``PlaintextHandler.ensure_ingested`` walker
    ``precis.sim.ingest.ingest_sim`` uses, not the create-only ``put()``.

    Returns ``(ref_ids, n_skipped, messages)``.
    """
    from precis.handlers.plaintext import PlaintextHandler

    dest_dir = dest_root / harvest_subdir
    dest_dir.mkdir(parents=True, exist_ok=True)
    handler = PlaintextHandler(hub=hub, root=dest_root)

    ref_ids: list[int] = []
    n_skipped = 0
    messages: list[str] = []

    files, n_symlinks_skipped = _iter_out_files(out_dir)
    if n_symlinks_skipped:
        n_skipped += n_symlinks_skipped
        messages.append(
            f"skip  {n_symlinks_skipped} symlink(s)  - not projected "
            "(host containment: never dereferenced)"
        )

    real_out_dir = Path(os.path.realpath(out_dir))
    for src in files:
        rel = src.relative_to(out_dir)
        # Belt-and-braces containment (Finding 1): even though
        # ``_iter_out_files`` never descends into or reports a symlinked
        # entry, re-verify here that the resolved real path is still
        # under ``out_dir`` before touching it — a second, independent
        # guard against any future walk-order/race edge the first check
        # might miss.
        real_src = Path(os.path.realpath(src))
        if real_src != real_out_dir and real_out_dir not in real_src.parents:
            n_skipped += 1
            messages.append(f"skip  {rel}  - resolves outside out/ (containment)")
            continue
        try:
            size = src.stat().st_size
        except OSError:
            continue
        if size > MAX_HARVEST_FILE_BYTES:
            n_skipped += 1
            messages.append(f"skip  {rel}  - over {MAX_HARVEST_FILE_BYTES}B cap")
            continue
        try:
            data = src.read_bytes()
        except OSError as exc:
            n_skipped += 1
            messages.append(f"skip  {rel}  - unreadable ({exc})")
            continue
        if _is_probably_binary(data):
            n_skipped += 1
            messages.append(f"skip  {rel}  - binary")
            continue

        dest_name = _dest_name(str(rel))
        dest = dest_dir / dest_name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)

        dest_rel = str(dest.relative_to(dest_root))
        try:
            slug = file_slug_from_path(dest_rel)
        except ValueError:
            slug = None
        if slug is None or not is_valid_file_slug(slug):
            n_skipped += 1
            messages.append(f"skip  {rel}  - invalid slug for path")
            continue

        ref = handler.ensure_ingested(slug, force=True)
        if ref is None:
            n_skipped += 1
            messages.append(f"fail  {rel}  - ingest returned None")
            continue
        # Stamp the ORIGINAL relative path (pre-``_dest_name`` rewrite) on
        # the ref's meta — the on-disk projection name is a lossy,
        # legible-only rewrite (``main.py`` -> ``main-py.txt``), so
        # :func:`stage_run_artifact`'s plaintext-reconstruction fallback
        # needs this to restore a runnable filename rather than the
        # rewritten one.
        hub.live_store.stamp_ref_meta(ref.id, {"harvest_orig_path": str(rel)})
        ref_ids.append(ref.id)
        messages.append(f"ok    {rel}  -> plaintext:{slug}")

    return ref_ids, n_skipped, messages


# ── mode:run staging: fetch a build's tarball into the run's /work ──


def stage_run_artifact(
    store: Store,
    *,
    folder_id: int,
    dest_dir: Path,
    artifact_store_root: Path | None = None,
    root: Path | None = None,
) -> dict[str, Any]:
    """Stage a prior ``mode:build`` harvest's code tree into ``dest_dir``
    (a ``mode:run`` container's ``/work`` root, NOT ``/work/out`` — the
    staged tree is what ``uv sync`` + ``RUN.json.cmd`` run against).

    Design contract ("Fetch verifies the sha; on miss, reconstruct from
    the folder's plaintext refs"): prefer the content-addressed tarball,
    sha256-verified; on a miss (tarball gone / corrupted — NAS hiccup,
    manual GC before a GC pass exists), fall back to reconstructing from
    the folder's ``plaintext`` child refs, restoring each file's
    ORIGINAL relative path from ``meta.harvest_orig_path`` (stamped by
    :func:`project_out`) — the on-disk projection name is a lossy,
    legible-only rewrite (``main.py`` -> ``main-py.txt``) and can't be
    run as-is.

    Returns the folder's ``meta`` dict (``image`` / ``artifact`` /
    ``run_recipe``) so the caller doesn't need a second lookup. Raises
    ``ValueError`` when the folder doesn't exist, or neither the tarball
    nor the plaintext fallback yields anything to stage.
    """
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta FROM refs WHERE ref_id = %s AND kind = 'folder' "
            "AND retired_at IS NULL",
            (folder_id,),
        ).fetchone()
    if row is None:
        raise ValueError(f"sandbox_run: folder:{folder_id} not found")
    folder_meta: dict[str, Any] = dict(row[0] or {})
    dest_dir.mkdir(parents=True, exist_ok=True)

    artifact = folder_meta.get("artifact")
    staged = False
    if isinstance(artifact, dict) and artifact.get("sha256"):
        sha256 = str(artifact["sha256"])
        if verify_artifact(sha256, root=artifact_store_root):
            tar_path = artifact_path(sha256, root=artifact_store_root)
            try:
                with tarfile.open(tar_path, mode="r:gz") as tar:
                    # ``filter="data"`` always exists — repo pins
                    # requires-python >=3.12 (Finding 5: no older-stdlib
                    # fallback to maintain).
                    tar.extractall(dest_dir, filter="data")
                staged = True
            except (OSError, tarfile.TarError) as exc:
                log.warning(
                    "sandbox_run stage: tarball extract failed for "
                    "folder:%d (falling back to plaintext refs): %s",
                    folder_id,
                    exc,
                )

    if not staged:
        n = _reconstruct_from_plaintext_refs(store, folder_id, dest_dir, root=root)
        if n == 0:
            raise ValueError(
                f"sandbox_run: folder:{folder_id} has no verifiable artifact "
                "tarball and no reconstructable plaintext refs — nothing to "
                "stage"
            )
        log.info(
            "sandbox_run stage: reconstructed %d file(s) for folder:%d from "
            "plaintext refs (tarball miss)",
            n,
            folder_id,
        )

    return folder_meta


def _reconstruct_from_plaintext_refs(
    store: Store, folder_id: int, dest_dir: Path, *, root: Path | None = None
) -> int:
    """Rebuild ``dest_dir`` from ``folder_id``'s ``plaintext`` children,
    restoring each file's original relative path/name. Returns the count
    of files written; ``0`` means nothing was reconstructable (no
    ``PRECIS_ROOT``, no stamped ``harvest_orig_path``, or the backing
    file is gone too)."""
    from precis.dispatch import Hub
    from precis.handlers.plaintext import PlaintextHandler

    effective_root = root
    if effective_root is None:
        from precis.config import load_config

        raw = load_config().root
        effective_root = Path(raw).expanduser().resolve() if raw else None
    if effective_root is None:
        return 0

    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT (SELECT id_value FROM ref_identifiers "
            "         WHERE ref_id = refs.ref_id AND id_kind = 'cite_key' "
            "         ORDER BY created_at DESC LIMIT 1) AS slug, "
            "       meta "
            "  FROM refs WHERE parent_id = %s "
            "   AND kind = 'plaintext' AND retired_at IS NULL",
            (folder_id,),
        ).fetchall()
    if not rows:
        return 0

    handler = PlaintextHandler(hub=Hub(store=store, embedder=None), root=effective_root)
    n = 0
    for slug, meta in rows:
        orig_rel = dict(meta or {}).get("harvest_orig_path")
        if not orig_rel or not slug:
            continue
        try:
            src = handler._resolve_path(str(slug), must_exist=True)
        except Exception:  # any resolve failure just skips this file
            continue
        if not src.is_file():
            continue
        dest = dest_dir / str(orig_rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(src.read_bytes())
        n += 1
    return n


# ── lineage: each build mints a new folder version ─────────────────


def _link_supersedes_lineage(store: Store, job_ref_id: int, folder_id: int) -> None:
    """Chain this harvest folder to the previous one from the same owning
    todo, if any (``supersedes`` edge — design: "each build mints a new
    folder version"). The lineage pointer lives on the job's *parent*
    todo (``meta.sandbox_harvest_folder_id``) since a fresh ``kind='job'``
    ref is minted per dispatch — the todo is the stable "same task,
    re-run" identity across builds.
    """
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT parent_id FROM refs WHERE ref_id = %s", (job_ref_id,)
        ).fetchone()
    todo_id = int(row[0]) if row and row[0] is not None else None
    if todo_id is None:
        return
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta->>'sandbox_harvest_folder_id' FROM refs WHERE ref_id = %s",
            (todo_id,),
        ).fetchone()
    prev_folder_id = int(row[0]) if row and row[0] else None
    if prev_folder_id is not None and prev_folder_id != folder_id:
        store.add_link(
            src_ref_id=folder_id, dst_ref_id=prev_folder_id, relation="supersedes"
        )
    store.stamp_ref_meta(todo_id, {"sandbox_harvest_folder_id": folder_id})


# ── orchestration ────────────────────────────────────────────────────


def harvest_out(
    store: Store,
    *,
    job_ref_id: int,
    container_name: str,
    work_dir: Path,
    image: str,
    model: str,
    root: Path | None = None,
    artifact_store_root: Path | None = None,
    extra_derived_from: int | None = None,
) -> HarvestResult:
    """Turn a container's exited-0 ``/work/out`` into the DB + NAS
    harvest — used for both a ``mode:build``'s code output and a
    ``mode:run``'s result output (design: "harvest result/forensics
    only" — same ``/work/out`` lane, same projection). Returns an empty
    :class:`HarvestResult` (``folder_ref_id is None``) when ``out/`` has
    no files — nothing to harvest (the "empty out/" taxonomy class; the
    job still succeeds on exit code, this only affects the summary
    text).

    ``root`` is ``PRECIS_ROOT`` (default: read from the env — ``None``
    when unset skips the plaintext projection with a logged warning; the
    tarball still lands).

    ``extra_derived_from`` (``mode:run`` only) links this run's result
    folder ``derived-from`` the build folder it re-ran (design: "run-of
    link to the build folder" — reusing the existing ``derived-from``
    relation rather than a new ``run-of`` migration; the job already
    carries the same relation to name *which run job* produced this
    folder, so a second ``derived-from`` edge to the build folder just
    names the other ancestor).
    """
    out_dir = work_dir / "out"
    _out_files, _out_symlinks_skipped = _iter_out_files(out_dir)
    if not _out_files:
        empty_messages: tuple[str, ...] = ("out/ empty — nothing harvested",)
        if _out_symlinks_skipped:
            empty_messages = (
                *empty_messages,
                f"({_out_symlinks_skipped} symlink(s) present but not "
                "projected — host containment)",
            )
        return HarvestResult(messages=empty_messages)

    run_recipe = parse_run_json(out_dir)
    artifact = build_tarball(out_dir, root=artifact_store_root)

    effective_root = root
    if effective_root is None:
        from precis.config import load_config

        raw = load_config().root
        effective_root = Path(raw).expanduser().resolve() if raw else None

    folder_meta: dict[str, Any] = {
        "job_id": job_ref_id,
        "image": image,
        "model": model,
        "artifact": artifact,
    }
    if run_recipe is not None:
        folder_meta["run_recipe"] = run_recipe
    if extra_derived_from is not None:
        folder_meta["run_of_folder_id"] = extra_derived_from

    folder = store.insert_ref(
        kind="folder",
        slug=None,
        title=f"sandbox_run {container_name} out",
        meta=folder_meta,
    )
    store.add_link(src_ref_id=folder.id, dst_ref_id=job_ref_id, relation="derived-from")
    if extra_derived_from is not None:
        store.add_link(
            src_ref_id=folder.id, dst_ref_id=extra_derived_from, relation="derived-from"
        )
    _link_supersedes_lineage(store, job_ref_id, folder.id)

    ref_ids: list[int] = []
    n_skipped = 0
    messages: list[str] = []
    if effective_root is not None:
        from precis.dispatch import Hub

        hub = Hub(store=store, embedder=None)
        ref_ids, n_skipped, messages = project_out(
            hub,
            out_dir=out_dir,
            dest_root=effective_root,
            harvest_subdir=f"sandbox/{container_name}",
        )
        for rid in ref_ids:
            store.set_parent(rid, folder.id)
    else:
        messages.append(
            "PRECIS_ROOT not set — tarball harvested, no plaintext projection"
        )
        log.warning(
            "sandbox_run harvest: PRECIS_ROOT unset — skipping plaintext "
            "projection for job %d (tarball still harvested)",
            job_ref_id,
        )

    meta_updates: dict[str, Any] = {
        "artifact": artifact,
        "harvest_folder_id": folder.id,
    }
    if run_recipe is not None:
        meta_updates["run_recipe"] = run_recipe
    store.stamp_ref_meta(job_ref_id, meta_updates)

    return HarvestResult(
        folder_ref_id=folder.id,
        n_projected=len(ref_ids),
        n_skipped=n_skipped,
        artifact=artifact,
        run_recipe=run_recipe,
        messages=tuple(messages),
    )


def summarize(result: HarvestResult) -> str:
    """One-line, taxonomy-labeled summary for the ``job_summary`` chunk."""
    if result.folder_ref_id is None:
        return "out/ empty — nothing harvested."
    bits = [f"harvested folder:{result.folder_ref_id} ({result.n_projected} file(s)"]
    if result.n_skipped:
        bits[-1] += f", {result.n_skipped} skipped"
    bits[-1] += ")."
    if result.artifact:
        bits.append(
            f"artifact sha256={str(result.artifact['sha256'])[:12]}… "
            f"({result.artifact['size']}B)."
        )
    if result.run_recipe is not None:
        bits.append("RUN.json recipe parsed.")
    return " ".join(bits)


__all__ = [
    "ARTIFACT_SUBDIR",
    "MAX_HARVEST_FILE_BYTES",
    "HarvestResult",
    "artifact_key",
    "artifact_path",
    "artifact_root",
    "build_tarball",
    "harvest_out",
    "parse_run_json",
    "project_out",
    "stage_run_artifact",
    "summarize",
    "verify_artifact",
]
