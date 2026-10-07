"""Explicit file/graph coexistence, separate from the one-shot cutover importer.

Namespace advisory locks serialize mirror creation without a schema change;
ordered NO KEY UPDATE ref locks serialize body edits without blocking mention
FKs. A saved graph digest refuses divergent edits instead of choosing a winner.
Files are snapshots, rechecked before commit, not a distributed transaction.
Only file-local wiki/Markdown links are resolved; native mention autolinking
uses extra pooled reads and cannot join this atomic import. Existing links
are preserved, including refusal before deleting a chunk with anchored links.
Export only creates new directories: refusing existing destinations avoids
silently replacing local edits without pretending a file lock binds editors.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from psycopg import Connection

from precis.cli.memory import GRAPH_MARKER, ImportRefused
from precis.store import ChunkInsert, Store, Tag
from precis.utils.text import slugify

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
_WIKI = re.compile(r"\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]")
_MARKDOWN = re.compile(r"\[[^\]]*\]\(([^)\s#]+\.md)(?:#[^)]*)?\)")
_KEY = "file_mirror"


class _UniqueLoader(yaml.SafeLoader):
    """Refuse YAML keys that safe_load would silently overwrite."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> Any:
        pairs = self.construct_pairs(node, deep=deep)
        result: dict[str, Any] = {}
        for key, value in pairs:
            if not isinstance(key, str) or key in result:
                raise ImportRefused(f"duplicate or non-string YAML key: {key!r}")
            result[key] = value
        return result


@dataclass
class MirrorReport:
    """Counts and explicit unresolved/missing names; no implied retirement."""

    created: int = 0
    updated: int = 0
    unchanged: int = 0
    missing: list[str] = field(default_factory=list)
    unresolved: list[tuple[str, str]] = field(default_factory=list)
    refs: dict[str, int] = field(default_factory=dict)
    #: Export: live SPACE:repo-dev handles with no mirror key here; none written.
    unexported: list[str] = field(default_factory=list)
    #: Import: legacy one-shot nodes soft-deleted by ``legacy='retire'``.
    retired: list[str] = field(default_factory=list)
    #: Export: native nodes (no ``file_mirror``) rendered to new files and stamped.
    exported_native: list[str] = field(default_factory=list)
    #: Import: legacy nodes adopted in place by ``legacy='refresh'`` (not in created).
    refreshed: list[str] = field(default_factory=list)


@dataclass
class _File:
    name: str
    header: str
    body: str
    frontmatter: dict[str, Any]

    @property
    def title(self) -> str:
        return str(self.frontmatter.get("name", self.name))

    @property
    def hook(self) -> str:
        return str(self.frontmatter.get("description", ""))


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
    ).hexdigest()


def _namespace(value: str) -> None:
    if not _NAME.fullmatch(value):
        raise ImportRefused("namespace must use letters, digits, dots, _ or -")


def _filename(value: str) -> None:
    if not value.endswith(".md") or not _NAME.fullmatch(value):
        raise ImportRefused(f"only flat Markdown filenames are supported: {value!r}")


def _parse(name: str, raw: bytes) -> _File:
    _filename(name)
    try:
        text = raw.decode("utf-8")
    except UnicodeError as exc:
        raise ImportRefused(f"{name}: expected UTF-8") from exc
    if name == "MEMORY.md":
        if GRAPH_MARKER in text:
            raise ImportRefused("MEMORY.md is a graph pointer, not a source index")
        return _File(name, "", text, {})
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise ImportRefused(f"{name}: YAML frontmatter required")
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        raise ImportRefused(f"{name}: unclosed YAML frontmatter")
    try:
        fm = yaml.load("".join(lines[1:end]), Loader=_UniqueLoader)
        json.dumps(fm, allow_nan=False)
    except (yaml.YAMLError, TypeError, ValueError, RecursionError) as exc:
        raise ImportRefused(f"{name}: invalid JSON-compatible YAML: {exc}") from exc
    if not isinstance(fm, dict):
        raise ImportRefused(f"{name}: YAML must be a mapping")
    for key in ("name", "description"):
        if not isinstance(fm.get(key), str) or not fm[key].strip():
            raise ImportRefused(f"{name}: nonempty YAML {key} required")
    meta = fm.get("metadata")
    if not isinstance(meta, dict) or meta.get("type") not in (
        "user",
        "feedback",
        "project",
        "reference",
    ):
        raise ImportRefused(
            f"{name}: metadata.type must be user/feedback/project/reference"
        )
    body = "".join(lines[end + 1 :])
    if not body.strip():
        raise ImportRefused(f"{name}: empty memory body")
    return _File(name, "".join(lines[: end + 1]), body, fm)


def _read_files(root: Path) -> dict[str, bytes]:
    if root.is_symlink() or not root.is_dir():
        raise ImportRefused("source must be a real directory")
    files: dict[str, bytes] = {}
    folded: set[str] = set()
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for name in sorted(os.listdir(root_fd)):
            info = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode):
                raise ImportRefused(f"only regular files are supported: {name}")
            if Path(name).suffix.lower() != ".md":
                continue
            _filename(name)
            if name.casefold() in folded:
                raise ImportRefused(f"case-folded filename collision: {name}")
            folded.add(name.casefold())
            fd = os.open(
                name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root_fd
            )
            with os.fdopen(fd, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ImportRefused(f"not a regular file: {name}")
                files[name] = stream.read()
    finally:
        os.close(root_fd)
    if not files:
        raise ImportRefused("source has no Markdown files")
    return files


def _targets(body: str) -> set[str]:
    targets: set[str] = set()
    for raw in (*_WIKI.findall(body), *_MARKDOWN.findall(body)):
        if "://" in raw:
            continue
        name = raw.strip()
        if not name.endswith(".md"):
            name += ".md"
        _filename(name)
        targets.add(name)
    return targets


def _lock(conn: Connection, namespace: str) -> None:
    conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"precis-memory-mirror:{namespace}",),
    )


def _nodes(
    conn: Connection, namespace: str
) -> dict[str, tuple[int, str, dict[str, Any]]]:
    rows = conn.execute(
        "SELECT ref_id, title, meta, retired_at FROM refs WHERE kind='memory' "
        "AND meta->'file_mirror'->>'namespace' = %s "
        "ORDER BY ref_id FOR NO KEY UPDATE",
        (namespace,),
    ).fetchall()
    nodes: dict[str, tuple[int, str, dict[str, Any]]] = {}
    folded: set[str] = set()
    for rid, title, meta, retired in rows:
        name = meta[_KEY]["filename"]
        _filename(name)
        if name.casefold() in folded or retired is not None:
            raise ImportRefused(f"duplicate/retired mirror identity: {name}")
        folded.add(name.casefold())
        nodes[name] = (rid, title, meta)
    return nodes


def _body(conn: Connection, rid: int) -> str:
    rows = conn.execute(
        "SELECT text FROM chunks WHERE ref_id=%s AND chunk_kind='memory_body' "
        "ORDER BY ord",
        (rid,),
    ).fetchall()
    if len(rows) != 1:
        raise ImportRefused(f"memory {rid}: expected one body chunk")
    return str(rows[0][0])


def _edges(conn: Connection, rid: int, namespace: str) -> list[Any]:
    return conn.execute(
        "SELECT dst_ref_id, src_chunk_id, dst_chunk_id, relation, set_by, meta "
        "FROM links WHERE src_ref_id=%s AND meta->>'source'='file-mirror' "
        "AND meta->>'namespace'=%s ORDER BY link_id FOR UPDATE",
        (rid, namespace),
    ).fetchall()


def _state(
    conn: Connection, rid: int, title: str, meta: dict[str, Any], namespace: str
) -> str:
    return _digest(
        [
            title,
            meta.get("hook"),
            meta.get("frontmatter"),
            _body(conn, rid),
            _edges(conn, rid, namespace),
        ]
    )


def _legacy_nodes(conn: Connection, titles: set[str]) -> list[tuple[int, str]]:
    """Live repo-dev memories with no ``file_mirror`` key titled like a topic."""
    rows = conn.execute(
        "SELECT r.ref_id, r.title FROM refs r WHERE r.kind='memory' "
        "AND r.retired_at IS NULL AND NOT (r.meta ? %s) AND r.title = ANY(%s) "
        "AND EXISTS (SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id=rt.tag_id "
        "WHERE rt.ref_id=r.ref_id AND t.namespace='SPACE' AND t.value='repo-dev' "
        "AND (rt.expires_at IS NULL OR rt.expires_at > now())) "
        "ORDER BY r.ref_id FOR NO KEY UPDATE OF r",
        (_KEY, sorted(titles)),
    ).fetchall()
    return [(int(rid), str(title)) for rid, title in rows]


def _unexported(conn: Connection, namespace: str) -> list[str]:
    """Handles of live repo-dev memories this namespace's export leaves out."""
    rows = conn.execute(
        "SELECT r.ref_id FROM refs r WHERE r.kind='memory' AND r.retired_at IS NULL "
        "AND COALESCE(r.meta->'file_mirror'->>'namespace', '') <> %s "
        "AND EXISTS (SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id=rt.tag_id "
        "WHERE rt.ref_id=r.ref_id AND t.namespace='SPACE' AND t.value='repo-dev' "
        "AND (rt.expires_at IS NULL OR rt.expires_at > now())) ORDER BY r.ref_id",
        (namespace,),
    ).fetchall()
    return [f"me{row[0]}" for row in rows]


_TYPES = ("user", "feedback", "project", "reference")


def _natives(conn: Connection) -> list[tuple[int, str, dict[str, Any]]]:
    """Live repo-dev memories with no ``file_mirror`` key and a title, locked."""
    rows = conn.execute(
        "SELECT r.ref_id, r.title, r.meta FROM refs r WHERE r.kind='memory' "
        "AND r.retired_at IS NULL AND NOT (r.meta ? %s) AND btrim(r.title) <> '' "
        "AND EXISTS (SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id=rt.tag_id "
        "WHERE rt.ref_id=r.ref_id AND t.namespace='SPACE' AND t.value='repo-dev' "
        "AND (rt.expires_at IS NULL OR rt.expires_at > now())) "
        "ORDER BY r.ref_id FOR NO KEY UPDATE OF r",
        (_KEY,),
    ).fetchall()
    return [(int(rid), str(title), dict(meta or {})) for rid, title, meta in rows]


def _describe(meta: dict[str, Any], body: str, title: str) -> str:
    """``meta.hook``, else the body's first non-empty line (sans ``#``), else title."""
    hook = str(meta.get("hook") or "").strip()
    if hook:
        return hook
    for line in body.splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return line[:160].rstrip()
    return title.strip()


def _render_native(
    slug: str, meta: dict[str, Any], body: str, title: str
) -> tuple[str, dict[str, Any]]:
    """Header text and frontmatter dict for a native node under the export policy."""
    kind = meta.get("type")
    fm: dict[str, Any] = {
        "name": slug,
        "description": _describe(meta, body, title),
        "metadata": {"type": kind if kind in _TYPES else "project"},
    }
    dumped = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True)
    return f"---\n{dumped}---\n", fm


def _guard_chunk_links(conn: Connection, rid: int) -> None:
    # Replacing a chunk must not cascade-delete another author's anchors.
    # Lock chunks before a fresh link read, so concurrent FK inserts cannot
    # slip between the check and deletion.
    chunks = conn.execute(
        "SELECT chunk_id FROM chunks WHERE ref_id=%s "
        "AND chunk_kind='memory_body' ORDER BY chunk_id FOR UPDATE",
        (rid,),
    ).fetchall()
    ids = [row[0] for row in chunks]
    linked = conn.execute(
        "SELECT 1 FROM links WHERE src_chunk_id=ANY(%s) OR dst_chunk_id=ANY(%s) LIMIT 1",
        (ids, ids),
    ).fetchone()
    if linked:
        raise ImportRefused(
            f"memory {rid}: body has chunk-anchored links; reconcile explicitly"
        )


def import_mirror(
    store: Store, source: Path, *, namespace: str, legacy: str = "refuse"
) -> MirrorReport:
    """Atomically import a file snapshot; refuse conflicting graph changes.

    ``legacy`` decides what happens to live repo-dev nodes made by the one-shot
    importer (no ``file_mirror`` key, title equal to a topic's ``name:``):
    ``refuse`` (default) raises, ``retire`` soft-deletes them in this
    transaction, ``keep`` imports beside them, ``refresh`` adopts each in place
    (same ref_id and inbound links, ``file_mirror`` stamped, body and links
    rewritten as for a new node; several nodes with one title refuse).
    """
    _namespace(namespace)
    if legacy not in ("refuse", "retire", "keep", "refresh"):
        raise ImportRefused("legacy must be refuse, retire, keep or refresh")
    report = MirrorReport()
    with store.tx() as conn:
        _lock(conn, namespace)
        raw = _read_files(source)
        files = {name: _parse(name, data) for name, data in raw.items()}
        targets = {name: _targets(f.body) for name, f in files.items()}
        nodes = _nodes(conn, namespace)
        adopt: dict[str, int] = {}
        if legacy != "keep":
            found = _legacy_nodes(
                conn,
                {f.title for n, f in files.items() if n != "MEMORY.md"},
            )
            if found and legacy == "refuse":
                listing = "; ".join(f"me{rid} {title!r}" for rid, title in found)
                raise ImportRefused(
                    f"legacy nodes without file_mirror share topic titles: {listing}; "
                    "re-run with legacy='retire' (or --legacy retire) to soft-delete "
                    "them, or 'keep' to import beside them"
                )
            if legacy == "refresh":
                by_title: dict[str, list[int]] = {}
                for rid, title in found:
                    by_title.setdefault(title, []).append(rid)
                for title, rids in by_title.items():
                    if len(rids) > 1:
                        listing = ", ".join(f"me{r}" for r in rids)
                        raise ImportRefused(
                            f"ambiguous legacy nodes for title {title!r}: {listing}; "
                            "retire all but one first"
                        )
                for n, f in files.items():
                    if n != "MEMORY.md" and n not in nodes and f.title in by_title:
                        rid = by_title[f.title][0]
                        if rid in adopt.values():
                            raise ImportRefused(
                                f"me{rid}: several files share title {f.title!r}"
                            )
                        adopt[n] = rid
            else:
                for rid, _title in found:
                    store.retire_ref(rid, conn=conn)
                    report.retired.append(f"me{rid}")
        folded = {name.casefold(): name for name in nodes}
        for name in files:
            if name.casefold() in folded and folded[name.casefold()] != name:
                raise ImportRefused(f"filename collides with graph identity: {name}")
        report.missing = sorted(set(nodes) - set(files))
        # Validate all existing inputs before the first write. Missing files stay live.
        for name in files.keys() & nodes.keys():
            rid, title, meta = nodes[name]
            if _state(conn, rid, title, meta, namespace) != meta[_KEY]["graph_digest"]:
                raise ImportRefused(
                    f"{name}: graph changed since import; export/reconcile first"
                )
        changed: set[str] = set()
        for name, f in files.items():
            if name in nodes:
                rid, title, meta = nodes[name]
                if _digest(raw[name].decode()) == meta[_KEY]["file_digest"]:
                    continue
                if _body(conn, rid) != f.body:
                    _guard_chunk_links(conn, rid)
                    store.chunks.replace_body_chunk(
                        rid,
                        f.body,
                        chunk_kind="memory_body",
                        source="memory-mirror",
                        conn=conn,
                    )
                if title != f.title:
                    store.chunks.set_ref_title(
                        rid, f.title, source="memory-mirror", conn=conn
                    )
                report.updated += 1
            elif name in adopt:
                rid = adopt[name]
                row = conn.execute(
                    "SELECT meta FROM refs WHERE ref_id=%s", (rid,)
                ).fetchone()
                meta = dict(row[0] or {}) if row else {}
                if _body(conn, rid) != f.body:
                    _guard_chunk_links(conn, rid)
                    store.chunks.replace_body_chunk(
                        rid,
                        f.body,
                        chunk_kind="memory_body",
                        source="memory-mirror",
                        conn=conn,
                    )
                for tag in ["SPACE:repo-dev", f"mirror:{namespace}"]:
                    store.add_tag(rid, Tag.parse_strict(tag, kind="memory"), conn=conn)
                report.refreshed.append(f"me{rid}")
            else:
                ref = store.insert_ref(
                    kind="memory", slug=None, title=f.title, conn=conn
                )
                rid, meta = ref.id, {}
                store.chunks.insert_chunks(
                    rid,
                    [
                        ChunkInsert(
                            ord=0, text=f.body, meta={"chunk_kind": "memory_body"}
                        )
                    ],
                    conn=conn,
                )
                for tag in ["SPACE:repo-dev", f"mirror:{namespace}"] + (
                    ["section:index"] if name == "MEMORY.md" else []
                ):
                    store.add_tag(rid, Tag.parse_strict(tag, kind="memory"), conn=conn)
                report.created += 1
            mirror: dict[str, Any] = {
                "namespace": namespace,
                "filename": name,
                "header": f.header,
                "file_digest": _digest(raw[name].decode()),
            }
            meta = {
                **meta,
                "hook": f.hook,
                "frontmatter": f.frontmatter,
                "order": 0,
                _KEY: mirror,
            }
            store.update_ref(rid, meta_patch=meta, conn=conn)
            nodes[name] = (rid, f.title, meta)
            changed.add(name)
        # All refs now exist: forward references and newly available targets resolve.
        for name, f in files.items():
            rid, title, meta = nodes[name]
            unresolved = sorted(targets[name] - nodes.keys())
            report.unresolved.extend((name, target) for target in unresolved)
            want = {nodes[t][0] for t in targets[name] & nodes.keys() if t != name}
            have = {row[0] for row in _edges(conn, rid, namespace)}
            if want != have or meta[_KEY].get("unresolved", []) != unresolved:
                if name not in changed:
                    report.updated += 1
                    changed.add(name)
                conn.execute(
                    "DELETE FROM links WHERE src_ref_id=%s AND meta->>'source'='file-mirror' "
                    "AND meta->>'namespace'=%s AND NOT (dst_ref_id=ANY(%s))",
                    (rid, namespace, list(want)),
                )
                for target in sorted(want - have):
                    edge = store.add_link(
                        src_ref_id=rid,
                        dst_ref_id=target,
                        relation="related-to",
                        meta={"source": "file-mirror", "namespace": namespace},
                        conn=conn,
                    )
                    if edge.meta != {"source": "file-mirror", "namespace": namespace}:
                        raise ImportRefused(
                            f"{name}: link to {target} already has other provenance"
                        )
            if name in changed:
                mirror = {**meta[_KEY], "unresolved": unresolved}
                mirror["graph_digest"] = _state(conn, rid, title, meta, namespace)
                store.update_ref(rid, meta_patch={_KEY: mirror}, conn=conn)
            else:
                report.unchanged += 1
        report.refs = {name: row[0] for name, row in nodes.items()}
        if _read_files(source) != raw:
            raise ImportRefused("files changed during import; transaction rolled back")
    return report


def export_mirror(store: Store, dest: Path, *, namespace: str) -> MirrorReport:
    """Export a locked snapshot to an exclusively new directory; never overwrite.

    Nodes carrying this namespace's ``file_mirror`` key are written byte-faithfully.
    A live repo-dev node with no ``file_mirror`` key and a title is rendered to a
    new ``<slug>.md`` (slug of the title, ``-<ref_id>`` on a filename clash;
    ``name:`` = slug, ``description:`` = ``meta.hook`` (else the body's first line, else the
    title; stamped back as ``meta.hook``), ``metadata.type`` =
    ``meta.type`` when one of user/feedback/project/reference, else ``project``)
    and stamped with ``file_mirror`` in the same transaction, so the next import
    sees an ordinary mirror node (its title becomes the slug, as import would
    set it); handles go in ``exported_native``. ``created`` counts every file
    written. ``unexported`` lists the remaining live repo-dev
    nodes (another namespace's, or an empty or unsluggable title).
    """
    _namespace(namespace)
    report = MirrorReport()
    output: dict[str, bytes] = {}
    with store.tx() as conn:
        _lock(conn, namespace)
        nodes = _nodes(conn, namespace)
        if not nodes:
            raise ImportRefused(f"no mirrored memories in namespace {namespace!r}")
        for name, (rid, title, meta) in nodes.items():
            body = _body(conn, rid)
            raw = (meta[_KEY]["header"] + body).encode("utf-8")
            f = _parse(name, raw)
            if (
                title != f.title
                or meta.get("hook") != f.hook
                or meta.get("frontmatter") != f.frontmatter
            ):
                raise ImportRefused(
                    f"{name}: graph metadata differs from YAML; explicit reconciliation required"
                )
            output[name] = raw
        taken = {name.casefold() for name in output}
        stamps: list[
            tuple[int, str, str, str, dict[str, Any], dict[str, Any], bytes]
        ] = []
        for rid, title, meta in _natives(conn):
            base = slugify(title).replace("-", "_")
            if not base:
                continue
            slug = base if f"{base}.md".casefold() not in taken else f"{base}-{rid}"
            name = f"{slug}.md"
            if name.casefold() in taken:
                continue
            taken.add(name.casefold())
            body = _body(conn, rid)
            header, fm = _render_native(slug, meta, body, title)
            raw = (header + body).encode("utf-8")
            output[name] = raw
            stamps.append((rid, title, name, header, fm, meta, raw))
        stamped_ids = {st[0] for st in stamps}
        report.unexported = [
            h for h in _unexported(conn, namespace) if int(h[2:]) not in stamped_ids
        ]
        # mkdir and exclusive file creation refuse races without trusting advisory file locks.
        try:
            dest.mkdir()
        except FileExistsError as exc:
            raise ImportRefused(
                f"export destination exists: {dest}; choose a new directory"
            ) from exc
        dest_fd = os.open(dest, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for name, raw in output.items():
                fd = os.open(
                    name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=dest_fd,
                )
                with os.fdopen(fd, "wb") as stream:
                    stream.write(raw)
        finally:
            os.close(dest_fd)
        # Stamp last, inside the transaction: a failed write rolls the stamps back.
        for rid, title, name, header, fm, meta, raw in stamps:
            mirror: dict[str, Any] = {
                "namespace": namespace,
                "filename": name,
                "header": header,
                "file_digest": _digest(raw.decode()),
                "unresolved": [],
            }
            stamped = {
                **meta,
                "hook": fm["description"],
                "frontmatter": fm,
                "order": 0,
                _KEY: mirror,
            }
            if title != fm["name"]:  # a mirror node's title is its file's name:
                store.chunks.set_ref_title(
                    rid, fm["name"], source="memory-mirror", conn=conn
                )
            mirror["graph_digest"] = _state(conn, rid, fm["name"], stamped, namespace)
            store.update_ref(rid, meta_patch=stamped, conn=conn)
            report.exported_native.append(f"me{rid}")
    report.created = len(output)
    return report
