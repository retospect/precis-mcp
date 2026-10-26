"""``precis gripe-fix`` — the gripe-fix loop's CLI mirror (gr519361).

    precis gripe-fix import DIR [--apply]        one-shot import of the file-era
                                                 coordinator state; dry run by default
    precis gripe-fix brief SLUG [--out FILE]     a batch's dossier — the brief in
                                                 force plus every member's body and
                                                 comments — for a builder without MCP
    precis gripe-fix assign SLUG --host H --session S [--tree T] [--dry-run]
                                                 record who builds the batch

Every write goes through :class:`precis.handlers.gripe.GripeHandler`
``edit(mode='fix')`` — the same door the MCP exposes — never SQL
(``docs/backlog/gripe-fix-loop-on-the-gripe.md``). Against prod run it as
``scripts/prod-precis gripe-fix …``; ``brief`` and any ``--dry-run`` count
as reads there.

Import mapping (the state directory the GRIPE_FIX coordinator kept):

- ``batches.txt`` rows ``name|ids|files|lane|notes`` → ``batch:<name>``
  on each id, brief = ``briefs/<name>.md`` or ``briefs/<name>-start.md``,
  else a synthesized brief marked ``[imported from batches.txt]``;
- ``briefs/<slug>.md`` with no row → batch ``<slug>`` (``-start``,
  ``resume-``, ``-r<N>``, ``-rulings<N>`` stripped), members = the gripe
  ids the text cites (reported as inferred);
- ``<slug>-fix.md`` / ``<slug>-r<N>-fix.md`` → a fix-round brief;
- ``handoff-<name>.md`` → handoff on the batch its head names
  (``worktree-<x>``), else ``<name>``;
- ``<slug>-review.md`` → review; verdict = the first ``LAND``/``FIX`` word;
- ``awaiting-deploy.txt`` ``sha: ids`` and ``close-*.txt`` ``id sha batch``
  → ``landed``.

Idempotent: a text already on the gripe is skipped, a batch or sha already
set is skipped, a closed or missing gripe is reported and left alone.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from precis.cli._common import resolve_dsn

#: A gripe id in prose: ``gr<N>`` always, a bare six-digit ``4xxxxx``/``5xxxxx``
#: (the era's id range) too — a bare small integer is a count, not an id.
_ID_RE = re.compile(r"\bgr(\d{1,9})\b|\b([45]\d{5})\b")
#: An explicit id column (batches.txt, awaiting-deploy, close lists): any integer.
_ID_COL_RE = re.compile(r"\b(?:gr)?(\d{1,9})\b")
_SHA_RE = re.compile(r"[0-9a-f]{7,40}")
_TREE_RE = re.compile(r"worktree-([A-Za-z0-9._-]+)")
_SLUG_OK = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_SKIP_DIRS = ("census", "triage", "triage-dry", "r4_scripts", "specs", "reports")
_SKIP_FILES = ("queue.txt", "queue-recovered.txt", "closes.tsv", "batches.txt")


def add_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register the ``gripe-fix`` subparser on ``sub``."""
    p = sub.add_parser(
        "gripe-fix",
        help="Gripe-fix loop: import the file-era state, render a batch brief, assign a builder.",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--database-url", default=None)
    ops = p.add_subparsers(dest="op", required=True)
    imp = ops.add_parser("import", help="import a coordinator state directory")
    imp.add_argument("dir")
    imp.add_argument("--apply", action="store_true", help="write (default: dry run)")
    imp.add_argument(
        "--dry-run", action="store_true", help="explicit no-write (the default)"
    )
    imp.add_argument("--verbose", action="store_true", help="one line per gripe")
    br = ops.add_parser("brief", help="render a batch's dossier")
    br.add_argument("slug")
    br.add_argument("--out", default=None, help="write to FILE instead of stdout")
    asg = ops.add_parser("assign", help="record host/session/tree on a batch")
    asg.add_argument("slug")
    asg.add_argument("--host", required=True)
    asg.add_argument("--session", required=True)
    asg.add_argument("--tree", default=None)
    asg.add_argument("--dry-run", action="store_true")
    return p


def run(args: argparse.Namespace) -> None:
    from precis.config import load_config
    from precis.store import Store

    store = Store.connect(resolve_dsn(args.database_url, cfg=load_config()))
    try:
        handler = _handler(store)
        if args.op == "import":
            plan = plan_import(Path(args.dir))
            print(plan.report(verbose=args.verbose))
            if args.apply and not args.dry_run:
                print(apply_plan(handler, plan))
            else:
                print(
                    "dry run — nothing written; --apply writes through edit(mode='fix')"
                )
        elif args.op == "brief":
            body = handler.get(id="/batch", batch=args.slug).body
            if args.out:
                Path(args.out).write_text(body + "\n", encoding="utf-8")
                print(f"wrote {args.out} ({len(body)} chars)")
            else:
                print(body)
        elif args.op == "assign":
            print(
                assign_batch(
                    handler,
                    args.slug,
                    host=args.host,
                    session=args.session,
                    tree=args.tree,
                    dry_run=args.dry_run,
                )
            )
    finally:
        store.close()


def _handler(store: Any) -> Any:
    from precis.dispatch import Hub
    from precis.handlers.gripe import GripeHandler

    return GripeHandler(hub=Hub(store=store))


def batch_members(handler: Any, slug: str) -> list[int]:
    """Live gripes tagged ``batch:<slug>``."""
    from precis.handlers._gripe_fix import BATCH_PREFIX
    from precis.handlers.gripe import _TERMINAL_STATUS_TAGS

    refs = handler.store.list_refs(
        kind="gripe",
        tags=[f"{BATCH_PREFIX}{slug}"],
        exclude_tags=sorted(_TERMINAL_STATUS_TAGS),
        limit=500,
    )
    return sorted(r.id for r in refs)


def assign_batch(
    handler: Any,
    slug: str,
    *,
    host: str,
    session: str,
    tree: str | None,
    dry_run: bool,
) -> str:
    ids = batch_members(handler, slug)
    if not ids:
        return f"no live gripe carries batch:{slug} — nothing assigned"
    patch: dict[str, Any] = {"host": host, "session": session}
    if tree:
        patch["tree"] = tree
    return handler.edit(id=list(ids), mode="fix", dry_run=dry_run, **patch).body


# ── the import plan ────────────────────────────────────────────────────


@dataclass
class Item:
    """One file → one patch on a set of gripes."""

    kind: str  # batch | brief | fix-brief | handoff | review | landed
    batch: str | None
    ids: tuple[int, ...]
    source: str
    text: str = ""
    verdict: str | None = None
    sha: str | None = None
    inferred: bool = False
    mtime: float = 0.0

    def patch(self) -> dict[str, Any]:
        p: dict[str, Any] = {}
        if self.kind == "batch":
            p["batch"] = self.batch
            if self.text:
                p["brief"] = self.text
        elif self.kind in ("brief", "fix-brief"):
            p["brief"] = self.text
            if self.batch:
                p["batch"] = self.batch
        elif self.kind == "handoff":
            p["handoff"] = self.text
        elif self.kind == "review":
            p["review"] = self.verdict
            findings = _after_verdict(self.text)
            if findings:
                p["findings"] = findings
        elif self.kind == "landed":
            p["landed"] = self.sha
            if self.batch:
                p["batch"] = self.batch
        return p


@dataclass
class Plan:
    root: Path
    items: list[Item] = field(default_factory=list)
    unmapped: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    members: dict[str, tuple[int, ...]] = field(default_factory=dict)

    @property
    def gripes(self) -> set[int]:
        return {i for it in self.items for i in it.ids}

    def report(self, *, verbose: bool = False) -> str:
        kinds = Counter(it.kind for it in self.items)
        lines = [
            f"gripe-fix import plan — {self.root}",
            f"{len(self.items)} items over {len(self.gripes)} gripes: "
            + ", ".join(f"{k} {n}" for k, n in sorted(kinds.items())),
            f"batches: {len(self.members)}"
            + (
                f" ({sum(1 for it in self.items if it.kind == 'batch' and it.inferred)} "
                "with members inferred from the brief text)"
            ),
        ]
        by_batch: dict[str, list[Item]] = {}
        for it in self.items:
            by_batch.setdefault(it.batch or "(no batch)", []).append(it)
        for name in sorted(by_batch):
            its = by_batch[name]
            ids = sorted({i for it in its for i in it.ids})
            parts = [f"{it.kind}<{it.source}>" for it in its]
            lines.append(f"  {name}: {len(ids)} gripes · " + " ".join(parts))
            if verbose:
                lines.append("    " + " ".join(f"gr{i}" for i in ids))
        if self.unmapped:
            lines += [
                "unmapped (no batch or no members):",
                *(f"  {u}" for u in self.unmapped),
            ]
        if self.skipped:
            lines += ["not imported:", *(f"  {s}" for s in self.skipped)]
        return "\n".join(lines)


def _slug(stem: str) -> str:
    s = stem.lower()
    s = re.sub(r"^(resume|handoff|current-brief)-", "", s)
    s = re.sub(r"-(start|resume|review|fix\d*)$", "", s)
    s = re.sub(r"-r\d+$", "", s)
    s = re.sub(r"-(rulings\d*|add-\d+|notes|split)$", "", s)
    return s


def _ids(text: str) -> tuple[int, ...]:
    """Gripe ids cited in prose (``gr<N>`` or a bare six-digit id)."""
    return tuple(sorted({int(a or b) for a, b in _ID_RE.findall(text)}))


def _id_col(text: str) -> tuple[int, ...]:
    """Gripe ids in an explicit id column — every integer counts."""
    return tuple(sorted({int(m) for m in _ID_COL_RE.findall(text)}))


def _verdict(text: str) -> str | None:
    for line in text.splitlines()[:6]:
        m = re.search(r"\b(LAND|FIX)\b", line)
        if m:
            return m.group(1).lower()
    return None


def _after_verdict(text: str) -> str:
    """The review text minus its leading verdict word — the findings."""
    return re.sub(r"^\W*(LAND|FIX)\b[\s—:-]*", "", text.strip(), count=1).strip()


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""


def plan_import(root: Path) -> Plan:
    plan = Plan(root=root)
    briefs_dir = root / "briefs"
    used_briefs: set[Path] = set()

    def brief_for(name: str) -> Path | None:
        for cand in (f"{name}.md", f"{name}-start.md", f"{name}-r2.md"):
            p = briefs_dir / cand
            if p.is_file():
                return p
        return None

    # 1. batches.txt rows
    batches = root / "batches.txt"
    if batches.is_file():
        for raw in _read(batches).splitlines():
            if not raw.strip() or raw.lstrip().startswith("#"):
                continue
            cols = [c.strip() for c in raw.split("|")]
            name = _slug(cols[0])
            if not _SLUG_OK.match(name):
                plan.unmapped.append(f"batches.txt row {cols[0]!r}: not a slug")
                continue
            ids = _id_col(cols[1]) if len(cols) > 1 else ()
            bpath = brief_for(name)
            text = _read(bpath) if bpath else ""
            inferred = False
            if not ids and text:
                ids = _ids(text)
                inferred = True
            if not ids:
                plan.unmapped.append(f"batches.txt row {name}: no gripe ids")
                continue
            if bpath:
                used_briefs.add(bpath)
                src = f"briefs/{bpath.name}"
            else:
                src = "batches.txt"
                text = (
                    f"[imported from batches.txt] batch {name}\n"
                    f"files: {cols[2] if len(cols) > 2 else '-'}\n"
                    f"lane: {cols[3] if len(cols) > 3 else '-'}\n"
                    f"notes: {cols[4] if len(cols) > 4 else '-'}"
                )
            plan.members[name] = ids
            plan.items.append(
                Item(
                    kind="batch",
                    batch=name,
                    ids=ids,
                    source=src,
                    text=text,
                    inferred=inferred,
                    mtime=(bpath or batches).stat().st_mtime,
                )
            )
    # 2. briefs with no row: members inferred from the text
    if briefs_dir.is_dir():
        # A batch's ``-start`` (or bare) brief is the batch item; its
        # ``-r2``/``-rulings``/``-add-`` siblings are later rounds.
        def _brief_order(path: Path) -> tuple[str, int, str]:
            later = bool(re.search(r"-(r\d+|rulings\d*|add-\d+|resume)$", path.stem))
            return (_slug(path.stem), 1 if later else 0, path.name)

        for p in sorted(briefs_dir.glob("*.md"), key=_brief_order):
            if p in used_briefs or p.name.startswith("_"):
                continue
            name = _slug(p.stem)
            if name in plan.members:
                # a resume/rulings file for a known batch: a later brief round
                plan.items.append(
                    Item(
                        kind="fix-brief",
                        batch=name,
                        ids=plan.members[name],
                        source=f"briefs/{p.name}",
                        text=_read(p),
                        mtime=p.stat().st_mtime,
                    )
                )
                continue
            text = _read(p)
            ids = _ids(text)
            if not _SLUG_OK.match(name) or not ids:
                plan.unmapped.append(f"briefs/{p.name}: no gripe ids cited")
                continue
            plan.members[name] = ids
            plan.items.append(
                Item(
                    kind="batch",
                    batch=name,
                    ids=ids,
                    source=f"briefs/{p.name}",
                    text=text,
                    inferred=True,
                    mtime=p.stat().st_mtime,
                )
            )
    # 3. top-level fix briefs, handoffs, reviews
    for p in sorted(root.glob("*.md")):
        stem = p.stem
        text = _read(p)
        if stem.startswith("handoff-"):
            kind = "handoff"
            head = "\n".join(text.splitlines()[:4])
            m = _TREE_RE.search(head)
            name = _slug(m.group(1).rstrip(".,:;)")) if m else _slug(stem)
            verdict = None
        elif stem.endswith("-review"):
            kind, name = "review", _slug(stem)
            verdict = _verdict(text)
            if verdict is None:
                plan.unmapped.append(f"{p.name}: no LAND/FIX verdict in its head")
                continue
        elif re.search(r"-fix\d*$", stem):
            kind, name, verdict = "fix-brief", _slug(stem), None
        else:
            plan.skipped.append(p.name)
            continue
        ids = plan.members.get(name, ())
        if not ids:
            cited = _ids(text) if kind == "fix-brief" else ()
            if cited:
                ids = cited
                plan.members[name] = ids
            else:
                plan.unmapped.append(f"{p.name}: batch {name!r} has no known members")
                continue
        plan.items.append(
            Item(
                kind=kind,
                batch=name,
                ids=ids,
                source=p.name,
                text=text,
                verdict=verdict,
                mtime=p.stat().st_mtime,
            )
        )
    # 4. landed shas
    aw = root / "awaiting-deploy.txt"
    if aw.is_file():
        for raw in _read(aw).splitlines():
            m = re.match(r"^([0-9a-f]{7,40})(?:/[0-9a-f]{7,40})*:\s*(.*)$", raw.strip())
            if not m:
                if raw.strip():
                    plan.skipped.append(f"awaiting-deploy.txt: {raw.strip()[:60]}")
                continue
            ids = _id_col(m.group(2))
            if ids:
                plan.items.append(
                    Item(
                        kind="landed",
                        batch=None,
                        ids=ids,
                        source="awaiting-deploy.txt",
                        sha=m.group(1),
                        mtime=aw.stat().st_mtime,
                    )
                )
    for p in sorted(root.glob("close*.txt")):
        for raw in _read(p).splitlines():
            m = re.match(
                r"^(\d{1,9})\s+([0-9a-f]{7,40})(?:\s+([A-Za-z0-9._-]+))?", raw.strip()
            )
            if not m:
                continue
            # The third token is a batch name only when it names one the
            # plan already knows — close lists also carry free prose there.
            third = _slug(m.group(3).rstrip(":")) if m.group(3) else ""
            plan.items.append(
                Item(
                    kind="landed",
                    batch=third if third in plan.members else None,
                    ids=(int(m.group(1)),),
                    source=p.name,
                    sha=m.group(2),
                    mtime=p.stat().st_mtime,
                )
            )
    # 5. everything else is reported, not imported
    for d in _SKIP_DIRS:
        if (root / d).is_dir():
            plan.skipped.append(f"{d}/ ({sum(1 for _ in (root / d).iterdir())} files)")
    for f in _SKIP_FILES:
        if (root / f).is_file() and f != "batches.txt":
            plan.skipped.append(f)
    for p in sorted(root.iterdir()):
        if p.is_file() and p.suffix in (".sh", ".py", ".sql", ".tsv", ".diff"):
            plan.skipped.append(p.name)
    plan.items.sort(key=lambda it: (it.mtime, it.source))
    return plan


def apply_plan(handler: Any, plan: Plan) -> str:
    """Write the plan through ``edit(mode='fix')``, one gripe at a time, in
    file-time order; skip what is already there. Returns the summary."""
    from precis.errors import PrecisError
    from precis.handlers import _gripe_fix as gf

    counts: Counter[str] = Counter()
    failed: list[str] = []
    store = handler.store
    for it in plan.items:
        patch = it.patch()
        for gid in it.ids:
            ref = store.get_ref(kind="gripe", id=gid)
            if ref is None:
                counts["missing"] += 1
                continue
            tags = store.tags_for(gid)
            state = gf.fix_state(store, ref, tags)
            if state.terminal:
                counts["closed"] += 1
                continue
            have = {e.text.strip() for e in state.events}
            todo = dict(patch)
            for key in ("brief", "handoff"):
                if key in todo and todo[key].strip() in have:
                    del todo[key]
            if "review" in todo:
                text = gf.review_text(todo["review"], todo.get("findings"))
                if text.strip() in have:
                    del todo["review"]
                    todo.pop("findings", None)
            if "batch" in todo and todo["batch"] == state.batch:
                del todo["batch"]
            if "landed" in todo and todo["landed"] == state.landed:
                del todo["landed"]
            if not todo:
                counts["already"] += 1
                continue
            try:
                handler.edit(id=gid, mode="fix", **todo)
                counts["written"] += 1
            except PrecisError as exc:
                failed.append(f"gr{gid} <{it.source}>: {exc}")
                counts["failed"] += 1
    lines = [
        "applied: "
        + ", ".join(f"{k} {n}" for k, n in sorted(counts.items()))
        + (f" · {len(plan.gripes)} gripes in plan")
    ]
    lines += failed[:40]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="precis gripe-fix")
    sub = p.add_subparsers(dest="cmd", required=True)
    add_parser(sub)
    run(p.parse_args(argv if argv is not None else sys.argv[1:]))
    return 0


__all__ = [
    "Item",
    "Plan",
    "add_parser",
    "apply_plan",
    "assign_batch",
    "batch_members",
    "plan_import",
    "run",
]
