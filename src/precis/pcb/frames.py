"""``view='frames'`` — a board's construction-trace asks (gr521596).

A workspace run's ``ws_trace`` chunk parses through
:mod:`precis_geom.trace_schema` into the same frame doc structures use;
the board's ``meta['generated']['trace'] = {'run_job', 'root'}`` names
the run its entity ``meta.frame`` stamps index into (``run_job=None``
reads the board's own chunks — a containerless import). Entity bindings
live on the rows: ``pcb_instances.meta.frame`` (component ``meta.frame``
as the spec's fallback), ``pcb_nets.meta.frame``.

``precis`` cannot import ``precis_se`` (layering — ``precis_se`` sits on
``precis``), so the frame walk below is a ported subset of
``precis_se.traceview`` (``Trace`` minus lca/modifiers/diff, ``line``,
``chain_line``, ``card``) and the ws_trace read speaks plain SQL over
``chunks``, the same table ``precis_se.persist._read_ws_trace`` walks —
the ``_python_ws.py`` precedent. Deferred to later slices: set→LCA,
run-vs-run ``against``, modifier/service frames and the findings
``frame`` column.
"""

from __future__ import annotations

from typing import Any

from precis.errors import BadInput
from precis_geom import trace_schema

#: The ws_trace read — ``precis_se.persist._read_ws_trace``'s own query.
_TRACE_SQL = (
    "SELECT text FROM chunks WHERE ref_id = %s "
    "AND chunk_kind = 'ws_trace' ORDER BY ord DESC"
)

#: The next free chunk ord on a ref — ws_trace stores append like
#: ``import_workspace_run``'s manifest/trace pair.
_ORD_SQL = "SELECT COALESCE(MAX(ord), -1) + 1 FROM chunks WHERE ref_id = %s"

#: The newest ws_trace chunk on a ref — a re-put of the same board
#: carries an identical trace doc, and the append is skipped when the
#: latest chunk already holds it (dumps is canonical).
_NEWEST_TRACE_SQL = _TRACE_SQL + " LIMIT 1"

#: ``args['trace']`` payload ceiling in UTF-8 bytes — far above a real
#: run's trace.json; a bigger payload is a misdirected blob, so vetting
#: refuses it before parse rather than storing a multi-MB chunk.
_MAX_TRACE_BYTES = 2_000_000

#: Per-field cap on a frame's free text (``src``/``file``/``qual`` and any
#: other string value) — longer values clip ellipsis-marked so one
#: runaway line can't bloat the stored doc.
_MAX_FIELD_CHARS = 500


def vet_trace(raw: Any) -> tuple[dict[str, Any], str | None]:
    """A ``put`` ``args['trace']`` — the run's ``trace.json`` as text or
    an already-parsed dict → ``(doc, root frame key)``. The doc parses
    through the shared schema (a refusal names the offending key), the
    payload is refused past ``_MAX_TRACE_BYTES``, and frames are capped
    at ``MAX_FRAMES`` with each string field clipped to
    ``_MAX_FIELD_CHARS`` — the same bounds the se importer's
    ``_load_trace`` applies (gr521596)."""
    if isinstance(raw, dict):
        text = trace_schema.dumps(raw)
    elif isinstance(raw, str):
        text = raw
    else:
        raise BadInput(
            f"args['trace'] must be the run's trace.json (text or object), "
            f"got {type(raw).__name__}",
            next="pass out/trace.json verbatim from the run",
        )
    size = len(text.encode("utf-8"))
    if size > _MAX_TRACE_BYTES:
        raise BadInput(
            f"args['trace'] is {size} bytes — over the {_MAX_TRACE_BYTES}-byte cap",
            next="trim the run's trace.json or omit args['trace']",
        )
    try:
        doc = trace_schema.loads(text)
    except ValueError as exc:
        raise BadInput(
            f"args['trace']: {exc}",
            next="pass the run's out/trace.json verbatim — it must match "
            f"schema {sorted(trace_schema.SCHEMA_VERSIONS)} "
            "(precis_geom.trace_schema)",
        ) from exc
    frames = list(doc.get("frames") or [])
    if len(frames) > trace_schema.MAX_FRAMES:
        doc = {
            **doc,
            "frames": frames[: trace_schema.MAX_FRAMES],
            "run": {**(doc.get("run") or {}), "truncated": True},
        }
    for fr in doc["frames"]:
        for name, value in fr.items():
            if isinstance(value, str) and len(value) > _MAX_FIELD_CHARS:
                fr[name] = value[: _MAX_FIELD_CHARS - 1] + "…"
    root = next((str(f["key"]) for f in doc["frames"] if f.get("parent") is None), None)
    return doc, root


def bind_trace_meta(
    meta: dict[str, Any] | None,
    *,
    run_job: int | None,
    root: str | None,
    prior: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The batch-``put`` ``meta`` with the run's trace bound — the
    ``meta.generated.trace = {'run_job', 'root'}`` record structure/cad
    refs carry (:func:`precis_se.workspace_import._generated_ctx`).
    ``prior`` is the board's existing ``meta['generated']``: the merge
    is deep for that one key so an earlier run's fields survive."""
    gen = dict(prior or {})
    gen.update(dict((meta or {}).get("generated") or {}))
    gen["trace"] = {"run_job": run_job, "root": root}
    return {**(meta or {}), "generated": gen}


def pending_trace_chunk(
    conn: Any,
    ref_id: int,
    doc: dict[str, Any],
    *,
    run_job: int | None,
) -> tuple[int, int, str] | None:
    """The producer half of ``view='frames'`` (gr521596): the run's trace
    doc as a ``ws_trace`` chunk to store, as ``(home, ord, text)`` — on
    ``run_job`` when the run had a job ref, else on the board itself (the
    containerless path — the same home rule ``import_workspace_run``
    uses). The caller inserts it (this module ships in the store-free
    geom wheel); the board's ``meta.generated.trace`` stamp rides the
    put's own ``meta`` merge (:func:`bind_trace_meta`). A re-put whose
    trace serializes to the newest existing chunk's text returns ``None``
    — the same board must not grow chunks."""
    home = int(run_job) if run_job is not None else int(ref_id)
    text = trace_schema.dumps(doc)
    latest = conn.execute(_NEWEST_TRACE_SQL, (home,)).fetchone()
    if latest is not None and latest[0] == text:
        return None
    row = conn.execute(_ORD_SQL, (home,)).fetchone()
    assert row is not None
    return home, int(row[0]), text


def read_ws_trace(store: Any, home: int, root: Any) -> dict[str, Any] | None:
    """Newest ``ws_trace`` chunk on ``home`` whose root frame's key is
    ``root`` (``root=None`` → the latest chunk); a chunk that fails the
    schema parse is skipped, never raised — the same lenient posture as
    ``precis_se.persist._read_ws_trace``."""
    with store.pool.connection() as conn:
        rows = conn.execute(_TRACE_SQL, (home,)).fetchall()
    for (text,) in rows:
        try:
            doc = trace_schema.loads(text)
        except Exception:
            continue
        if root is None or any(
            f.get("parent") is None and f.get("key") == root
            for f in doc.get("frames") or []
        ):
            return doc
    return None


def bound_frames(
    store: Any, ref_id: int
) -> tuple[dict[str, int | None], dict[str, int | None]]:
    """``({refdes: frame}, {net: frame})`` — every instance/net row and
    the creator frame id its ``meta.frame`` cites (``None`` when the row
    carries no binding). The instance's stamp wins over its component's
    (the spec stores both)."""
    parts: dict[str, int | None] = {}
    nets: dict[str, int | None] = {}
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT i.refdes, COALESCE(i.meta->>'frame', c.meta->>'frame')"
            " FROM pcb_instances i"
            " JOIN pcb_components c ON c.component_id = i.component_id"
            " WHERE i.ref_id = %s",
            (ref_id,),
        ).fetchall()
        for refdes, frame in rows:
            parts[str(refdes)] = _frame_id(frame)
        rows = conn.execute(
            "SELECT name, meta->>'frame' FROM pcb_nets WHERE ref_id = %s",
            (ref_id,),
        ).fetchall()
        for name, frame in rows:
            nets[str(name)] = _frame_id(frame)
    return parts, nets


def _frame_id(raw: Any) -> int | None:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


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
    """``made``/``touched`` as ``3 parts 2 nets``."""
    return " ".join(
        f"{_counts({entity: v})} {entity}"
        for entity, v in sorted((counts or {}).items())
    )


def _args_text(args: dict[str, Any] | None) -> str:
    """``(R=4,P=6)`` — the frame's arg digest, compact."""
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
    """One parsed trace.json doc — frames plus the lookups the asks need."""

    def __init__(self, doc: dict[str, Any]) -> None:
        self.doc = doc
        self.run: dict[str, Any] = dict(doc.get("run") or {})
        self.ws: str | None = str(self.run.get("workspace") or "") or None
        self.frames: list[dict[str, Any]] = list(doc.get("frames") or [])
        self.by_key: dict[str, dict[str, Any]] = {f["key"]: f for f in self.frames}
        self.by_id: dict[int, dict[str, Any]] = {}
        self.children: dict[str | None, list[dict[str, Any]]] = {}
        for f in self.frames:
            for i in _id_span(f):
                self.by_id.setdefault(i, f)
            self.children.setdefault(f.get("parent"), []).append(f)

    def frame(self, key: str) -> dict[str, Any] | None:
        return self.by_key.get(key)

    def maker_frame(self, maker: int | None) -> dict[str, Any] | None:
        """The frame an entity's ``meta.frame`` stamp cites."""
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


def line(fr: dict[str, Any], ws: str | None = None) -> str:
    """One frame as the spec renders it: user ``main.py:30 bundle(R=4)``;
    everything else ``junction(k=2)#7`` — ``#occ`` past the first
    occurrence, a collapsed fold shows its ``occ..occ_end`` span."""
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
    ``· made …, touched …`` — the entity→maker ask's whole answer."""
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


def card(trace: Trace, key: str, *, samples: list[str] | None = None) -> str:
    """The ``{'frame': key}`` ask: the frame line, own+subtree counts,
    children one line each, the frame's probe records (S1c) one line
    each, up to three bound entities citing the subtree, and the
    ``frame:<key>`` selector."""
    fr = trace.frame(key)
    if fr is None:
        return f"frame {key} not in this trace"
    out = [line(fr, trace.ws)]
    made = _counts_text(fr.get("made")) or "—"
    sub_made = sum(_counts(f.get("made")) for f in trace.subtree(key))
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
    probes = [p for p in (fr.get("probes") or ()) if isinstance(p, dict)]
    for p in probes[:5]:
        out.append(
            f"  probe {p.get('label', '?')}: resolves to "
            f"{p.get('count', '?')} {p.get('kind', '')}".rstrip()
        )
    if len(probes) > 5:
        out.append(f"  … {len(probes) - 5} more probes")
    for s in (samples or [])[:3]:
        out.append(f"  e.g. {s}")
    out.append(f"selector: frame:{key}")
    return "\n".join(out)
