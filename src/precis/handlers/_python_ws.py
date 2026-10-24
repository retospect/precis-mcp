"""DB-backed ``ws-<slug>`` roots for the python kind —
structure-workspaces slice 2p (docs/backlog/structure-workspaces.md
§Join with the python kind).

A structure workspace's code is a ``folder`` ref — ``meta['workspace']``
marks it and names the linked design; its files are the folder's
``plaintext`` children (the same tree ``stage_code`` stages) whose
relative path is ``meta.workspace_path`` or, for harvested folders,
``meta.harvest_orig_path``. The root ``ws-<slug>`` materialises that
tree into a cache dir (``~/.cache/precis/ws-roots/<slug>`` by default)
so the whole python surface — ``source``, ``callers``, eyes — works on
workspace code exactly as on ``main::…``.

A root exists only once the workspace has a run: the design's
``meta['workspaces'][<slug>]`` (written by
``precis_se.workspace_import.import_workspace_run``) records the last
import's ``run_job``/``script_sha``. The folder is indexed at its
current content — the spec's "at the last run's ``script_sha``"
checkout becomes real with slice 1b (git objects in the DB); until then
the run record is the gate, not the pin.

``precis`` cannot import ``precis_se`` (layering — precis_se sits on
precis), so the trace reads behind ``view='placed'`` and the eye's
Placed group speak plain SQL over ``refs``/``chunks``, the same two
tables ``precis_se.persist._read_ws_trace`` walks. The trace doc is the
JSON of a ``ws_trace`` chunk; only the keys ``frames[].qual|key|parent|
id|id_end|made|touched`` and ``run.workspace`` are read.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path, PurePosixPath
from typing import Any, NamedTuple

from precis.errors import BadInput, NotFound

log = logging.getLogger(__name__)

_PREFIX = "ws-"
_DEFAULT_STAGE = Path.home() / ".cache" / "precis" / "ws-roots"

#: One ``refs``/``ref_identifiers`` row → the workspace folder. Matches
#: either convention: the folder's own cite_key slug is ``ws-<slug>``,
#: or ``meta['workspace']['slug']`` names it.
_FOLDER_SQL = """
SELECT r.ref_id, r.meta FROM refs r
WHERE r.kind = 'folder' AND r.retired_at IS NULL
  AND (r.meta->'workspace'->>'slug' = %s
       OR (SELECT ri.id_value FROM ref_identifiers ri
            WHERE ri.ref_id = r.ref_id AND ri.id_kind = 'cite_key'
            LIMIT 1) = %s)
"""

#: File children of the folder — the ``plaintext`` refs
#: ``stage_code``/the workspace editor write.
_FILES_SQL = """
SELECT ref_id,
       COALESCE(meta->>'workspace_path', meta->>'harvest_orig_path')
  FROM refs
 WHERE parent_id = %s AND kind = 'plaintext' AND retired_at IS NULL
"""

#: Designs the workspace has run into — ``meta['workspaces'][<slug>]``
#: is the slice-0 importer's ownership record.
_DESIGNS_SQL = """
SELECT r.ref_id, r.kind,
       (SELECT ri.id_value FROM ref_identifiers ri
         WHERE ri.ref_id = r.ref_id AND ri.id_kind = 'cite_key'
         LIMIT 1) AS slug,
       r.meta->'workspaces'->%s AS entry
  FROM refs r
 WHERE r.retired_at IS NULL AND r.meta->'workspaces' ? %s
 ORDER BY 3, 1
"""

_TRACE_SQL = (
    "SELECT text FROM chunks WHERE ref_id = %s "
    "AND chunk_kind = 'ws_trace' ORDER BY ord DESC"
)


def _id_span(fr: dict[str, Any]) -> range:
    """The run-local ids one emitted frame covers (``id..id_end``)."""
    end = fr.get("id_end")
    return range(int(fr["id"]), int(end) + 1 if end is not None else int(fr["id"]) + 1)


def _add_counts(acc: dict[str, int], counts: dict[str, Any] | None) -> None:
    """Sum one ``made``/``touched`` dict into ``acc`` — ints plus the
    lengths of ordinal ``[lo, hi]`` ranges (``traceview._counts``)."""
    for entity, v in (counts or {}).items():
        if isinstance(v, int):
            acc[entity] = acc.get(entity, 0) + v
        elif isinstance(v, list):
            for r in v:
                if isinstance(r, (list, tuple)) and len(r) >= 2:
                    acc[entity] = acc.get(entity, 0) + int(r[1]) - int(r[0]) + 1


def _counts_text(counts: dict[str, int]) -> str:
    """``made``/``touched`` as ``24 atoms 48 bonds``."""
    return " ".join(f"{n} {entity}" for entity, n in sorted(counts.items()))


class PlacedRow(NamedTuple):
    """One design's row in ``view='placed'`` / the eye's ``Placed:``
    group — the frames whose ``qual`` matched, subtree counts included."""

    design: str
    occurrences: int
    made: dict[str, int]
    touched: dict[str, int]
    selector: str
    run: int | None


def render_placed_row(r: PlacedRow) -> str:
    """``hcol-array-1: 3 occurrences · made 72 atoms 144 bonds · touched
    96 · frame:qual=main.bundle · run 17`` (spec §Structure fisheye)."""
    parts = [
        f"{r.design}: {r.occurrences} occurrence"
        f"{'s' if r.occurrences != 1 else ''} · made {_counts_text(r.made) or '—'}"
    ]
    touched = _counts_text(r.touched)
    if touched:
        parts.append(f"touched {touched}")
    parts.append(r.selector)
    if r.run is not None:
        parts.append(f"run {r.run}")
    return " · ".join(parts)


def render_placed(alias: str, qualname: str, rows: list[PlacedRow]) -> str:
    """The ``view='placed'`` body: the id, then one line per design."""
    lines = [f"placed: {alias}::{qualname}"]
    if not rows:
        lines.append("no design built from this workspace places it")
    lines.extend(render_placed_row(r) for r in rows)
    return "\n".join(lines)


class WorkspaceRoots:
    """``ws-<slug>`` alias → a materialised on-disk index root.

    ``resolve`` raises :class:`NotFound` for an unknown workspace (no
    folder ref) and for an unrun one (folder but no import record) —
    "an unrun workspace has no root". ``store=None`` makes every alias
    unknown (stateless deployment mode).
    """

    def __init__(self, store: Any, stage_dir: Path | str | None = None) -> None:
        self._store = store
        self._dir = Path(stage_dir) if stage_dir is not None else _DEFAULT_STAGE

    @staticmethod
    def owns(alias: str) -> bool:
        return alias.startswith(_PREFIX) and len(alias) > len(_PREFIX)

    @staticmethod
    def slug_of(alias: str) -> str:
        return alias[len(_PREFIX) :]

    def resolve(self, alias: str) -> Path:
        slug = self.slug_of(alias)
        if not slug or self._store is None:
            raise NotFound(
                f"unknown workspace root {alias!r}",
                next="get(kind='python') lists configured roots",
            )
        folder = self._folder(slug)
        if folder is None:
            raise NotFound(
                f"unknown workspace root {alias!r} — no folder ref "
                f"carries meta.workspace.slug = {slug!r}",
                next="get(kind='python') lists configured roots; a "
                "ws-<slug> root appears once a workspace exists",
            )
        folder_id, ws_meta = folder
        if self._run_record(slug, ws_meta) is None:
            design = ws_meta.get("design")
            raise NotFound(
                f"workspace {slug!r} has no run — a ws- root indexes the "
                "code at the last run's script_sha",
                next=(
                    f"get(kind='se', id={str(design).split(':')[-1]!r}) — "
                    "the design's meta.workspaces records workspace imports"
                    if design
                    else "get(kind='python') lists configured roots"
                ),
            )
        return self._stage(slug, self._files(folder_id))

    # ── DB lookups ──────────────────────────────────────────────────

    def _folder(self, slug: str) -> tuple[int, dict[str, Any]] | None:
        """``(ref_id, meta['workspace'])`` of the workspace's folder."""
        with self._store.pool.connection() as conn:
            rows = conn.execute(_FOLDER_SQL, (slug, f"{_PREFIX}{slug}")).fetchall()
        for ref_id, meta in rows:
            ws = (meta or {}).get("workspace")
            if isinstance(ws, dict) and ws.get("slug", slug) == slug:
                return int(ref_id), dict(ws)
        return None

    def _run_record(self, slug: str, ws_meta: dict[str, Any]) -> dict[str, Any] | None:
        """The design's ``meta['workspaces'][<slug>]`` — the has-a-run
        record the importer stamps (``run_job``/``script_sha``)."""
        design = ws_meta.get("design")
        if not design:
            return None
        kind, sep, dslug = str(design).partition(":")
        if not sep:
            kind, dslug = "se", str(design)
        try:
            ref = self._store.get_ref(kind=kind, id=dslug)
        except Exception:
            return None
        if ref is None:
            return None
        entry = ((ref.meta or {}).get("workspaces") or {}).get(slug)
        return dict(entry) if isinstance(entry, dict) else None

    def _files(self, folder_id: int) -> dict[str, str]:
        """relpath → text for the folder's ``.py`` children (chunk
        bodies — the DB is the store, not the file projection)."""
        with self._store.pool.connection() as conn:
            rows = conn.execute(_FILES_SQL, (folder_id,)).fetchall()
        out: dict[str, str] = {}
        for ref_id, rel in rows:
            if not rel or not str(rel).endswith(".py"):
                continue
            path = PurePosixPath(str(rel))
            if path.is_absolute() or ".." in path.parts:
                log.warning("ws root: skipping out-of-tree path %r", rel)
                continue
            text = "".join(
                c.text for c in self._store.chunks.list_chunks_for_ref(int(ref_id))
            )
            out[path.as_posix()] = text
        return out

    def _stage(self, slug: str, files: dict[str, str]) -> Path:
        """Materialise the folder under ``stage_dir/<slug>`` and return
        it as an indexable root. A sha256 fingerprint of the file set
        skips the rewrite when nothing changed (the ``.py`` set only —
        ``uv.lock`` churn doesn't reindex)."""
        base = self._dir / slug
        canon = "".join(
            f"{rel}\x00{hashlib.sha256(t.encode('utf-8')).hexdigest()}\n"
            for rel, t in sorted(files.items())
        )
        fp = hashlib.sha256(canon.encode("utf-8")).hexdigest()
        stamp = base / ".fingerprint"
        if base.is_dir() and stamp.is_file():
            try:
                if stamp.read_text(encoding="utf-8").strip() == fp:
                    return base
            except OSError:
                pass
        base.mkdir(parents=True, exist_ok=True)
        for stale in base.rglob("*.py"):
            stale.unlink()
        for rel, text in files.items():
            dest = base / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(text, encoding="utf-8")
        stamp.write_text(fp + "\n", encoding="utf-8")
        return base

    # ── placed: designs built from the workspace ────────────────────

    def _trace_doc(self, home: int) -> dict[str, Any] | None:
        """The newest parseable ``ws_trace`` chunk on ``home`` (a run
        job or, for a containerless import, the design ref)."""
        with self._store.pool.connection() as conn:
            rows = conn.execute(_TRACE_SQL, (home,)).fetchall()
        for (text,) in rows:
            try:
                doc = json.loads(text)
            except (TypeError, ValueError):
                continue
            if isinstance(doc, dict) and isinstance(doc.get("frames"), list):
                return doc
        return None

    def placed(
        self, slug: str, qualname: str, *, run: int | str | None = None
    ) -> list[PlacedRow]:
        """``view='placed'`` rows: per design built from this workspace,
        the frames whose ``qual`` is ``qualname`` — occurrences and
        subtree ``made``/``touched`` sums. ``run`` pins the trace read
        to one home ref: that ref must exist and hold a ``ws_trace``
        chunk of this workspace's run (BadInput otherwise); default is
        the design's latest import."""
        if self._store is None:
            return []
        home: int | None = None
        if run is not None:
            home = int(run)
            if self._store.get_ref(kind="job", id=home) is None:
                raise BadInput(
                    f"run {home} is not a job ref",
                    next="args={'run': <run job ref id>} — the design's "
                    "meta.workspaces names the latest run's job",
                )
        with self._store.pool.connection() as conn:
            designs = conn.execute(_DESIGNS_SQL, (slug, slug)).fetchall()
        out: list[PlacedRow] = []
        for ref_id, _kind, dslug, entry in designs:
            entry = dict(entry) if isinstance(entry, dict) else {}
            doc = (
                self._trace_doc(home)
                if home is not None
                else self._trace_doc(entry.get("run_job") or int(ref_id))
            )
            if home is not None and doc is None:
                raise BadInput(
                    f"run {home} holds no ws_trace chunk",
                    next="args={'run': <run job ref id>} — an imported "
                    "run stores its trace on its job ref",
                )
            if doc is None:
                continue
            run_ws = str((doc.get("run") or {}).get("workspace") or "")
            if home is not None and run_ws != slug:
                raise BadInput(
                    f"run {home} is a run of workspace {run_ws!r}, not {slug!r}",
                    next=f"get(kind='python', id='ws-{run_ws or '<slug>'}::"
                    "<qualname>', view='placed')",
                )
            frames = doc["frames"]
            by_key = {f["key"]: f for f in frames if f.get("key") is not None}
            children: dict[Any, list[dict[str, Any]]] = {}
            for f in frames:
                children.setdefault(f.get("parent"), []).append(f)
            hits = [f for f in frames if str(f.get("qual")) == qualname]
            made: dict[str, int] = {}
            touched: dict[str, int] = {}
            # Walk each hit's subtree by KEY (Trace.subtree): each frame
            # contributes once — a collapsed frame's id..id_end span is
            # still one frame — and a frame shared by two occurrences'
            # subtrees counts once across hits.
            seen: set[Any] = set()
            for f in hits:
                stack = [f["key"]]
                while stack:
                    key = stack.pop()
                    if key in seen:
                        continue
                    seen.add(key)
                    fr = by_key.get(key)
                    if fr is None:
                        continue
                    stack.extend(c["key"] for c in children.get(key, []))
                    _add_counts(made, fr.get("made"))
                    _add_counts(touched, fr.get("touched"))
            out.append(
                PlacedRow(
                    design=str(dslug or ref_id),
                    occurrences=len(hits),
                    made=made,
                    touched=touched,
                    selector=f"frame:qual={qualname}",
                    run=home if home is not None else entry.get("run_job"),
                )
            )
        return out
