"""Construction-trace capture — ``@traced`` on every public primitive.

One ``Tracer`` is live per run (context-local — activate with
:func:`start` / :func:`run`; :mod:`precis_ws.out` activates it in the
sandbox). Each ``@traced`` call:

1. walks ``sys._getframe()`` up the live Python stack and mints a *user*
   frame for every ``/work/**`` function frame between the deepest live
   traced frame and this call — library internals never appear (they
   collapse into the public frame that called them), and
   ``sys.setprofile`` is deliberately not used (it instruments every call,
   2–5× on agent loops, and sees internals it would have to filter out);
2. derives ``occ`` — the 0-based occurrence counter of ``qual``
   under the current parent, across all call sites — and the stable
   ``key`` (``precis_geom.trace_schema.frame_key``, which hashes
   structure only: an inserted line above the call keeps the key);
3. records ``args`` as a bounded digest (scalars verbatim, ``%.6g``
   floats, ``<Class sha8>`` for objects);
4. on exit pops itself (a call that raised first marks its frame
   ``error`` = the exception type name — the innermost traced frame it
   left); the frame stays in the tree under its parent.

``record(made=…, touched=…)`` inside a traced function counts the
entities it placed/changed on *its own* frame; ``leaf_id()`` is the stamp
entities carry (``Instance.maker`` …). ``with trace("name"):`` mints a
named user frame without a function boundary.

``Tracer.dump()`` emits the schema-2 document (``trace_schema.dumps``),
applying the sibling collapse (past ``COLLAPSE_SIBLINGS`` same-``qual``
childless siblings fold into one frame with an ``occ`` range) and the
``MAX_FRAMES`` cap (``run.truncated``). ``line_in_def``/``src`` carry the
call site's position relative to its enclosing def and its source text —
re-anchor fields, never part of the key.
"""

from __future__ import annotations

import contextlib
import dataclasses
import functools
import inspect
import json
import os
import re
import sys
import types
from collections.abc import Callable, Iterator
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, TypeVar

from precis_geom import trace_schema as ts

_F = TypeVar("_F", bound=Callable[..., Any])

#: Staged sandbox infrastructure that lives under ``/work`` but is not
#: user code — calls through it never mint user frames.
_INFRA_FILES = frozenset({"precis_client.py"})

_ACTIVE: ContextVar[Tracer | None] = ContextVar("precis_geom_trace", default=None)

_ADDR_RE = re.compile(r"0x[0-9a-fA-F]+")


def _utcnow() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


@dataclass(slots=True)
class _Frame:
    """One node of the trace tree; ``py`` is the live Python frame it was
    minted from — held so its id cannot be recycled while the entry sits
    on the active stack (id reuse would alias a stale stack entry)."""

    id: int
    key: str
    parent_key: str | None
    qual: str
    file: str
    line: int
    line_in_def: int
    src: str | None
    occ: int
    kind: str
    args: dict[str, Any]
    made: dict[str, int]
    touched: dict[str, Any]
    py: types.FrameType | None
    children: list[_Frame] = field(default_factory=list)
    #: ``occ`` counts per ``qual`` across all call sites (schema 2).
    _occ_next: dict[str, int] = field(default_factory=dict)
    #: Exception type name when the call raised — set on the innermost
    #: traced frame the exception left; ``dump`` omits it when ``None``.
    error: str | None = None


class Tracer:
    """The live trace of one run. ``stack`` is the active path (root →
    innermost live frame); the tree hangs off ``root.children``."""

    def __init__(self, work_root: str | os.PathLike[str] | None = None) -> None:
        self.work_root = os.path.normpath(
            str(work_root or os.environ.get("PRECIS_WS_WORK_DIR") or "/work")
        )
        self.root = _Frame(
            id=0,
            key=ts.sha8("precis-trace-root"),
            parent_key=None,
            qual="",
            file="",
            line=0,
            line_in_def=0,
            src=None,
            occ=0,
            kind="root",
            args={},
            made={},
            touched={},
            py=None,
        )
        self.stack: list[_Frame] = [self.root]
        self._next_id = 1
        self.started_utc = _utcnow()
        self.run_meta: dict[str, Any] = {}
        #: Exceptions that already marked a deeper frame. The objects
        #: themselves (exceptions take no weakref) — a freed ``id()``
        #: would be recycled and silently un-mark a retry loop's next
        #: failure.
        self._raised: set[BaseException] = set()
        #: filename → ``((st_mtime_ns, st_size), lines)`` for ``src`` —
        #: a rewrite between calls re-reads (``linecache``'s coarser
        #: staleness check misses a same-size edit).
        self._src_cache: dict[str, tuple[tuple[int, int], list[str]]] = {}

    # -- frame minting -----------------------------------------------------

    def _file_of(self, f: types.FrameType) -> str:
        """The frame's file — ``work_root``-relative for user code, else
        the caller's module name (library internals: kept for the key,
        never rendered)."""
        name = f.f_code.co_filename
        rel = os.path.relpath(name, self.work_root)
        if not rel.startswith("..") and not os.path.isabs(rel):
            return rel.replace(os.sep, "/")
        mod = f.f_globals.get("__name__")
        return str(mod) if mod else os.path.basename(name)

    def _in_work(self, f: types.FrameType) -> bool:
        """The frame's file lives under ``work_root`` and is not staged
        infra — unlike ``_is_user_frame`` a ``<module>`` top level counts
        (a script's own toplevel call site is a legal citation)."""
        if os.path.basename(f.f_code.co_filename) in _INFRA_FILES:
            return False
        rel = os.path.relpath(f.f_code.co_filename, self.work_root)
        return not rel.startswith("..") and not os.path.isabs(rel)

    def _is_user_frame(self, f: types.FrameType) -> bool:
        return f.f_code.co_name != "<module>" and self._in_work(f)

    def _site(self, f: types.FrameType) -> tuple[int, str | None]:
        """``(line_in_def, src)`` for a call-site frame inside
        ``work_root``: the line's offset from its enclosing code object's
        ``co_firstlineno`` and the stripped source line (120-char cap).
        ``(0, None)`` for sites outside ``work_root`` — driver, harness
        and library internals carry no re-anchor fields."""
        if not self._in_work(f):
            return 0, None
        src = self._line_at(f.f_code.co_filename, f.f_lineno).strip()
        return f.f_lineno - f.f_code.co_firstlineno, (src[:120] or None)

    def _line_at(self, filename: str, lineno: int) -> str:
        """The text of ``filename:lineno`` — contents cached per
        ``(st_mtime_ns, st_size)``; an unreadable file yields ``""``."""
        try:
            st = os.stat(filename)
            stamp = (st.st_mtime_ns, st.st_size)
            hit = self._src_cache.get(filename)
            if hit is None or hit[0] != stamp:
                with open(filename, encoding="utf-8") as fh:
                    hit = (stamp, fh.read().splitlines())
                self._src_cache[filename] = hit
            lines = hit[1]
            return lines[lineno - 1] if 0 < lineno <= len(lines) else ""
        except OSError:
            return ""

    def _push(
        self,
        *,
        qual: str,
        kind: str,
        file: str,
        line: int,
        line_in_def: int,
        src: str | None,
        args: dict[str, Any],
        py: types.FrameType | None,
    ) -> _Frame:
        parent = self.stack[-1]
        occ = parent._occ_next.get(qual, 0)
        parent._occ_next[qual] = occ + 1
        fr = _Frame(
            id=self._next_id,
            key=ts.frame_key(parent.key, qual, occ),
            parent_key=parent.key,
            qual=qual,
            file=file,
            line=line,
            line_in_def=line_in_def,
            src=src,
            occ=occ,
            kind=kind,
            args=args,
            made={},
            touched={},
            py=py,
        )
        self._next_id += 1
        parent.children.append(fr)
        self.stack.append(fr)
        return fr

    def _sync(self, callsite: types.FrameType) -> None:
        """Re-align ``self.stack`` with the live Python stack: drop entries
        whose frames returned, then mint a ``user`` frame for every
        ``/work`` function frame between the deepest live entry and this
        call."""
        live: list[types.FrameType] = []
        f: types.FrameType | None = callsite
        while f is not None:
            live.append(f)
            f = f.f_back
        live_ids = {id(fr) for fr in live}
        stack = self.stack
        prefix = 1  # the synthetic root is always live
        while (
            prefix < len(stack)
            and stack[prefix].py is not None
            and id(stack[prefix].py) in live_ids
        ):
            prefix += 1
        del stack[prefix:]
        # Consume the surviving prefix in stack order, then mint /work
        # frames deeper than it. Pushes grow ``stack`` but ``matched`` is
        # counted against the frozen ``prefix``, so new frames mint freely.
        matched = 1
        for f in reversed(live):
            if matched < prefix:
                # Consecutive stack entries can share one Python frame
                # (``with trace()`` inside a function minted as a user
                # frame) — consume all of them for this live frame.
                while matched < prefix and f is stack[matched].py:
                    matched += 1
                continue
            if self._is_user_frame(f):
                caller = f.f_back
                if caller is not None and self._in_work(caller):
                    file, line = self._file_of(caller), caller.f_lineno
                    line_in_def, src = self._site(caller)
                else:
                    # Outermost user frame: the caller is the driver /
                    # harness outside /work — anchor on the callee's own
                    # def line so the stack's root lands inside the
                    # script (gr513668); the call site is outside the
                    # work root so the re-anchor fields stay empty.
                    file, line = self._file_of(f), f.f_code.co_firstlineno
                    line_in_def, src = 0, None
                self._push(
                    qual=_user_qual(f),
                    kind="user",
                    file=file,
                    line=line,
                    line_in_def=line_in_def,
                    src=src,
                    args=_frame_args(f),
                    py=f,
                )

    def _enter(
        self,
        qual: str,
        kind: str,
        args: dict[str, Any],
        callsite: types.FrameType,
        wrapper_frame: types.FrameType,
    ) -> _Frame:
        self._sync(callsite)
        line_in_def, src = self._site(callsite)
        return self._push(
            qual=qual,
            kind=kind,
            file=self._file_of(callsite),
            line=callsite.f_lineno,
            line_in_def=line_in_def,
            src=src,
            args=args,
            py=wrapper_frame,
        )

    # -- failure anchoring ----------------------------------------------------

    def mark_raise(self, exc: BaseException) -> None:
        """Mark the innermost live frame ``error`` for an ``exc`` that
        left user code without crossing a ``@traced`` call (a bare
        ``raise`` in the script), so the dump still names a culprit.
        No-op when ``exc`` already marked the traced frame it left, or
        only the synthetic root is live."""
        if exc in self._raised or self.stack[-1].kind == "root":
            return
        self.stack[-1].error = type(exc).__name__
        self._raised.add(exc)

    def user_anchor(
        self, tb: types.TracebackType | None
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """``(innermost in-work site, deepest site)`` of a traceback —
        the failure's user-code anchor candidates, minted with the same
        ``_in_work`` predicate the user-frame walk uses. A site is
        ``{file, line}`` (``file`` work_root-relative, or the module/
        basename outside it); the in-work site adds ``qual``
        (``<stem>.<qualname>``) unless the frame is ``<module>``. Either
        side is ``None`` when absent."""
        user = deepest = None
        while tb is not None:
            deepest = tb
            if self._in_work(tb.tb_frame):
                user = tb
            tb = tb.tb_next
        u_at: dict[str, Any] | None = None
        if user is not None:
            f = user.tb_frame
            u_at = {"file": self._file_of(f), "line": user.tb_lineno}
            if f.f_code.co_name != "<module>":
                u_at["qual"] = _user_qual(f)
        d_at = (
            {"file": self._file_of(deepest.tb_frame), "line": deepest.tb_lineno}
            if deepest is not None
            else None
        )
        return u_at, d_at

    # -- serialization ------------------------------------------------------

    def dump(self, run: dict[str, Any] | None = None) -> dict[str, Any]:
        """The schema-2 trace document (post-collapse, capped).

        A ``_sync`` re-mint can in principle repeat a ``(parent, qual,
        occ)`` key (a re-entered generator under the same parent): the
        later frame emits ``sha8(key, "#", args digest, n)`` — n its rank
        among equal keys in emit order — and ``run.key_collisions``
        counts them (0 expected; ``loads`` rejects uncounted repeats, so
        a collision is never a silent merge)."""
        frames: list[dict[str, Any]] = []
        collapsed = 0
        key_collisions = 0
        emitted: dict[str, int] = {}

        def _emit_dict(d: dict[str, Any]) -> None:
            nonlocal key_collisions
            key = d["key"]
            n = emitted.get(key, 0)
            if n:
                digest = json.dumps(d.get("args") or {}, sort_keys=True)
                d = {**d, "key": ts.sha8(f"{key}\x00#\x00{digest}\x00{n}")}
                key_collisions += 1
            emitted[key] = n + 1
            emitted.setdefault(d["key"], 1)
            frames.append(d)

        def emit(fr: _Frame) -> None:
            nonlocal collapsed
            d = {
                "id": fr.id,
                "key": fr.key,
                "parent": fr.parent_key,
                "qual": fr.qual,
                "file": fr.file,
                "line": fr.line,
                "line_in_def": fr.line_in_def,
                "src": fr.src,
                "occ": fr.occ,
                "kind": fr.kind,
                "args": fr.args,
                "made": fr.made,
                "touched": fr.touched,
            }
            if fr.error is not None:
                d["error"] = fr.error
            _emit_dict(d)
            groups: dict[str, list[_Frame]] = {}
            order: list[str] = []
            for ch in fr.children:
                if ch.qual not in groups:
                    groups[ch.qual] = []
                    order.append(ch.qual)
                groups[ch.qual].append(ch)
            for gk in order:
                grp = groups[gk]
                if len(grp) > ts.COLLAPSE_SIBLINGS and all(not c.children for c in grp):
                    for c in grp[: ts.COLLAPSE_SIBLINGS]:
                        emit(c)
                    rest = grp[ts.COLLAPSE_SIBLINGS :]
                    collapsed += len(rest)
                    _emit_dict(
                        {
                            "id": rest[0].id,
                            "id_end": rest[-1].id,
                            "key": ts.frame_key(
                                fr.key, grp[0].qual, ts.COLLAPSE_SIBLINGS
                            ),
                            "parent": fr.key,
                            "qual": grp[0].qual,
                            "file": grp[0].file,
                            "line": grp[0].line,
                            "line_in_def": grp[0].line_in_def,
                            "src": grp[0].src,
                            "occ": ts.COLLAPSE_SIBLINGS,
                            "occ_end": rest[-1].occ,
                            "collapsed": len(rest),
                            "kind": grp[0].kind,
                            "args": rest[0].args,
                            "made": _sum_counts(c.made for c in rest),
                            "touched": _sum_counts(c.touched for c in rest),
                        }
                    )
                else:
                    for c in grp:
                        emit(c)

        emit(self.root)
        truncated = False
        if len(frames) > ts.MAX_FRAMES:
            frames = frames[: ts.MAX_FRAMES]
            truncated = True
        meta = {
            "workspace": None,
            "job": None,
            "script_sha": None,
            "lock_sha": None,
            "started_utc": self.started_utc,
            "finished_utc": _utcnow(),
            "collapsed": collapsed,
            "truncated": truncated,
        }
        meta.update(self.run_meta)
        if run:
            meta.update(run)
        meta["key_collisions"] = key_collisions
        return {"schema": ts.SCHEMA_VERSION, "run": meta, "frames": frames}


def _sum_counts(dicts: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for d in dicts:
        for k, v in d.items():
            if isinstance(v, int):
                out[k] = out.get(k, 0) + v
            else:  # ordinal ranges: concatenate; bounded by the frame cap
                out.setdefault(k, []).extend(v)
    return out


def _stable_repr(v: Any) -> str:
    try:
        r = repr(v)
    except Exception:
        r = f"<unrepr {type(v).__name__}>"
    return _ADDR_RE.sub("0x…", r)[:4096]


def _canon(v: Any) -> Any:
    """A json-able canonical form for hashing — ndarray content goes in as
    ``tobytes()`` (C-speed; ``repr`` of an array is the dominant cost)."""
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, (bytes, bytearray)):
        return ts.sha8(bytes(v).hex())
    if hasattr(v, "tobytes") and hasattr(v, "shape"):  # numpy array, duck-typed
        return ["nd", list(v.shape), ts.sha8(v.tobytes().hex())]
    if dataclasses.is_dataclass(v) and not isinstance(v, type):
        return [
            type(v).__name__,
            [[f.name, _canon(getattr(v, f.name))] for f in dataclasses.fields(v)],
        ]
    if isinstance(v, (list, tuple)):
        return [_canon(x) for x in v]
    if isinstance(v, dict):
        return {
            str(k): _canon(x) for k, x in sorted(v.items(), key=lambda kv: str(kv[0]))
        }
    return _stable_repr(v)


def _arg_digest(v: Any) -> Any:
    """Bounded, deterministic digest of one call argument."""
    if v is None or isinstance(v, (bool, int)):
        return v
    if isinstance(v, float):
        return float(f"{v:.6g}")
    if isinstance(v, str):
        return v if len(v) <= 80 else f"<str {len(v)} {ts.sha8(v)}>"
    if isinstance(v, (bytes, bytearray)):
        return f"<bytes {len(v)} {ts.sha8(bytes(v).hex())}>"
    canon = _canon(v)
    try:
        canon_s = json.dumps(canon, sort_keys=True)
    except (TypeError, ValueError):
        canon_s = str(canon)[:4096]
    n = len(v) if hasattr(v, "__len__") else ""
    return f"<{type(v).__name__} {n} {ts.sha8(canon_s)}>"


def _frame_args(f: types.FrameType) -> dict[str, Any]:
    """Call arguments of a live user-code frame (names + digests)."""
    try:
        info = inspect.getargvalues(f)
    except Exception:
        return {}
    out: dict[str, Any] = {}
    names = list(info.args)
    if info.varargs:
        names.append(info.varargs)
    if info.keywords:
        names.append(info.keywords)
    for name in names:
        if name in info.locals:
            out[name] = _arg_digest(info.locals[name])
    return out


def _user_qual(f: types.FrameType) -> str:
    """``<file stem>.<co_qualname>`` — ``main.bundle`` — the shape the
    python-kind ``ws-<slug>`` root resolves (spec §Join with python)."""
    stem = os.path.splitext(os.path.basename(f.f_code.co_filename))[0]
    return f"{stem}.{f.f_code.co_qualname}"


def _core_qual(fn: Callable[..., Any]) -> str:
    """Module-relative dotted name with the wheel's top package dropped:
    ``precis.cad.graph.Design.prim`` → ``cad.graph.Design.prim``,
    ``hexfold.sheetnet.junction`` → ``sheetnet.junction``."""
    mod = getattr(fn, "__module__", "") or ""
    parts = mod.split(".")
    short = ".".join(parts[1:]) if len(parts) > 1 else mod
    return f"{short}.{fn.__qualname__}" if short else fn.__qualname__


def start(
    *, work_root: str | os.PathLike[str] | None = None, **run_meta: Any
) -> Tracer:
    """Activate a tracer for this context (idempotent — returns the live
    one if already active)."""
    tr = _ACTIVE.get()
    if tr is not None:
        return tr
    tr = Tracer(work_root)
    tr.run_meta.update(run_meta)
    _ACTIVE.set(tr)
    return tr


def stop() -> None:
    _ACTIVE.set(None)


@contextlib.contextmanager
def run(
    *, work_root: str | os.PathLike[str] | None = None, **run_meta: Any
) -> Iterator[Tracer]:
    """Activate a tracer for the block; restores the previous on exit."""
    prev = _ACTIVE.get()
    tr = Tracer(work_root)
    tr.run_meta.update(run_meta)
    _ACTIVE.set(tr)
    try:
        yield tr
    finally:
        _ACTIVE.set(prev)


def current() -> Tracer | None:
    return _ACTIVE.get()


def dump(run: dict[str, Any] | None = None) -> dict[str, Any] | None:
    tr = _ACTIVE.get()
    return tr.dump(run) if tr is not None else None


def record(
    *,
    made: dict[str, int] | None = None,
    touched: dict[str, Any] | None = None,
) -> None:
    """Count entities placed/changed on the innermost live frame. No-op
    when no tracer is active (serve path, plain imports)."""
    tr = _ACTIVE.get()
    if tr is None:
        return
    fr = tr.stack[-1]
    for k, v in (made or {}).items():
        fr.made[k] = fr.made.get(k, 0) + int(v)
    for k, v in (touched or {}).items():
        if isinstance(v, int):
            fr.touched[k] = fr.touched.get(k, 0) + v
        else:
            fr.touched.setdefault(k, []).extend(v)


def leaf_id() -> int | None:
    """The innermost live frame's run-local id — the ``maker`` stamp."""
    tr = _ACTIVE.get()
    return tr.stack[-1].id if tr is not None else None


def _argnames(f: Callable[..., Any]) -> tuple[str, ...]:
    """Positional/keyword parameter names of ``f`` (``co_varnames`` slice)."""
    try:
        code = f.__code__
    except AttributeError:
        return ()
    return code.co_varnames[: code.co_argcount + code.co_kwonlyargcount]


def _call_args(
    names: tuple[str, ...], args: tuple[Any, ...], kwargs: dict[str, Any]
) -> dict[str, Any]:
    """Parameter-name → digest. ``co_varnames`` positional mapping instead
    of ``inspect.signature().bind`` — bind is ~10 µs/call, the dominant
    per-call cost after arg digests. ``names`` is resolved once per
    decorated function (``traced``), not cached in a module-level dict
    keyed by ``id(f)``: a freed function's id is reused, so a global
    cache hands a later function the earlier one's parameter names."""
    out: dict[str, Any] = {}
    for i, a in enumerate(args):
        name = names[i] if i < len(names) else f"_{i}"
        if name in ("self", "cls"):
            continue
        out[name] = _arg_digest(a)
    for k, v in kwargs.items():
        if k not in ("self", "cls"):
            out[k] = _arg_digest(v)
    return out


def traced(
    fn: Callable[..., Any] | None = None,
    *,
    kind: str = "core",
    name: str | None = None,
) -> Any:
    """Push a frame per call on a public primitive. ``kind`` is ``core``
    for wheel functions; promoted agent modules use ``module``."""

    def deco(f: Callable[..., Any]) -> Callable[..., Any]:
        qual = name or _core_qual(f)
        argnames = _argnames(f)

        @functools.wraps(f)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            tr = _ACTIVE.get()
            if tr is None:
                return f(*args, **kwargs)
            fr = tr._enter(
                qual,
                kind,
                _call_args(argnames, args, kwargs),
                sys._getframe(1),
                sys._getframe(0),
            )
            try:
                return f(*args, **kwargs)
            except BaseException as exc:
                if exc not in tr._raised:
                    fr.error = type(exc).__name__
                    tr._raised.add(exc)
                raise
            finally:
                tr.stack.pop()

        return wrapper

    return deco(fn) if fn is not None else deco


@contextlib.contextmanager
def trace(name: str) -> Iterator[None]:
    """``with trace("bundle"):`` — a named user frame without a function
    boundary (optional sugar; nothing requires it)."""
    tr = _ACTIVE.get()
    if tr is None:
        yield
        return
    f = sys._getframe(1)
    tr._sync(f)
    line_in_def, src = tr._site(f)
    fr = tr._push(
        qual=name,
        kind="user",
        file=tr._file_of(f),
        line=f.f_lineno,
        line_in_def=line_in_def,
        src=src,
        args={},
        py=f,
    )
    try:
        yield
    finally:
        if tr.stack and tr.stack[-1] is fr:
            tr.stack.pop()
        elif fr in tr.stack:
            tr.stack.remove(fr)


__all__ = [
    "Tracer",
    "current",
    "dump",
    "leaf_id",
    "record",
    "run",
    "start",
    "stop",
    "trace",
    "traced",
]
