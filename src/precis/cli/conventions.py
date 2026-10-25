"""``precis conventions import`` — repo conventions and design trees as graph findings.

Two manifest lanes share one converge engine:

- **rules.yaml** (default): the packaged
  ``precis/data/conventions/rules.yaml`` (or ``--manifest PATH``) mints
  ``kind='memory'`` refs tagged ``SPACE:repo-dev`` — one *rule memory* per
  entry (``meta.convention_rule`` = the entry id) and one *enforcer
  memory* per DISTINCT enforcer path (``meta.convention_enforcer`` = the
  path), joined by ``tests`` edges, direction enforcer -> rule.
- **design-tree** (``--manifest design-tree`` or a directory of the
  ``src/precis/data/design-tree/`` manifests): the
  :mod:`precis.design_tree` forest mints ``kind='finding'`` refs tagged
  ``SPACE:design-conventions``, ``dt:<role>`` and ``domain:<d>``, keyed on
  ``meta.dtree.id`` — one ref per node, rule (``<node>#<rid>``), issue
  (``<node>#i<n>``), fix (``<node>#f<n>``), source (``src:<text>``) and
  enforcer (``enf:<path>``), joined by the SPEC §2.3 edge set:
  ``specialises`` (child -> parent, incl. ``also_under``/stub parents),
  ``qualifies`` (issue -> node), ``fixes`` (fix -> issue), ``part-of``
  (rule -> node), ``cites`` (node/rule -> source), ``tests``
  (enforcer -> rule) and ``related-to`` (``links``/``see``, with link meta
  ``{as: ...}`` for axis/failure-mode/principle/process/system targets).
  ``--space design-conventions --kind finding`` is required (and the
  default) on this lane; anything else is refused.

Idempotence keys: the importer's meta key (``convention_rule`` /
``convention_enforcer`` / ``dtree.id``) among refs that carry
``meta.source='conventions-import'`` (a hand-made ref with the same key
value is invisible to the importer and never clobbered); edges by the
unique ``(src, dst, relation)`` tuple. RETIRED importer refs still block
re-minting: a human retirement is respected, so the key is reported
``skipped-retired`` and nothing is created, updated or linked to or from
it. Several LIVE refs sharing one key (legacy duplicate) are reported
``duplicate`` and left untouched (no create/update/edges, never retired).
The transaction's first statement is a ``pg_advisory_xact_lock``, so
overlapping runs serialise. Default mode creates missing refs and edges
only and never touches an existing ref. ``--sync`` also rewrites the
title and body of importer-owned refs whose text differs (body = DELETE +
INSERT via ``replace_body_chunk``, never an UPDATE), converges the
importer-owned meta fragment (``meta.dtree`` is replaced wholesale, so a
role/domain/scale/coverage change propagates), converges the lane's
importer-managed tags (``dt:<role>``, ``domain:<d>``, ``SPACE:`` — adds
missing, removes stale ``set_by='system'`` ones, never an agent tag),
replaces importer edges whose link meta changed (``{as: ...}`` follows
the target's role), and removes importer edges
(``meta.source='conventions-import'``) the manifest no longer has.
Refs are never retired or deleted: importer refs absent from the manifest
are listed as ``orphaned``. ``--dry-run`` computes the plan inside the
transaction and rolls it back, so the printed diff is exactly what a real
run would do.

The manifest is validated before the first write (the rules.yaml lane:
unique ids, required keys, and, when the repo tree is present, that each
non-``ruff:`` enforcer path exists; the design-tree lane: the
:mod:`precis.design_tree` schema).
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from psycopg import Connection

from precis import design_tree
from precis.cli._common import resolve_dsn
from precis.store import ChunkInsert, Store, Tag

SOURCE = "conventions-import"
_CONVENTIONS_SPACE = "repo-dev"
_CONVENTIONS_KIND = "memory"
_DTREE_SPACE = "design-conventions"
_DTREE_KIND = "finding"
_BODY_KIND = {"memory": "memory_body", "finding": "finding_body"}
_REQUIRED = ("id", "file", "title", "body")


class ManifestError(ValueError):
    """The manifest is malformed; nothing was written."""


class _DryRun(Exception):
    """Raised inside the transaction to roll back a ``--dry-run``."""


@dataclass
class ConventionsReport:
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    edges_added: int = 0
    edges_removed: int = 0
    orphaned: list[str] = field(default_factory=list)
    skipped_retired: int = 0
    duplicates: int = 0
    lines: list[str] = field(default_factory=list)

    def render(self) -> str:
        head = (
            f"created={self.created} updated={self.updated} "
            f"unchanged={self.unchanged} edges_added={self.edges_added} "
            f"edges_removed={self.edges_removed} orphaned={len(self.orphaned)} "
            f"skipped_retired={self.skipped_retired} duplicates={self.duplicates}"
        )
        return "\n".join([*self.lines, *(f"orphaned {o}" for o in self.orphaned), head])


@dataclass(frozen=True)
class _Item:
    """One ref to converge. ``key_field`` names the meta key the
    idempotence index reads; ``meta`` already carries the key fragment."""

    key_field: str  # 'convention_rule' | 'convention_enforcer' | 'dtree'
    key: str
    title: str
    body: str
    meta: dict[str, Any]
    tags: list[str]


@dataclass(frozen=True)
class _Edge:
    src: tuple[str, str]  # (key_field, key)
    dst: tuple[str, str]
    relation: str
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _Lane:
    """What a manifest lane manages. ``key_fields`` are the meta-key
    indexes ``_refs`` queries *unconditionally* — even a group the
    manifest has no items for, so orphans are still reported and
    ``--sync`` still reaps their edges. ``relations`` bounds the ``have``
    edge fetch and the ``--sync`` reap. ``tag_prefixes`` (matched against
    the rendered tag string) bound which stale ``set_by='system'`` tags
    ``--sync`` may remove."""

    key_fields: tuple[str, ...]
    relations: frozenset[str]
    tag_prefixes: tuple[str, ...]


def default_manifest() -> Path:
    return Path(__file__).resolve().parents[1] / "data" / "conventions" / "rules.yaml"


def dtree_manifest_dir() -> Path:
    """The packaged ``src/precis/data/design-tree/`` (populated by S1)."""
    return Path(__file__).resolve().parents[1] / "data" / "design-tree"


def repo_root() -> Path | None:
    """Repo checkout root, or ``None`` when running from an installed package."""
    root = Path(__file__).resolve().parents[3]
    if (root / "docs" / "conventions").is_dir() and (root / "pyproject.toml").is_file():
        return root
    return None


def load_manifest(
    manifest: Path, *, root: Path | None = None, check_paths: bool = True
) -> list[dict[str, Any]]:
    """Parse + validate; path existence is checked against ``root`` (default:
    the detected repo root; skipped when none is detectable)."""
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ManifestError(f"{manifest}: top level must be a list of entries")
    if root is None and check_paths:
        root = repo_root()
    if not check_paths:
        root = None
    seen: set[str] = set()
    for i, entry in enumerate(data):
        if not isinstance(entry, dict):
            raise ManifestError(f"entry {i}: must be a mapping")
        for key in _REQUIRED:
            if not isinstance(entry.get(key), str) or not entry[key].strip():
                raise ManifestError(f"entry {i}: nonempty string {key!r} required")
        if entry["id"] in seen:
            raise ManifestError(f"duplicate id: {entry['id']}")
        seen.add(entry["id"])
        enforcers = entry.get("enforcers") or []
        if not isinstance(enforcers, list):
            raise ManifestError(f"{entry['id']}: enforcers must be a list")
        for enf in enforcers:
            if (
                not isinstance(enf, dict)
                or not isinstance(enf.get("path"), str)
                or not isinstance(enf.get("what"), str)
            ):
                raise ManifestError(f"{entry['id']}: enforcer needs path + what")
            path = enf["path"]
            if root is not None and not path.startswith("ruff:"):
                if not (root / path).exists():
                    raise ManifestError(f"{entry['id']}: enforcer path missing: {path}")
        if root is not None and not (root / entry["file"]).exists():
            raise ManifestError(f"{entry['id']}: file missing: {entry['file']}")
    return data


def _rule_body(entry: dict[str, Any]) -> str:
    where = entry["file"]
    if entry.get("section"):
        where += f" § {entry['section']}"
    return f"{entry['body'].strip()}\n\nSource: {where}"


def _rules_plan(manifest: Path) -> tuple[list[_Item], list[_Edge]]:
    """rules.yaml lane: one item per rule + per distinct enforcer path,
    ``tests`` edges enforcer -> rule (unchanged legacy behaviour)."""
    entries = load_manifest(manifest)
    items: list[_Item] = []
    edges: list[_Edge] = []
    enforcer_whats: dict[str, list[str]] = {}
    for e in entries:
        items.append(
            _Item(
                "convention_rule",
                e["id"],
                e["title"].strip(),
                _rule_body(e),
                {
                    "convention_rule": e["id"],
                    "convention_file": e["file"],
                    "convention_section": e.get("section"),
                },
                ["convention"],
            )
        )
        for enf in e.get("enforcers") or []:
            whats = enforcer_whats.setdefault(enf["path"], [])
            if enf["what"] not in whats:
                whats.append(enf["what"])
            edges.append(
                _Edge(
                    ("convention_enforcer", enf["path"]),
                    ("convention_rule", e["id"]),
                    "tests",
                )
            )
    for path, whats in enforcer_whats.items():
        items.append(
            _Item(
                "convention_enforcer",
                path,
                f"Enforcer: {path}",
                "\n".join(f"- {w}" for w in whats),
                {"convention_enforcer": path},
                ["enforcer"],
            )
        )
    return items, edges


def _dtree_plan(directory: Path) -> tuple[list[_Item], list[_Edge]]:
    """design-tree lane: validated forest -> finding items + the SPEC
    §2.3 edge set, all keyed on ``meta.dtree.id``."""
    try:
        tree = design_tree.load_dir(directory)
    except design_tree.DesignTreeError as exc:
        raise ManifestError(str(exc)) from exc
    items: list[_Item] = []
    for it in tree.items:
        node = tree.nodes.get(it.node_id)
        rec = node.record if node else {}
        dmeta: dict[str, Any] = {"id": it.key, "role": it.role}
        if node is not None:
            if node.domain is not None:
                dmeta["domain"] = node.domain
            if node.scale is not None:
                dmeta["scale"] = node.scale
        if rec.get("coverage") is not None:
            dmeta["coverage"] = rec["coverage"]
        tags = [f"dt:{it.role}"]
        if node is not None and node.domain:
            tags.append(f"domain:{node.domain}")
        body = (
            yaml.safe_dump(dict(it.fields), sort_keys=True, allow_unicode=True).strip()
            + f"\n\nSource: {it.key} ({node.manifest if node else directory.name})"
        )
        items.append(_Item("dtree", it.key, it.title, body, {"dtree": dmeta}, tags))
    edges = [
        _Edge(("dtree", e.src), ("dtree", e.dst), e.relation, dict(e.meta))
        for e in tree.edges
    ]
    return items, edges


_Row = tuple[int, str, dict[str, Any], str, bool]
# (ref_id, title, meta, body, retired)
_LOCK_KEY = "precis-conventions-import"

#: How the importer's idempotence key is read out of ``refs.meta`` per
#: key field. ``dtree`` keys live one level down (``meta.dtree.id``).
_KEY_SQL = {
    "convention_rule": ("r.meta->>'convention_rule'", "r.meta ? 'convention_rule'"),
    "convention_enforcer": (
        "r.meta->>'convention_enforcer'",
        "r.meta ? 'convention_enforcer'",
    ),
    "dtree": ("r.meta->'dtree'->>'id'", "r.meta->'dtree' ? 'id'"),
}


def _lock(conn: Connection) -> None:
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (_LOCK_KEY,))


def _refs(
    conn: Connection, key_field: str, kind: str, body_kind: str
) -> dict[str, list[_Row]]:
    """Importer refs (live AND retired) carrying the key field:
    value -> rows ordered by ref_id."""
    keyexpr, exists = _KEY_SQL[key_field]
    rows = conn.execute(
        f"SELECT r.ref_id, r.title, r.meta, {keyexpr}, "
        "r.retired_at IS NOT NULL, "
        "(SELECT c.text FROM chunks c WHERE c.ref_id=r.ref_id "
        " AND c.chunk_kind=%s ORDER BY c.ord LIMIT 1) "
        "FROM refs r WHERE r.kind=%s "
        f"AND r.meta->>'source'=%s AND {exists} ORDER BY r.ref_id",
        (body_kind, kind, SOURCE),
    ).fetchall()
    out: dict[str, list[_Row]] = {}
    for rid, t, meta, k, ret, b in rows:
        out.setdefault(str(k), []).append(
            (int(rid), str(t), dict(meta or {}), b or "", bool(ret))
        )
    return out


def import_conventions(
    store: Store,
    manifest: Path | str,
    *,
    dry_run: bool = False,
    sync: bool = False,
    space: str | None = None,
    kind: str | None = None,
) -> ConventionsReport:
    """Mint/converge convention or design-tree findings; see module docstring.

    ``manifest`` is a rules.yaml path, a design-tree directory, or the
    literal ``'design-tree'`` (the packaged ``data/design-tree/``). The
    lane fixes the ref kind and SPACE; ``space``/``kind`` args must match
    it when given.
    """
    named = isinstance(manifest, str) and manifest == "design-tree"
    if named:
        manifest = dtree_manifest_dir()
    path = Path(manifest)
    if not named and not path.exists():
        raise ManifestError(f"{path}: no such manifest file or directory")
    if path.is_dir() or named:
        if space not in (None, _DTREE_SPACE) or kind not in (None, _DTREE_KIND):
            raise ManifestError(
                f"a design-tree manifest mints kind='{_DTREE_KIND}' in "
                f"SPACE:{_DTREE_SPACE}; got --kind {kind} --space {space}"
            )
        kind, space = _DTREE_KIND, _DTREE_SPACE
        if not path.is_dir() or not list(path.glob("*.yaml")):
            raise ManifestError(
                f"{path}: no *.yaml manifests — the design-tree manifests land with S1"
            )
        items, edges = _dtree_plan(path)
        lane = _Lane(
            key_fields=("dtree",),
            relations=_EDGE_RELATIONS,
            tag_prefixes=("dt:", "domain:", "SPACE:"),
        )
    else:
        if space not in (None, _CONVENTIONS_SPACE) or kind not in (
            None,
            _CONVENTIONS_KIND,
        ):
            raise ManifestError(
                f"a rules.yaml manifest mints kind='{_CONVENTIONS_KIND}' in "
                f"SPACE:{_CONVENTIONS_SPACE}; got --kind {kind} --space {space}"
            )
        kind, space = _CONVENTIONS_KIND, _CONVENTIONS_SPACE
        items, edges = _rules_plan(path)
        lane = _Lane(
            key_fields=("convention_rule", "convention_enforcer"),
            relations=frozenset({"tests"}),
            tag_prefixes=("convention", "enforcer", "SPACE:"),
        )
    report = ConventionsReport()
    try:
        with store.tx() as conn:
            _lock(conn)
            _converge(store, conn, items, edges, kind, space, sync, report, lane)
            if dry_run:
                raise _DryRun
    except _DryRun:
        pass
    return report


def _converge(
    store: Store,
    conn: Connection,
    items: list[_Item],
    edges: list[_Edge],
    kind: str,
    space: str,
    sync: bool,
    report: ConventionsReport,
    lane: _Lane,
) -> None:
    body_kind = _BODY_KIND[kind]
    skipped: set[int] = set()  # retired / duplicate refs left alone
    ids: dict[tuple[str, str], int] = {}  # (key_field, key) -> ref_id
    space_tag = f"SPACE:{space}"

    def ensure(item: _Item, found: dict[str, list[_Row]]) -> None:
        idx = (item.key_field, item.key)
        rows = found.get(item.key, [])
        live = [r for r in rows if not r[4]]
        if len(live) > 1:
            report.duplicates += 1
            skipped.update(r[0] for r in live)
            report.lines.append(
                f"duplicate {item.key}: " + ", ".join(f"me{r[0]}" for r in live)
            )
            return
        if not live and rows:
            report.skipped_retired += 1
            skipped.update(r[0] for r in rows)
            report.lines.append(f"skipped-retired me{rows[-1][0]} {item.key}")
            return
        if live:
            rid, old_title, old_meta, old_body, _ = live[0]
            ids[idx] = rid
            changed = False
            if sync:
                if old_body != item.body:
                    store.chunks.replace_body_chunk(
                        rid,
                        item.body,
                        chunk_kind=body_kind,
                        source=SOURCE,
                        conn=conn,
                    )
                    changed = True
                if old_title != item.title:
                    store.chunks.set_ref_title(
                        rid, item.title, source=SOURCE, conn=conn
                    )
                    changed = True
                if _sync_meta(conn, rid, old_meta, item.meta, item, report):
                    changed = True
                if _sync_tags(store, conn, rid, item, kind, space_tag, lane, report):
                    changed = True
            if changed:
                report.updated += 1
                report.lines.append(f"updated me{rid} {item.key}")
            else:
                report.unchanged += 1
            return
        ref = store.insert_ref(
            kind=kind,
            slug=None,
            title=item.title,
            meta={"source": SOURCE, **item.meta},
            conn=conn,
        )
        store.chunks.insert_chunks(
            ref.id,
            [ChunkInsert(ord=0, text=item.body, meta={"chunk_kind": body_kind})],
            conn=conn,
        )
        for tag in [space_tag, *item.tags]:
            store.add_tag(
                ref.id,
                Tag.parse_strict(tag, kind=kind),
                set_by="system",
                conn=conn,
            )
        ids[idx] = int(ref.id)
        report.created += 1
        report.lines.append(f"created me{ref.id} {item.key}")

    # Converge per key field, in lane order — every declared field is
    # queried even when the manifest has no items for it, so its refs are
    # still reported orphaned and ``--sync`` still reaps their edges.
    wanted: dict[str, set[str]] = {}
    groups: list[tuple[str, dict[str, list[_Row]]]] = []
    by_field: dict[str, list[_Item]] = {}
    for item in items:
        by_field.setdefault(item.key_field, []).append(item)
    for key_field in lane.key_fields:
        found = _refs(conn, key_field, kind, body_kind)
        groups.append((key_field, found))
        wanted[key_field] = {i.key for i in by_field.get(key_field, [])}
        for item in by_field.get(key_field, []):
            ensure(item, found)
    report.orphaned = [
        f"me{r[0]} {k}"
        for key_field, found in groups
        for k, v in found.items()
        if k not in wanted[key_field]
        for r in v
        if not r[4]
    ]

    # Refs this lane owns (found importer rows + anything minted this run);
    # ``--sync`` reaps only edges with BOTH ends inside it, so one lane can
    # never reap the other's importer edges (they share meta.source).
    managed: set[int] = {
        r[0] for _, found in groups for rows in found.values() for r in rows
    } | set(ids.values())

    wanted_edges: dict[tuple[int, int, str], dict[str, Any]] = {}
    for e in edges:
        src = ids.get(e.src)
        dst = ids.get(e.dst)
        if src is not None and dst is not None:
            wanted_edges[(src, dst, e.relation)] = e.meta
    if not wanted_edges and not sync:
        return
    relations = sorted({rel for _, _, rel in wanted_edges} | lane.relations)
    have = {
        (int(s), int(d), str(rel)): meta
        for s, d, rel, meta in conn.execute(
            "SELECT src_ref_id, dst_ref_id, relation, meta FROM links "
            "WHERE relation = ANY(%s) AND src_ref_id = ANY(%s::int[]) "
            "AND src_chunk_id IS NULL AND dst_chunk_id IS NULL",
            (relations, sorted(managed)),
        ).fetchall()
    }
    for (src, dst, rel), meta in sorted(wanted_edges.items()):
        if (src, dst, rel) in have:
            continue
        store.add_link(
            src_ref_id=src,
            dst_ref_id=dst,
            relation=rel,
            meta={"source": SOURCE, **meta},
            conn=conn,
        )
        report.edges_added += 1
        report.lines.append(f"edge me{src} -{rel}-> me{dst}")
    if sync:
        for (src, dst, rel), meta in sorted(have.items()):
            if (
                (meta or {}).get("source") != SOURCE
                or src not in managed
                or dst not in managed
                or src in skipped
                or dst in skipped
            ):
                continue
            want_meta = wanted_edges.get((src, dst, rel))
            if want_meta is None:
                store.remove_link(
                    src_ref_id=src, dst_ref_id=dst, relation=rel, conn=conn
                )
                report.edges_removed += 1
                report.lines.append(f"edge-removed me{src} -{rel}-> me{dst}")
            elif {k: v for k, v in (meta or {}).items() if k != "source"} != (
                want_meta
            ):
                # same triple, changed link meta ({as: ...}) — replace it
                store.remove_link(
                    src_ref_id=src, dst_ref_id=dst, relation=rel, conn=conn
                )
                store.add_link(
                    src_ref_id=src,
                    dst_ref_id=dst,
                    relation=rel,
                    meta={"source": SOURCE, **want_meta},
                    conn=conn,
                )
                report.edges_removed += 1
                report.edges_added += 1
                report.lines.append(f"edge-meta me{src} -{rel}-> me{dst}")


def _sync_meta(
    conn: Connection,
    rid: int,
    old_meta: dict[str, Any],
    fragment: dict[str, Any],
    item: _Item,
    report: ConventionsReport,
) -> bool:
    """Converge the importer-owned meta fragment (``meta.dtree`` for the
    design-tree lane — a nested dict, so ``||`` replaces it wholesale and
    stale inner keys drop). Only keys the importer mints are compared."""
    patch = {k: v for k, v in fragment.items() if old_meta.get(k) != v}
    if not patch:
        return False
    conn.execute(
        "UPDATE refs SET meta = meta || %s::jsonb, updated_at = now() "
        "WHERE ref_id = %s",
        (json.dumps(patch), rid),
    )
    report.lines.append(f"meta me{rid} {item.key}: {', '.join(sorted(patch))}")
    return True


def _sync_tags(
    store: Store,
    conn: Connection,
    rid: int,
    item: _Item,
    kind: str,
    space_tag: str,
    lane: _Lane,
    report: ConventionsReport,
) -> bool:
    """Converge the importer-managed tags (``dt:<role>``, ``domain:<d>``,
    ``SPACE:``): add missing, remove stale — but only ``set_by='system'``
    tags inside the lane's prefixes, never an agent/human tag."""
    desired = {space_tag, *item.tags}
    have: dict[str, str] = {}  # rendered tag -> set_by
    for ns, val, set_by in conn.execute(
        "SELECT t.namespace, t.value, rt.set_by FROM ref_tags rt "
        "JOIN tags t ON t.tag_id = rt.tag_id WHERE rt.ref_id = %s",
        (rid,),
    ).fetchall():
        have[val if ns in ("OPEN", "FLAG") else f"{ns}:{val}"] = str(set_by)
    changed = False
    for tag_s in sorted(set(have) - desired):
        if have[tag_s] == "system" and tag_s.startswith(lane.tag_prefixes):
            store.remove_tag(rid, Tag.parse_strict(tag_s, kind=kind), conn=conn)
            changed = True
            report.lines.append(f"tag-removed me{rid} {tag_s}")
    for tag_s in sorted(desired - set(have)):
        store.add_tag(
            rid,
            Tag.parse_strict(tag_s, kind=kind),
            set_by="system",
            conn=conn,
        )
        changed = True
        report.lines.append(f"tag-added me{rid} {tag_s}")
    return changed


#: Relations the importer manages; the ``have`` fetch is scoped to these
#: so foreign edges can never be reaped by ``--sync``.
_EDGE_RELATIONS = frozenset(
    {"tests", "specialises", "qualifies", "fixes", "part-of", "cites", "related-to"}
)


def add_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register the ``conventions`` subparser (``import``) on ``sub``."""
    cp = sub.add_parser(
        "conventions",
        help="Import repo conventions or design-tree manifests as graph findings.",
        description=(
            "`import` mints one finding per rule in the conventions manifest, "
            "one per distinct enforcer, and enforcer -tests-> rule edges; "
            "with --manifest design-tree it mints the design graph as "
            "SPACE:design-conventions findings keyed on meta.dtree.id."
        ),
    )
    csub = cp.add_subparsers(dest="conventions_cmd", required=True)
    imp = csub.add_parser("import", help="Import a conventions manifest (idempotent).")
    imp.add_argument(
        "--manifest",
        default=None,
        help=(
            "Manifest path, a design-tree manifest directory, or "
            "'design-tree' for the packaged data/design-tree/ (default: "
            "the packaged data/conventions/rules.yaml)."
        ),
    )
    imp.add_argument(
        "--space",
        default=None,
        help=(
            "SPACE tag value for minted refs (default: repo-dev for "
            "rules.yaml, design-conventions for a design-tree manifest; "
            "a mismatch with the manifest lane is refused)."
        ),
    )
    imp.add_argument(
        "--kind",
        default=None,
        help=(
            "Ref kind minted (default: memory for rules.yaml, finding "
            "for a design-tree manifest; a mismatch is refused)."
        ),
    )
    imp.add_argument(
        "--dry-run", action="store_true", help="Print the plan; write nothing."
    )
    imp.add_argument(
        "--sync",
        action="store_true",
        help=(
            "Also rewrite changed titles/bodies of importer refs, converge "
            "their meta/tags/link meta, and drop importer edges the "
            "manifest no longer has (never retires)."
        ),
    )
    imp.add_argument("--database-url", default=None, help="Postgres DSN override.")
    return cp


def run(args: argparse.Namespace) -> None:
    """Implements ``precis conventions import``."""
    from precis.config import load_config

    cfg = load_config()
    store = Store.connect(resolve_dsn(args.database_url, cfg=cfg))
    try:
        manifest = args.manifest if args.manifest else default_manifest()
        report = import_conventions(
            store,
            manifest,
            dry_run=args.dry_run,
            sync=args.sync,
            space=args.space,
            kind=args.kind,
        )
        print(report.render())
    except ManifestError as exc:
        raise SystemExit(f"precis conventions import: {exc}") from exc
    finally:
        store.close()


__all__ = [
    "ConventionsReport",
    "ManifestError",
    "add_parser",
    "default_manifest",
    "dtree_manifest_dir",
    "import_conventions",
    "load_manifest",
    "repo_root",
    "run",
]
