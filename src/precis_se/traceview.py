"""Construction-trace reads — ``view='trace'`` and the ``frame:`` selector
(structure-workspaces slice 2t, docs/backlog/structure-workspaces.md
§Construction traces).

The stored form is one ``ws_trace`` chunk per workspace run
(:mod:`precis_geom.trace_schema` schema 1), hung on the run job's ref —
or on the design's own ref when a containerless import ran without a job
(:func:`precis_se.workspace_import.import_workspace_run`). Each bound
structure/cad ref's ``meta['generated']['trace'] = {'run_job', 'root'}``
names the run its entity ``maker`` ids index into, and
``meta['generated']['frame']`` is the manifest entry's own frame.

A :class:`Trace` is the parsed doc plus the indexes the asks need:
parent/children links by ``key``, subtree id sets (a collapsed sibling
frame contributes its whole ``id``..``id_end`` span), the LCA of a set of
creator frames, and the one-line renders the spec fixes (~20 tokens a
frame — user frames as ``main.py:30 bundle(R=4)``, core frames as
``junction(k=2)#7`` with ``#occ`` only past the first occurrence).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: Frame fields a run-vs-run diff compares — identity (key/parent/qual/
#: file/line/occ) is never a "change"; a rerun differs only here.
_DIFF_FIELDS = ("args", "made", "touched")


def _counts(m: dict[str, Any] | None) -> int:
    """Total entities one ``made``/``touched`` dict accounts for — ints
    plus the lengths of ordinal ``[lo, hi]`` ranges."""
    total = 0
    for v in (m or {}).values():
        if isinstance(v, int):
            total += v
        elif isinstance(v, list):
            for r in v:
                if isinstance(r, (list, tuple)) and len(r) >= 2:
                    total += int(r[1]) - int(r[0]) + 1
    return total


def _counts_text(counts: dict[str, Any] | None) -> str:
    """``made``/``touched`` as ``24 atoms 48 bonds`` — ints as counts,
    ordinal ranges as their covered length."""
    parts = []
    for entity, v in sorted((counts or {}).items()):
        n = _counts({entity: v})
        parts.append(f"{n} {entity}")
    return " ".join(parts)


def _args_text(args: dict[str, Any] | None) -> str:
    """``(R=4,P=6)`` — the frame's arg digest, compact; ``()`` when empty."""
    if not args:
        return ""
    items = []
    for k in sorted(args):
        v = args[k]
        s = v if isinstance(v, str) else repr(v)
        items.append(f"{k}={s}")
    text = ",".join(items)
    return f"({text[:60]}{'…' if len(text) > 60 else ''})"


def _id_span(fr: dict[str, Any]) -> range:
    """The run-local ids one emitted frame covers — ``id..id_end`` for a
    collapsed sibling fold, ``id`` alone otherwise."""
    end = fr.get("id_end")
    return range(int(fr["id"]), int(end) + 1 if end is not None else int(fr["id"]) + 1)


class Trace:
    """One parsed trace.json doc — frames plus the lookups views need."""

    def __init__(self, doc: dict[str, Any]) -> None:
        self.doc = doc
        self.run: dict[str, Any] = dict(doc.get("run") or {})
        #: The run's workspace slug — user frames render their python id
        #: ``ws-<slug>::<qual>`` (slice 2p).
        self.ws: str | None = str(self.run.get("workspace") or "") or None
        self.frames: list[dict[str, Any]] = list(doc.get("frames") or [])
        self.by_key: dict[str, dict[str, Any]] = {f["key"]: f for f in self.frames}
        #: Every id a frame covers (collapsed spans included) → the frame.
        self.by_id: dict[int, dict[str, Any]] = {}
        self.children: dict[str | None, list[dict[str, Any]]] = {}
        for f in self.frames:
            for i in _id_span(f):
                self.by_id.setdefault(i, f)
            self.children.setdefault(f.get("parent"), []).append(f)

    def frame(self, key: str) -> dict[str, Any] | None:
        return self.by_key.get(key)

    def maker_frame(self, maker: int | None) -> dict[str, Any] | None:
        """The frame an entity's ``maker`` stamp cites."""
        return self.by_id.get(int(maker)) if maker is not None else None

    def path(self, frame: dict[str, Any]) -> list[dict[str, Any]]:
        """Root → ``frame`` inclusive (root's synthetic node first)."""
        out: list[dict[str, Any]] = []
        cur: dict[str, Any] | None = frame
        while cur is not None:
            out.append(cur)
            parent = cur.get("parent")
            cur = self.by_key.get(parent) if parent is not None else None
        out.reverse()
        return out

    def subtree(self, key: str) -> list[dict[str, Any]]:
        """``key``'s frame plus every descendant, in doc order."""
        out: list[dict[str, Any]] = []
        stack = [key]
        while stack:
            k = stack.pop()
            f = self.by_key.get(k)
            if f is None:
                continue
            out.append(f)
            stack.extend(c["key"] for c in self.children.get(k, []))
        return out

    def subtree_ids(self, key: str) -> set[int]:
        """Every run-local id the subtree covers (collapsed spans too)."""
        ids: set[int] = set()
        for f in self.subtree(key):
            ids.update(_id_span(f))
        return ids

    def lca(self, maker_ids: list[int]) -> dict[str, Any] | None:
        """The deepest frame every maker's root path shares."""
        paths: list[list[str]] = []
        for m in maker_ids:
            fr = self.maker_frame(m)
            if fr is None:
                continue
            paths.append([f["key"] for f in self.path(fr)])
        if not paths:
            return None
        common = set(paths[0])
        for p in paths[1:]:
            common &= set(p)
        # Deepest = the common ancestor with the longest root path.
        best: dict[str, Any] | None = None
        best_depth = -1
        for f in paths[0]:
            if f in common:
                depth = len(self.path(self.by_key[f]))
                if depth > best_depth:
                    best_depth = depth
                    best = self.by_key[f]
        return best

    def modifiers(self, ordinal: int, *, entity: str = "atoms") -> list[dict[str, Any]]:
        """Frames whose ``touched[entity]`` ordinal ranges cover
        ``ordinal`` — the modifier frames (junction fuse, decorate,
        relax), in event (doc) order."""
        out = []
        for f in self.frames:
            touched = (f.get("touched") or {}).get(entity)
            if not isinstance(touched, list):
                continue
            for r in touched:
                if (
                    isinstance(r, (list, tuple))
                    and len(r) >= 2
                    and int(r[0]) <= ordinal <= int(r[1])
                ):
                    out.append(f)
                    break
        return out

    def touched_ordinals(self, key: str, *, entity: str = "atoms") -> set[int]:
        """The ordinals a subtree's ``touched[entity]`` ranges cover."""
        out: set[int] = set()
        for f in self.subtree(key):
            touched = (f.get("touched") or {}).get(entity)
            if isinstance(touched, list):
                for r in touched:
                    if isinstance(r, (list, tuple)) and len(r) >= 2:
                        out.update(range(int(r[0]), int(r[1]) + 1))
        return out


def line(fr: dict[str, Any], ws: str | None = None) -> str:
    """One frame as the spec renders it: user ``main.py:30 bundle(R=4)``;
    everything else ``junction(k=2)#7`` — ``#occ`` past the first
    occurrence of the same call site under one parent; a collapsed fold
    shows its ``occ..occ_end`` span. ``ws`` (the run's workspace slug)
    appends the user frame's python id `` · ws-<slug>::<qual>`` (slice
    2p — the id is one ``get(kind='python', id=…)`` paste)."""
    qual = str(fr.get("qual") or "?")
    args = _args_text(fr.get("args"))
    occ = int(fr.get("occ") or 0)
    occ_end = fr.get("occ_end")
    if fr.get("kind") == "user":
        file = str(fr.get("file") or "")
        lineno = fr.get("line") or 0
        base = f"{file}:{lineno} {qual}{args}"
        if ws:
            base += f" · ws-{ws}::{qual}"
    else:
        base = f"{qual}{args}"
        if occ_end is not None:
            base += f"#{occ}..{occ_end}"
        elif occ > 0:
            base += f"#{occ}"
    return base


def chain_line(
    path: list[dict[str, Any]], leaf: dict[str, Any], ws: str | None = None
) -> str:
    """Root→leaf joined `` → `` (root skipped), then the leaf's own
    ``· made …, touched …`` — the atom→trace ask's whole answer."""
    parts = [line(f, ws) for f in path if f.get("kind") != "root"]
    text = " → ".join(parts) if parts else "(root)"
    made = _counts_text(leaf.get("made"))
    touched = _counts_text(leaf.get("touched"))
    if made or touched:
        text += " ·"
        if made:
            text += f" made {made}"
        if touched:
            text += f"{',' if made else ' '}touched {touched}"
    return text


def nearest_user(trace: Trace, frame: dict[str, Any]) -> dict[str, Any] | None:
    """The nearest ``kind='user'`` frame at or above ``frame`` — the
    script line a finding's creator traces back to."""
    last = None
    for f in trace.path(frame):
        if f.get("kind") == "user":
            last = f
    return last


def creator_text(trace: Trace, frame: dict[str, Any]) -> str:
    """The findings-table ``frame`` cell: ``main.py:30 bundle →
    junction#7`` — the subject's creator leaf behind its nearest user
    frame (just the user frame when creator and user coincide)."""
    user = nearest_user(trace, frame)
    leaf = line(frame)
    if user is None:
        return leaf
    utext = f"{user.get('file')}:{user.get('line')} {user.get('qual')}"
    return utext if user is frame else f"{utext} → {leaf}"


def card(trace: Trace, key: str, *, samples: list[str] | None = None) -> str:
    """The ``{'frame': key}`` ask: the frame line, own+subtree counts,
    children one line each, up to three sample atom paths, and the
    ``frame:<key>`` selector — never an atom list."""
    fr = trace.frame(key)
    if fr is None:
        return f"frame {key} not in this trace"
    out = [line(fr, trace.ws)]
    made = _counts_text(fr.get("made")) or "—"
    sub_made = _counts(
        {e: v for f in trace.subtree(key) for e, v in (f.get("made") or {}).items()}
    )
    own = _counts(fr.get("made"))
    out.append(f"made {made}" + (f" · subtree {sub_made}" if sub_made != own else ""))
    touched = _counts_text(fr.get("touched"))
    if touched:
        out.append(f"touched {touched}")
    kids = trace.children.get(key, [])
    if kids:
        out.append(f"children: {len(kids)}")
        out.extend(
            f"  {line(c, trace.ws)} · made {_counts_text(c.get('made')) or '—'}"
            for c in kids[:8]
        )
        if len(kids) > 8:
            out.append(f"  … {len(kids) - 8} more")
    for s in (samples or [])[:3]:
        out.append(f"  e.g. {s}")
    out.append(f"selector: frame:{key}")
    return "\n".join(out)


@dataclass
class DiffRow:
    kind: str  # 'added' | 'removed' | 'changed' | 'unchanged'
    frame: dict[str, Any]
    depth: int
    note: str = ""


def diff(old: Trace, new: Trace) -> list[DiffRow]:
    """``{'against': run}`` — frames added/removed/changed by ``key``;
    a subtree unchanged end-to-end folds to one line at its root."""
    rows: list[DiffRow] = []

    def same(f: dict[str, Any]) -> bool:
        o = old.by_key.get(f["key"])
        return o is not None and all(o.get(k) == f.get(k) for k in _DIFF_FIELDS)

    def subtree_same(f: dict[str, Any]) -> bool:
        if not same(f):
            return False
        okids = {c["key"]: c for c in old.children.get(f["key"], [])}
        kids = new.children.get(f["key"], [])
        return set(okids) == {c["key"] for c in kids} and all(
            subtree_same(c) for c in kids
        )

    def walk(f: dict[str, Any], depth: int) -> None:
        if subtree_same(f):
            n = len(new.subtree(f["key"]))
            rows.append(DiffRow("unchanged", f, depth, f"{n} frames"))
            return
        o = old.by_key.get(f["key"])
        if o is None:
            rows.append(DiffRow("added", f, depth))
        elif not same(f):
            changed = [k for k in _DIFF_FIELDS if o.get(k) != f.get(k)]
            rows.append(DiffRow("changed", f, depth, ",".join(changed)))
        for c in new.children.get(f["key"], []):
            walk(c, depth + 1)
        # Old children missing in new are removals — named once each.
        new_keys = {c["key"] for c in new.children.get(f["key"], [])}
        for oc in old.children.get(f["key"], []):
            if oc["key"] not in new_keys:
                rows.append(DiffRow("removed", oc, depth + 1))

    for f in new.children.get(None, []):
        walk(f, 1)
    # Subtrees present only in old hang off an old root child the new doc
    # lacks — the walk names them already via removed rows.
    return rows


def render_diff(rows: list[DiffRow], ws: str | None = None) -> str:
    """The diff rows as lines: ``+``/``-``/``~``/``=`` with depth indent."""
    glyphs = {"added": "+", "removed": "-", "changed": "~", "unchanged": "="}
    out = []
    for r in rows:
        note = f" ({r.note})" if r.note else ""
        out.append(f"{glyphs[r.kind]} {'  ' * (r.depth - 1)}{line(r.frame, ws)}{note}")
    return "\n".join(out) if out else "(no frames)"
