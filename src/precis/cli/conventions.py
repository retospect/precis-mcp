"""``precis conventions import`` — repo conventions as ``SPACE:repo-dev`` memory.

Reads the packaged manifest ``precis/data/conventions/rules.yaml`` (or
``--manifest PATH``) and mints, in ONE transaction:

- one *rule memory* per manifest entry (tags ``SPACE:repo-dev``,
  ``convention``; ``meta.convention_rule`` = the entry id);
- one *enforcer memory* per DISTINCT enforcer path (tags ``SPACE:repo-dev``,
  ``enforcer``; ``meta.convention_enforcer`` = the path);
- one ``tests`` edge per (enforcer, rule) pair, direction **enforcer -> rule**:
  the ``tests`` relation means "source tests target" (``store/types.py``,
  migration 0142).

Idempotence keys: ``meta.convention_rule`` / ``meta.convention_enforcer``
among memory refs that carry ``meta.source='conventions-import'`` (a
hand-made memory with the same key is invisible to the importer and never
clobbered); edges by the unique ``(src, dst, relation)`` tuple. RETIRED
importer refs still block re-minting: a human retirement is respected, so the
key is reported ``skipped-retired`` and nothing is created, updated or linked
to or from it. Several LIVE refs sharing one key (legacy duplicate) are
reported ``duplicate`` and left untouched (no create/update/edges, never
retired). The transaction's first statement is a
``pg_advisory_xact_lock``, so overlapping runs serialise. Default mode creates missing refs and edges
only and never touches an existing ref. ``--sync`` also rewrites the title
and body of importer-owned refs whose text differs (body = DELETE + INSERT
via ``replace_body_chunk``, never an UPDATE) and removes importer ``tests``
edges (``meta.source='conventions-import'``) the manifest no longer has.
Refs are never retired or deleted: importer refs absent from the manifest are
listed as ``orphaned``. ``--dry-run`` computes the plan inside the transaction
and rolls it back, so the printed diff is exactly what a real run would do.

The manifest is validated before the first write (unique ids, required keys,
and, when the repo tree is present, that each non-``ruff:`` enforcer path
exists).
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from psycopg import Connection

from precis.cli._common import resolve_dsn
from precis.store import ChunkInsert, Store, Tag

SOURCE = "conventions-import"
SPACE_TAG = "SPACE:repo-dev"
_BODY_KIND = "memory_body"
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


def default_manifest() -> Path:
    return Path(__file__).resolve().parents[1] / "data" / "conventions" / "rules.yaml"


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


_Row = tuple[int, str, str, bool]  # (ref_id, title, body, retired)
_LOCK_KEY = "precis-conventions-import"


def _lock(conn: Connection) -> None:
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (_LOCK_KEY,))


def _refs(conn: Connection, key: str) -> dict[str, list[_Row]]:
    """Importer finding refs (live AND retired) carrying ``meta.<key>``:
    value -> rows ordered by ref_id."""
    rows = conn.execute(
        "SELECT r.ref_id, r.title, r.meta->>%s, r.retired_at IS NOT NULL, "
        "(SELECT c.text FROM chunks c WHERE c.ref_id=r.ref_id "
        " AND c.chunk_kind=%s ORDER BY c.ord LIMIT 1) "
        "FROM refs r WHERE r.kind='memory' "
        "AND r.meta->>'source'=%s AND r.meta ? %s ORDER BY r.ref_id",
        (key, _BODY_KIND, SOURCE, key),
    ).fetchall()
    out: dict[str, list[_Row]] = {}
    for rid, t, k, ret, b in rows:
        out.setdefault(str(k), []).append((int(rid), str(t), b or "", bool(ret)))
    return out


def import_conventions(
    store: Store,
    manifest: Path,
    *,
    dry_run: bool = False,
    sync: bool = False,
) -> ConventionsReport:
    """Mint/converge convention findings from ``manifest``; see module docstring."""
    entries = load_manifest(manifest)
    rules: dict[str, tuple[dict[str, Any], str]] = {
        e["id"]: (e, _rule_body(e)) for e in entries
    }
    enforcers: dict[str, list[str]] = {}
    pairs: set[tuple[str, str]] = set()
    for e in entries:
        for enf in e.get("enforcers") or []:
            whats = enforcers.setdefault(enf["path"], [])
            if enf["what"] not in whats:
                whats.append(enf["what"])
            pairs.add((enf["path"], e["id"]))
    report = ConventionsReport()
    try:
        with store.tx() as conn:
            _lock(conn)
            _converge(store, conn, rules, enforcers, pairs, sync, report)
            if dry_run:
                raise _DryRun
    except _DryRun:
        pass
    return report


def _converge(
    store: Store,
    conn: Connection,
    rules: dict[str, tuple[dict[str, Any], str]],
    enforcers: dict[str, list[str]],
    pairs: set[tuple[str, str]],
    sync: bool,
    report: ConventionsReport,
) -> None:
    skipped: set[int] = set()  # retired / duplicate refs left alone
    ids: dict[str, int] = {}  # "r:<rule id>" / "e:<path>" -> ref_id

    def ensure(
        key: str,
        value: str,
        found: dict[str, list[_Row]],
        title: str,
        body: str,
        meta: dict[str, Any],
        tags: list[str],
    ) -> None:
        idx = f"{'r' if key == 'convention_rule' else 'e'}:{value}"
        rows = found.get(value, [])
        live = [r for r in rows if not r[3]]
        if len(live) > 1:
            report.duplicates += 1
            skipped.update(r[0] for r in live)
            report.lines.append(
                f"duplicate {value}: " + ", ".join(f"me{r[0]}" for r in live)
            )
            return
        if not live and rows:
            report.skipped_retired += 1
            skipped.update(r[0] for r in rows)
            report.lines.append(f"skipped-retired me{rows[-1][0]} {value}")
            return
        if live:
            rid, old_title, old_body, _ = live[0]
            ids[idx] = rid
            if not sync or (old_title == title and old_body == body):
                report.unchanged += 1
                return
            if old_body != body:
                store.chunks.replace_body_chunk(
                    rid, body, chunk_kind=_BODY_KIND, source=SOURCE, conn=conn
                )
            if old_title != title:
                store.chunks.set_ref_title(rid, title, source=SOURCE, conn=conn)
            report.updated += 1
            report.lines.append(f"updated me{rid} {value}")
            return
        ref = store.insert_ref(
            kind="memory",
            slug=None,
            title=title,
            meta={"source": SOURCE, key: value, **meta},
            conn=conn,
        )
        store.chunks.insert_chunks(
            ref.id,
            [ChunkInsert(ord=0, text=body, meta={"chunk_kind": _BODY_KIND})],
            conn=conn,
        )
        for tag in tags:
            store.add_tag(
                ref.id,
                Tag.parse_strict(tag, kind="memory"),
                set_by="system",
                conn=conn,
            )
        ids[idx] = int(ref.id)
        report.created += 1
        report.lines.append(f"created me{ref.id} {value}")

    live_rules = _refs(conn, "convention_rule")
    live_enf = _refs(conn, "convention_enforcer")
    for rule_id, (entry, body) in rules.items():
        ensure(
            "convention_rule",
            rule_id,
            live_rules,
            entry["title"].strip(),
            body,
            {
                "convention_file": entry["file"],
                "convention_section": entry.get("section"),
            },
            [SPACE_TAG, "convention"],
        )
    for path, whats in enforcers.items():
        ensure(
            "convention_enforcer",
            path,
            live_enf,
            f"Enforcer: {path}",
            "\n".join(f"- {w}" for w in whats),
            {},
            [SPACE_TAG, "enforcer"],
        )
    report.orphaned = [
        *(
            f"me{r[0]} {k}"
            for k, v in live_rules.items()
            if k not in rules
            for r in v
            if not r[3]
        ),
        *(
            f"me{r[0]} {k}"
            for k, v in live_enf.items()
            if k not in enforcers
            for r in v
            if not r[3]
        ),
    ]

    wanted = {
        (ids[f"e:{p}"], ids[f"r:{r}"])
        for p, r in pairs
        if f"e:{p}" in ids and f"r:{r}" in ids
    }
    have = {
        (int(s), int(d)): meta
        for s, d, meta in conn.execute(
            "SELECT src_ref_id, dst_ref_id, meta FROM links "
            "WHERE relation='tests' AND src_chunk_id IS NULL "
            "AND dst_chunk_id IS NULL"
        ).fetchall()
    }
    for src, dst in sorted(wanted - have.keys()):
        store.add_link(
            src_ref_id=src,
            dst_ref_id=dst,
            relation="tests",
            meta={"source": SOURCE},
            conn=conn,
        )
        report.edges_added += 1
        report.lines.append(f"edge me{src} -tests-> me{dst}")
    if sync:
        for (src, dst), meta in sorted(have.items()):
            if (
                (meta or {}).get("source") == SOURCE
                and (src, dst) not in wanted
                and src not in skipped
                and dst not in skipped
            ):
                store.remove_link(
                    src_ref_id=src, dst_ref_id=dst, relation="tests", conn=conn
                )
                report.edges_removed += 1
                report.lines.append(f"edge-removed me{src} -tests-> me{dst}")


def add_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register the ``conventions`` subparser (``import``) on ``sub``."""
    cp = sub.add_parser(
        "conventions",
        help="Import repo convention rules and their enforcers as graph findings.",
        description=(
            "`import` mints one finding per rule in the conventions manifest, "
            "one per distinct enforcer, and enforcer -tests-> rule edges."
        ),
    )
    csub = cp.add_subparsers(dest="conventions_cmd", required=True)
    imp = csub.add_parser(
        "import", help="Import the conventions manifest (idempotent)."
    )
    imp.add_argument(
        "--manifest",
        default=None,
        help="Manifest path (default: the packaged data/conventions/rules.yaml).",
    )
    imp.add_argument(
        "--dry-run", action="store_true", help="Print the plan; write nothing."
    )
    imp.add_argument(
        "--sync",
        action="store_true",
        help=(
            "Also rewrite changed titles/bodies of importer refs and drop "
            "importer tests edges the manifest no longer has (never retires)."
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
        manifest = Path(args.manifest) if args.manifest else default_manifest()
        report = import_conventions(
            store, manifest, dry_run=args.dry_run, sync=args.sync
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
    "import_conventions",
    "load_manifest",
    "repo_root",
    "run",
]
