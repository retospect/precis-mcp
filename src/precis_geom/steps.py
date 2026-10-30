"""``precis_geom.steps`` — the build-step cache, one module for all three
scales (molecular-workspace-code slice 0; the ruling is se-flow-kernel
§Build-step stack cache, Reto 2026-10-23).

    from precis_geom import steps

    @steps.step
    def tube(periods, rows):                     # pure: params in, object out
        net, _ = prism_tube(periods, rows, 2, "++++++")
        return pga.geom(net, coords=stick(net))

    p = tube(4, 4)            # miss: runs, stores; hit: loads, skips the cost

A step is a pure function returning a :class:`~precis_geom.assembly.Geom`
(atomic, scene only), a cad ``SceneSpec`` (or DSL source text) or a pcb
:class:`~precis_geom.pcb.Board`. Re-running the script after a crash or an
edit is resume: every completed step before the first changed one is a
hit.

**Key** = sha256 of ``(step name, canonical params, parent step keys,
precis_geom.__version__, sha of the decorated function's own source)``.
Parents are the step-made objects among the arguments; an object built
outside a step keys by content (coordinates, no makers). The source sha
covers the step function's body, not its callees — an edit to a helper
can hit stale (``steps.clear()`` is the escape hatch; every hit prints
nothing, but ``steps.ls()`` shows the name, version and source sha).

**Storage** is content-addressed, ``<cache>/<key[:2]>/<key>/``: the
scale's artifact (atomic: ``scene.extxyz`` + ``scene.bonds.json`` — the
bytes ``out._write_atomic`` writes — plus ``scene.extra.json`` and
``scene.frac.f64``, the exact fractional coordinates and the fields
extxyz cannot carry, so a hit is bit-identical to a fresh build; macro:
``scene.cad.json`` or ``scene.cad``; pcb: ``board.json`` + the
``board.state.json`` sidecar) and ``step.json`` ``{key, name, params,
parents, ports, findings, wall_s, trace, ...}``. A ``maker`` column in an
artifact holds *relative codes*, never live frame ids (below), so a stored
entry is only meaningful through :func:`load` or a hit.

**Where.** A host run keeps ``<work root>/cache`` (``scratch/cache/``),
LRU-capped at 2 GB; the sandbox lane (work root ``/work``) keeps an
in-process LRU. ``PRECIS_STEPS_CACHE`` (a path, ``memory`` or ``off``),
``PRECIS_STEPS_CACHE_BYTES`` and :func:`configure` override;
``PRECIS_STEPS_FRESH=1`` ignores existing entries (and rewrites them).

**Trace.** Each call mints a ``step.<name>`` core frame carrying
``step: {key, hit}``. A miss records the frame subtree the function
built, relative to its root; a hit grafts that subtree under the live
step frame (``Tracer.graft``: fresh ids and keys from the live parent
chain) and remaps every ``maker`` in the artifact through the same table
— own frames by relative index, frames of an input step by (input index,
relative index), anything else by frame key. A stored entry that cannot
be remapped (trace schema changed, a referenced frame is absent, no
trace was recorded) is a miss, never a wrong maker. The run header gets
``steps: {hits, rebuilt, first_miss, wall_saved_s}``
(``Tracer.dump`` → ``Run.finish``).

Stdlib + numpy; the scale-specific classes are imported when needed.
"""

from __future__ import annotations

import contextlib
import dataclasses
import functools
import hashlib
import inspect
import json
import os
import pickle
import re
import shutil
import sys
import time
import uuid
import warnings
from collections import OrderedDict
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

import precis_geom as _pg
from precis.errors import BadInput
from precis_geom import trace as _trace
from precis_geom import trace_schema as _ts

_FORMAT = 1
#: Default LRU caps: the host dir and the in-process fallback.
_DEFAULT_DISK_BYTES = 2 * 1024**3
_DEFAULT_MEM_BYTES = 512 * 1024**2
_ADDR_RE = re.compile(r"0x[0-9a-fA-F]+")


class _Unstorable(Exception):
    """The value cannot be stored faithfully — the step runs uncached."""


class _GraftMismatch(Exception):
    """A stored entry cannot be re-attached to the live trace — a miss."""


# ── canonical JSON for step.json and the digests ─────────────────────────


def _jenc(v: Any, *, strict: bool = True) -> Any:
    """JSON-safe form of a value: tuples and ndarrays are tagged so they
    come back as themselves; anything else raises :class:`_Unstorable`
    (``strict``) or degrades to an address-free ``repr`` (digests)."""
    if v is None or isinstance(v, (bool, int, str, float)):
        return v
    if isinstance(v, np.generic):
        return _jenc(v.item(), strict=strict)
    if isinstance(v, np.ndarray):
        if v.dtype.kind not in "biuf":
            if strict:
                raise _Unstorable(f"ndarray dtype {v.dtype}")
            return {"__repr__": f"ndarray<{v.dtype}{list(v.shape)}>"}
        return {"__nd__": [str(v.dtype), list(v.shape), v.ravel().tolist()]}
    if isinstance(v, tuple):
        return {"__t__": [_jenc(x, strict=strict) for x in v]}
    if isinstance(v, list):
        return [_jenc(x, strict=strict) for x in v]
    if isinstance(v, dict):
        out: dict[str, Any] = {}
        for k, x in v.items():
            if not isinstance(k, str) or k in ("__t__", "__nd__", "__repr__"):
                if strict:
                    raise _Unstorable(f"dict key {k!r}")
                k = str(k)
            out[k] = _jenc(x, strict=strict)
        return out
    if strict:
        raise _Unstorable(type(v).__name__)
    if dataclasses.is_dataclass(v) and not isinstance(v, type):
        return {
            "__repr__": type(v).__name__,
            "f": {
                f.name: _jenc(getattr(v, f.name), strict=False)
                for f in dataclasses.fields(v)
            },
        }
    return {"__repr__": _ADDR_RE.sub("0x…", repr(v))}


def _jdec(v: Any) -> Any:
    if isinstance(v, list):
        return [_jdec(x) for x in v]
    if isinstance(v, dict):
        if set(v) == {"__t__"}:
            return tuple(_jdec(x) for x in v["__t__"])
        if set(v) == {"__nd__"}:
            dtype, shape, data = v["__nd__"]
            return np.asarray(data, dtype=dtype).reshape(shape)
        return {k: _jdec(x) for k, x in v.items()}
    return v


def _dumps(v: Any) -> str:
    return json.dumps(v, sort_keys=True, separators=(",", ":"))


def _sha(text: str | bytes) -> str:
    b = text.encode("utf-8") if isinstance(text, str) else text
    return hashlib.sha256(b).hexdigest()


# ── scale dispatch (classes imported only when their module is loaded) ───


def _loaded(mod: str, name: str) -> Any:
    m = sys.modules.get(mod)
    return getattr(m, name, None) if m is not None else None


def _kind_of(obj: Any) -> str | None:
    """``geom`` | ``spec`` | ``dsl`` | ``board`` for a step-storable value,
    else ``None``. A module that is not imported cannot have made the
    object, so it is not imported here."""
    geom = _loaded("precis_geom.assembly", "Geom")
    if geom is not None and isinstance(obj, geom):
        return "geom"
    board = _loaded("precis_geom.pcb", "Board")
    if board is not None and isinstance(obj, board):
        return "board"
    spec = _loaded("precis.cad.scene", "SceneSpec")
    if spec is not None and isinstance(obj, spec):
        return "spec"
    if isinstance(obj, str):
        return "dsl"
    return None


# ── content digests (an input built outside a step; the stamp check) ─────


def _strip_frames(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: v for k, v in r.items() if k != "frame"} for r in rows]


def _digest_geom(g: Any) -> str:
    h = hashlib.sha256()

    def put(x: Any) -> None:
        h.update(_dumps(_jenc(x, strict=False)).encode("utf-8"))
        h.update(b"\0")

    sc = g.scene
    if sc is not None:
        labels = list(sc.atoms)
        put([labels, [sc.atoms[la].element for la in labels]])
        cart = np.asarray(
            [sc.cell.frac_to_cart(sc.atoms[la].frac) for la in labels], dtype=float
        ).reshape(-1, 3)
        h.update((np.round(cart, 6) + 0.0).astype("<f8").tobytes())
        put((np.round(np.asarray(sc.cell.lattice, dtype=float), 6) + 0.0).tolist())
        put([bool(p) for p in sc.cell.pbc])
        put(
            [[b.i, b.j, b.order, b.kind, b.provenance, list(b.image)] for b in sc.bonds]
        )
        put(_atom_extras(sc))
    if g.design is not None:
        put(uuid.uuid4().hex)  # a cad Design has no stable content form
    put({n: dataclasses.asdict(p) for n, p in g.ports.items()})
    put(list(g.findings))
    put({k: v for k, v in g.meta.items() if k not in ("step", "_net")})
    put([g.period, g.fused, g.template, g.array])
    if g.pose is not None:
        put([g.pose.R, g.pose.t])
    for child, xf, _fid in g.placements:
        put(_digest_geom(child))
        put([xf.R, xf.t])
    return h.hexdigest()


def _digest_spec(spec: Any) -> str:
    from precis_geom import out as _out

    if spec.field_loader is not None or spec.hmap_loader is not None:
        return uuid.uuid4().hex  # runtime loaders are not content
    doc = _out._macro_doc(spec)
    for n in doc["nodes"]:
        n.pop("frame", None)
    return _sha(_dumps(_jenc(doc, strict=False)))


def _digest_board(b: Any) -> str:
    batch = b.to_batch()
    for sec in ("components", "nets"):
        if sec in batch:
            batch[sec] = _strip_frames(batch[sec])
    return _sha(_dumps(_jenc(batch, strict=False)))


def _digest(obj: Any, kind: str) -> str:
    if kind == "geom":
        return _digest_geom(obj)
    if kind == "spec":
        return _digest_spec(obj)
    if kind == "board":
        return _digest_board(obj)
    return _sha(obj)


# ── stamps: which step made an object, and which live frames are its ─────


@dataclass(slots=True)
class _Inst:
    """One live execution of a step: its key, the live frame id of every
    node of its relative subtree (``ids[0]`` is the step frame) and the
    content fingerprint of the object it returned."""

    key: str
    name: str
    ids: list[int]
    rel: dict[int, int]
    fp: str


def _stamp(obj: Any, inst: _Inst) -> None:
    with contextlib.suppress(AttributeError, TypeError):
        object.__setattr__(obj, "_step_inst", inst)


def _live_inst(obj: Any, kind: str) -> _Inst | None:
    """The stamp, only while the object still equals what the step
    returned — a mutated SceneSpec/Board (or a Geom whose scene was
    edited in place) falls back to a content key."""
    inst = getattr(obj, "_step_inst", None)
    if isinstance(inst, _Inst) and inst.fp == _digest(obj, kind):
        return inst
    return None


@dataclass(slots=True)
class _Par:
    obj: Any
    kind: str
    inst: _Inst | None
    digest: str

    @property
    def key(self) -> str:
        if self.inst is not None:
            return self.inst.key
        return f"content:{self.kind}:{self.digest}"


def _pcanon(v: Any, parents: list[_Par], seen: dict[int, int], depth: int = 0) -> Any:
    """Canonical JSON form of a call argument. Step-storable objects become
    ``{"$in": index}`` and are appended to ``parents``; anything that has
    no canonical form raises :class:`BadInput` — a key that silently
    ignored an argument would serve a wrong hit."""
    if depth > 24:
        raise BadInput("steps: parameter nesting deeper than 24 levels")
    if v is None or isinstance(v, (bool, int, str)):
        return v
    if isinstance(v, float):
        return {"f": repr(v)}
    if isinstance(v, np.generic):
        return _pcanon(v.item(), parents, seen, depth)
    if isinstance(v, np.ndarray):
        return {
            "nd": [str(v.dtype), list(v.shape), _sha(np.ascontiguousarray(v).tobytes())]
        }
    if isinstance(v, (bytes, bytearray)):
        return {"b": _sha(bytes(v))}
    if isinstance(v, Path):
        return {"path": str(v)}
    kind = _kind_of(v) if not isinstance(v, str) else None
    if kind is not None:
        ix = seen.get(id(v))
        if ix is None:
            inst = _live_inst(v, kind)
            parents.append(
                _Par(v, kind, inst, "" if inst is not None else _digest(v, kind))
            )
            ix = seen[id(v)] = len(parents) - 1
        return {"$in": ix}
    if isinstance(v, list):
        return [_pcanon(x, parents, seen, depth + 1) for x in v]
    if isinstance(v, tuple):
        return {"t": [_pcanon(x, parents, seen, depth + 1) for x in v]}
    if isinstance(v, (set, frozenset)):
        return {
            "s": sorted(_dumps(_pcanon(x, parents, seen, depth + 1)) for x in v),
        }
    if isinstance(v, dict):
        items = sorted(
            ((_dumps(_pcanon(k, parents, seen, depth + 1)), v[k]) for k in v),
            key=lambda kv: kv[0],
        )
        return {
            "d": [[k, _pcanon(x, parents, seen, depth + 1)] for k, x in items],
        }
    if (
        dataclasses.is_dataclass(v)
        and not isinstance(v, type)
        and getattr(getattr(type(v), "__dataclass_params__", None), "frozen", False)
        and not type(v).__module__.startswith("hexfold")
    ):
        # a frozen value object (Transform, Port, Bound …); a hexfold Net
        # carries run-local makers, so it is refused below
        return {
            "dc": [
                type(v).__qualname__,
                [
                    [f.name, _pcanon(getattr(v, f.name), parents, seen, depth + 1)]
                    for f in dataclasses.fields(v)
                ],
            ]
        }
    raise BadInput(
        f"steps: cannot key a {type(v).__name__} parameter — a step's "
        "parameters are plain values (numbers, strings, lists, dicts, "
        "arrays) and step-made objects",
        next="pass the recipe values and build the object inside the step "
        "(a hexfold Net enters the cache through pga.geom, not as a "
        "parameter)",
    )


# ── storage backends ─────────────────────────────────────────────────────


def _max_bytes(default: int) -> int:
    if _CFG["max_bytes"] is not None:
        return int(_CFG["max_bytes"])
    raw = os.environ.get("PRECIS_STEPS_CACHE_BYTES")
    return int(raw) if raw else default


def _entry_bytes(files: dict[str, bytes]) -> int:
    return sum(len(b) for b in files.values())


class _Mem:
    """The in-process LRU: ``key → (files, uses)``, oldest first."""

    kind = "memory"

    def __init__(self) -> None:
        self.data: OrderedDict[str, tuple[dict[str, bytes], dict[str, Any]]] = (
            OrderedDict()
        )

    def get(self, key: str) -> dict[str, bytes] | None:
        hit = self.data.get(key)
        return dict(hit[0]) if hit is not None else None

    def put(self, key: str, files: dict[str, bytes], uses: dict[str, Any]) -> None:
        self.data.pop(key, None)
        self.data[key] = (dict(files), {**uses, "bytes": _entry_bytes(files)})
        total = sum(u["bytes"] for _f, u in self.data.values())
        cap = _max_bytes(_DEFAULT_MEM_BYTES)
        while total > cap and len(self.data) > 1:
            old, (_f, u) = next(iter(self.data.items()))
            if old == key:
                break
            del self.data[old]
            total -= u["bytes"]

    def bump(self, key: str) -> None:
        hit = self.data.get(key)
        if hit is not None:
            hit[1]["hits"] = int(hit[1].get("hits", 0)) + 1
            hit[1]["used_ns"] = time.time_ns()
            self.data.move_to_end(key)

    def drop(self, key: str) -> None:
        self.data.pop(key, None)

    def uses(self) -> Iterator[tuple[str, dict[str, Any]]]:
        for k, (_f, u) in list(self.data.items()):
            yield k, dict(u)

    def clear(self) -> int:
        n = len(self.data)
        self.data.clear()
        return n


class _Disk:
    """``<root>/<key[:2]>/<key>/`` — one dir per entry, written to a temp
    dir and renamed in (a killed run leaves a ``.tmp-*``, never a torn
    entry), LRU-evicted by each entry's ``uses.json`` stamp."""

    kind = "disk"
    _USES = "uses.json"

    def __init__(self, root: Path) -> None:
        self.root = root

    def _dir(self, key: str) -> Path:
        return self.root / key[:2] / key

    def get(self, key: str) -> dict[str, bytes] | None:
        d = self._dir(key)
        try:
            files = {n: (d / n).read_bytes() for n in os.listdir(d) if n != self._USES}
        except OSError:
            return None
        return files if "step.json" in files else None

    def put(self, key: str, files: dict[str, bytes], uses: dict[str, Any]) -> None:
        d = self._dir(key)
        d.parent.mkdir(parents=True, exist_ok=True)
        tmp = d.parent / f".tmp-{key[:12]}-{os.getpid()}-{time.monotonic_ns()}"
        tmp.mkdir()
        try:
            for n, b in files.items():
                (tmp / n).write_bytes(b)
            (tmp / self._USES).write_text(
                json.dumps({**uses, "bytes": _entry_bytes(files)}), encoding="utf-8"
            )
            if d.exists():
                shutil.rmtree(d, ignore_errors=True)
            os.replace(tmp, d)
        except OSError:
            shutil.rmtree(tmp, ignore_errors=True)
            if not d.exists():
                raise
        self._evict(keep=key)

    def _read_uses(self, d: Path) -> dict[str, Any] | None:
        try:
            return json.loads((d / self._USES).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def bump(self, key: str) -> None:
        d = self._dir(key)
        u = self._read_uses(d)
        if u is None:
            return
        u["hits"] = int(u.get("hits", 0)) + 1
        u["used_ns"] = time.time_ns()
        with contextlib.suppress(OSError):
            (d / self._USES).write_text(json.dumps(u), encoding="utf-8")

    def drop(self, key: str) -> None:
        shutil.rmtree(self._dir(key), ignore_errors=True)

    def _shards(self) -> Iterator[Path]:
        try:
            for s in os.scandir(self.root):
                if s.is_dir() and re.fullmatch(r"[0-9a-f]{2}", s.name):
                    yield Path(s.path)
        except OSError:
            return

    def uses(self) -> Iterator[tuple[str, dict[str, Any]]]:
        for shard in self._shards():
            for e in os.scandir(shard):
                if e.is_dir() and not e.name.startswith(".tmp-"):
                    u = self._read_uses(Path(e.path))
                    if u is not None:
                        yield e.name, u

    def _evict(self, *, keep: str) -> None:
        rows = [(u.get("used_ns", 0), u.get("bytes", 0), k) for k, u in self.uses()]
        total = sum(r[1] for r in rows)
        cap = _max_bytes(_DEFAULT_DISK_BYTES)
        for _used, size, k in sorted(rows):
            if total <= cap:
                break
            if k == keep:
                continue
            self.drop(k)
            total -= size
        # torn writes from killed runs
        for shard in self._shards():
            for e in os.scandir(shard):
                if (
                    e.name.startswith(".tmp-")
                    and time.time() - e.stat().st_mtime > 3600
                ):
                    shutil.rmtree(e.path, ignore_errors=True)

    def clear(self) -> int:
        n = 0
        for shard in self._shards():
            n += sum(1 for e in os.scandir(shard) if not e.name.startswith(".tmp-"))
            shutil.rmtree(shard, ignore_errors=True)
        return n


_CFG: dict[str, Any] = {"dir": None, "max_bytes": None}
_MEM = _Mem()
_DISKS: dict[str, _Disk] = {}


def configure(
    cache_dir: str | os.PathLike[str] | None = None, max_bytes: int | None = None
) -> None:
    """Override the cache location (a path, ``"memory"`` or ``"off"``) and
    the LRU byte cap; no arguments restore the defaults."""
    _CFG["dir"] = None if cache_dir is None else str(cache_dir)
    _CFG["max_bytes"] = max_bytes


def _backend() -> _Mem | _Disk | None:
    spec = _CFG["dir"] or os.environ.get("PRECIS_STEPS_CACHE")
    if spec == "off":
        return None
    if spec == "memory":
        return _MEM
    if spec:
        root = Path(spec)
    else:
        tr = _trace.current()
        if tr is not None and os.path.normpath(tr.work_root) == "/work":
            return _MEM  # the sandbox lane: in-process only (slice 0b adds the dir)
        root = (Path(tr.work_root) if tr is not None else Path("scratch")) / "cache"
    return _DISKS.setdefault(str(root.resolve()), _Disk(root))


# ── the atomic scale: Geom ↔ extxyz + bonds.json + exact sidecars ────────

_ATOM_DEFAULTS = {
    "fixed": 0,
    "magmom": None,
    "oxidation": None,
    "charge": 0,
    "hybridization": None,
}


def _atom_extras(scene: Any) -> dict[str, Any]:
    """What extxyz / bonds.json cannot carry: per-atom intent fields,
    bond provenance/image, measures and the label high-water marks."""
    atoms = {}
    for label, a in scene.atoms.items():
        extra = {
            k: getattr(a, k)
            for k, dflt in _ATOM_DEFAULTS.items()
            if getattr(a, k, dflt) != dflt
        }
        if extra:
            atoms[label] = extra
    bonds = {
        str(i): {"provenance": b.provenance, "image": list(b.image)}
        for i, b in enumerate(scene.bonds)
        if b.provenance != "declared" or tuple(b.image) != (0, 0, 0)
    }
    out: dict[str, Any] = {}
    if atoms:
        out["atoms"] = atoms
    if bonds:
        out["bonds"] = bonds
    measures = getattr(scene, "measures", None)
    if measures:
        out["measures"] = [dataclasses.asdict(m) for m in measures]
    if getattr(scene, "label_hi", None):
        out["label_hi"] = dict(scene.label_hi)
    return out


class _Codec:
    """Maker ↔ relative code. A code ``>= 0`` is a node of the step's own
    subtree, ``None`` no maker, ``<= -2`` an index into ``ext``:
    ``["s", input_index, rel]`` (a frame of an input step) or
    ``["f", frame_key]`` (any other live frame)."""

    def __init__(
        self,
        tr: _trace.Tracer | None,
        own_rel: dict[int, int],
        parents: list[_Par],
    ) -> None:
        self.tr, self.own_rel, self.parents = tr, own_rel, parents
        self.ext: list[list[Any]] = []
        self._ext_ix: dict[str, int] = {}

    def _ext(self, item: list[Any]) -> int:
        k = _dumps(item)
        if k not in self._ext_ix:
            self._ext_ix[k] = len(self.ext)
            self.ext.append(item)
        return -2 - self._ext_ix[k]

    def enc(self, m: int | None) -> int | None:
        if m is None or self.tr is None:
            return None
        r = self.own_rel.get(m)
        if r is not None:
            return r
        for pi, p in enumerate(self.parents):
            if p.inst is not None and m in p.inst.rel:
                return self._ext(["s", pi, p.inst.rel[m]])
        fr = self.tr.by_id.get(m)
        return self._ext(["f", fr.key]) if fr is not None else None


def _map_makers(obj: Any, kind: str, f: Callable[[int | None], int | None]) -> None:
    """Apply ``f`` to every maker stamp of ``obj`` in place."""
    if kind == "geom":
        scenes = [obj.scene]
        ref = obj.meta.get("_net")
        if ref is not None:  # the exported Net: its atoms and (maybe) its scene
            if ref.scene is not obj.scene:
                scenes.append(ref.scene)
            ref.net = dataclasses.replace(
                ref.net,
                atoms=tuple(
                    dataclasses.replace(a, maker=f(a.maker)) for a in ref.net.atoms
                ),
            )
        for sc in scenes:
            for a in sc.atoms.values():
                a.maker = f(a.maker)
            for b in sc.bonds:
                b.maker = f(b.maker)
    elif kind == "spec":
        obj.nodes[:] = [dataclasses.replace(n, frame=f(n.frame)) for n in obj.nodes]
    elif kind == "board":
        for p in obj._parts.values():
            p.maker = f(p.maker)
        for n in obj._nets.values():
            n.maker = f(n.maker)


def _scene_files(scene: Any, codec: _Codec, stem: str) -> dict[str, bytes]:
    """``<stem>.extxyz`` (+ ``.bonds.json``) — the bytes ``out`` writes,
    makers as relative codes — plus ``<stem>.frac.f64`` / ``.extra.json``,
    the exact coordinates and the fields extxyz drops."""
    from precis.structure.scene import Scene
    from precis_geom import out as _out

    lat = np.asarray(scene.cell.lattice, dtype=float)
    if lat.shape != (3, 3) or abs(np.linalg.det(lat)) < 1e-12:
        raise _Unstorable("a singular cell")
    shim = Scene(
        cell=scene.cell,
        atoms={
            la: dataclasses.replace(a, maker=codec.enc(a.maker))
            for la, a in scene.atoms.items()
        },
        bonds=[dataclasses.replace(b, maker=codec.enc(b.maker)) for b in scene.bonds],
    )
    body, bonds_body = _out._atomic_texts(shim)
    frac = np.asarray([a.frac for a in scene.atoms.values()], dtype="<f8")
    extra = _jenc(
        {
            "lattice": lat.tolist(),
            "pbc": [bool(p) for p in scene.cell.pbc],
            **_atom_extras(scene),
        }
    )
    files = {
        f"{stem}.extxyz": body.encode("utf-8"),
        f"{stem}.frac.f64": frac.reshape(-1, 3).tobytes(),
        f"{stem}.extra.json": _dumps(extra).encode("utf-8"),
    }
    if bonds_body is not None:
        files[f"{stem}.bonds.json"] = bonds_body.encode("utf-8")
    return files


def _encode_geom(g: Any, codec: _Codec) -> tuple[dict[str, bytes], dict[str, Any]]:
    from precis_geom import assembly as _pga

    if (
        g.scene is None
        or g.design is not None
        or g.placements
        or g.template is not None
        or g.array is not None
        or g.pose is not None
    ):
        raise _Unstorable(
            "a Geom with placements, a pose, a symbolic template or a cad "
            "design — flatten it inside the step: pga.geom(g.flatten(), …)"
        )
    files = _scene_files(g.scene, codec, "scene")
    meta_in = {k: v for k, v in g.meta.items() if k not in ("step", "_net")}
    gdoc: dict[str, Any] = {"meta": meta_in, "period": g.period, "fused": g.fused}
    ref = g.meta.get("_net")
    if ref is not None:
        # The hexfold Net a pga.geom exported: mol.cap / mol.sites read it,
        # and a Net is not JSON — it rides as a pickle (the cache is the
        # user's own scratch dir or this process; never a trust boundary).
        if not isinstance(ref, _pga.NetRef):
            raise _Unstorable(f"meta['_net'] is a {type(ref).__name__}")
        net = dataclasses.replace(
            ref.net,
            atoms=tuple(
                dataclasses.replace(a, maker=codec.enc(a.maker)) for a in ref.net.atoms
            ),
        )
        files["net.pkl"] = pickle.dumps(net, protocol=pickle.HIGHEST_PROTOCOL)
        files["net.xyz.f64"] = np.asarray(ref.xyz, dtype="<f8").reshape(-1, 3).tobytes()
        pristine = ref.scene is g.scene
        if not pristine:
            files.update(_scene_files(ref.scene, codec, "netscene"))
        gdoc["net"] = {"pristine": pristine}
    meta = {
        "ports": [_jenc(dataclasses.asdict(p)) for p in g.ports.values()],
        "findings": [str(x) for x in g.findings],
        "geom": _jenc(gdoc),
    }
    return files, meta


_EXTXYZ_PROPS = re.compile(r"Properties=(\S+)")


def _scene_from(files: dict[str, bytes], stem: str) -> Any:
    from precis.structure.cell import Cell
    from precis.structure.scene import Atom, Bond, Measure, Scene

    lines = files[f"{stem}.extxyz"].decode("utf-8").splitlines()
    n = int(lines[0])
    props = _EXTXYZ_PROPS.search(lines[1])
    if props is None or not props.group(1).startswith("species:S:1:pos:R:3:label:S:1"):
        raise _GraftMismatch(f"{stem}.extxyz: unexpected Properties")
    has_maker = ":maker:I:1" in props.group(1)
    extra = _jdec(json.loads(files[f"{stem}.extra.json"].decode("utf-8")))
    lattice = np.asarray(extra["lattice"], dtype=float)
    px, py, pz = (bool(p) for p in extra["pbc"])
    cell = Cell(lattice, (px, py, pz))
    frac = np.frombuffer(files[f"{stem}.frac.f64"], dtype="<f8").reshape(-1, 3).copy()
    if frac.shape[0] != n:
        raise _GraftMismatch(f"{stem}.frac.f64: atom count differs from extxyz")
    atom_extra = extra.get("atoms") or {}
    atoms: dict[str, Atom] = {}
    for i in range(n):
        row = lines[2 + i].split()
        label = row[4]
        mk = int(row[5]) if has_maker else None
        atoms[label] = Atom(
            label=label,
            element=row[0],
            frac=frac[i],
            maker=None if mk is None or mk == -1 else mk,
            **atom_extra.get(label, {}),
        )
    bonds: list[Bond] = []
    if f"{stem}.bonds.json" in files:
        brows = json.loads(files[f"{stem}.bonds.json"].decode("utf-8"))["bonds"]
        bextra = extra.get("bonds") or {}
        for i, br in enumerate(brows):
            bx = bextra.get(str(i)) or {}
            bonds.append(
                Bond(
                    i=br["i"],
                    j=br["j"],
                    order=br.get("order", 1.0),
                    kind=br.get("kind", "pairwise"),
                    provenance=bx.get("provenance", "declared"),
                    image=tuple(bx["image"]) if "image" in bx else (0, 0, 0),
                    maker=br.get("maker"),
                )
            )
    return Scene(
        cell=cell,
        atoms=atoms,
        bonds=bonds,
        measures=[Measure(**m) for m in extra.get("measures", ())],
        label_hi=dict(extra.get("label_hi") or {}),
    )


def _decode_geom(files: dict[str, bytes], doc: dict[str, Any]) -> Any:
    from precis_geom import assembly as _pga

    scene = _scene_from(files, "scene")
    gd = _jdec(doc["geom"])
    meta = gd["meta"]
    if "net" in gd:
        xyz = np.frombuffer(files["net.xyz.f64"], dtype="<f8").reshape(-1, 3).copy()
        ref_scene = scene if gd["net"]["pristine"] else _scene_from(files, "netscene")
        meta["_net"] = _pga.NetRef(pickle.loads(files["net.pkl"]), xyz, ref_scene)
    ports = {}
    for p in _jdec(doc["ports"]):
        ports[p["name"]] = _pga.Port(
            name=p["name"],
            pos=tuple(p["pos"]) if p["pos"] is not None else None,
            direction=tuple(p["direction"]) if p["direction"] is not None else None,
            roles=tuple(p["roles"]),
            atom=p["atom"],
            annotations=p["annotations"],
        )
    return _pga.Geom(
        scene=scene,
        ports=ports,
        period=gd["period"],
        fused=gd["fused"],
        meta=meta,
        findings=tuple(doc["findings"]),
    )


# ── the macro scale: SceneSpec → .cad.json, DSL text → .cad ──────────────


def _encode_spec(spec: Any, codec: _Codec) -> tuple[dict[str, bytes], dict[str, Any]]:
    from precis_geom import out as _out

    if spec.field_loader is not None or spec.hmap_loader is not None:
        raise _Unstorable("a SceneSpec carrying field:/relief: loaders")
    enc = dataclasses.replace(
        spec,
        nodes=[dataclasses.replace(n, frame=codec.enc(n.frame)) for n in spec.nodes],
    )
    body = json.dumps(_out._macro_doc(enc), indent=1) + "\n"
    return {"scene.cad.json": body.encode("utf-8")}, {"ports": [], "findings": []}


def _vec3(raw: Any) -> tuple[float, float, float]:
    x, y, z = (float(v) for v in raw or (0.0, 0.0, 0.0))
    return (x, y, z)


def _decode_spec(files: dict[str, bytes], doc: dict[str, Any]) -> Any:
    from precis.cad.scene import NodeSpec, SceneSpec, coerce_pattern

    data = json.loads(files["scene.cad.json"].decode("utf-8"))
    nodes = []
    for raw in data["nodes"]:
        frame = raw.get("frame")
        nodes.append(
            NodeSpec(
                name=str(raw["name"]),
                op=str(raw.get("op") or "add"),
                config=str(raw["config"]),
                component=str(raw.get("component") or "part"),
                loc=_vec3(raw.get("loc")),
                rot=_vec3(raw.get("rot")),
                pattern=coerce_pattern(raw.get("pattern")),
                blend=float(raw.get("blend") or 0.0),
                frame=int(frame) if frame is not None else None,
            )
        )
    return SceneSpec(
        nodes=nodes,
        components=[str(c) for c in data.get("components") or ["part"]],
        meta=dict(data.get("meta") or {"units": "mm"}),
    )


# ── the pcb scale: Board ↔ board.json + board.state.json ─────────────────


def _encode_board(b: Any, codec: _Codec) -> tuple[dict[str, bytes], dict[str, Any]]:
    batch = b.to_batch()
    for sec in ("components", "nets"):
        for row in batch.get(sec, ()):
            if "frame" in row:
                code = codec.enc(row["frame"])
                if code is None:
                    del row["frame"]
                else:
                    row["frame"] = code
    state = b.cache_state()
    try:
        files = {
            "board.json": (json.dumps(_jenc(batch), indent=1) + "\n").encode("utf-8"),
            "board.state.json": _dumps(_jenc(state)).encode("utf-8"),
        }
    except _Unstorable as exc:
        raise _Unstorable(f"a Board holding {exc} in its batch") from exc
    return files, {"ports": [], "findings": []}


def _decode_board(files: dict[str, bytes], doc: dict[str, Any]) -> Any:
    from precis_geom import pcb as _pcb

    batch = _jdec(json.loads(files["board.json"].decode("utf-8")))
    state = _jdec(json.loads(files["board.state.json"].decode("utf-8")))
    return _pcb.Board.from_batch(batch, state)


def _encode_dsl(text: str, codec: _Codec) -> tuple[dict[str, bytes], dict[str, Any]]:
    return {"scene.cad": text.encode("utf-8")}, {"ports": [], "findings": []}


def _decode_dsl(files: dict[str, bytes], doc: dict[str, Any]) -> Any:
    return files["scene.cad"].decode("utf-8")


_ENCODE = {
    "geom": _encode_geom,
    "spec": _encode_spec,
    "board": _encode_board,
    "dsl": _encode_dsl,
}
_DECODE = {
    "geom": _decode_geom,
    "spec": _decode_spec,
    "board": _decode_board,
    "dsl": _decode_dsl,
}


# ── store / materialise one entry ────────────────────────────────────────


def _finalize(obj: Any, kind: str, key: str, name: str) -> Any:
    """The object the caller receives: a Geom gains ``meta['step']``."""
    if kind == "geom":
        return dataclasses.replace(obj, meta={**obj.meta, "step": key})
    return obj


def _make_inst(obj: Any, kind: str, key: str, name: str, ids: list[int]) -> _Inst:
    return _Inst(
        key=key,
        name=name,
        ids=ids,
        rel={i: r for r, i in enumerate(ids)},
        fp=_digest(obj, kind),
    )


def _store(
    be: _Mem | _Disk,
    *,
    key: str,
    name: str,
    kind: str,
    obj: Any,
    params: Any,
    parents: list[_Par],
    wall_s: float,
    src_sha: str,
    tr: _trace.Tracer | None,
    fr: Any,
) -> list[int]:
    """Encode ``obj`` and write the entry; returns the live id of every
    node of the step's subtree (``[]`` without a tracer). Raises
    :class:`_Unstorable` before anything is written."""
    nodes: list[dict[str, Any]] | None = None
    ids: list[int] = []
    if tr is not None:
        nodes, ids = tr.export_subtree(fr)
    codec = _Codec(tr, {i: r for r, i in enumerate(ids)}, parents)
    files, extra = _ENCODE[kind](obj, codec)
    doc = {
        "format": _FORMAT,
        "key": key,
        "name": name,
        "kind": kind,
        "params": params,
        "parents": [p.key for p in parents],
        "ports": extra.pop("ports"),
        "findings": extra.pop("findings"),
        "wall_s": round(wall_s, 6),
        "version": _pg.__version__,
        "src_sha": src_sha,
        "ext": codec.ext,
        "trace": (
            {"schema": _ts.SCHEMA_VERSION, "nodes": _jenc(nodes)}
            if nodes is not None
            else None
        ),
        **extra,
    }
    files["step.json"] = _dumps(doc).encode("utf-8")
    be.put(
        key,
        files,
        {
            "name": name,
            "params_digest": _sha(_dumps(params))[:16],
            "wall_s": round(wall_s, 6),
            "hits": 0,
            "used_ns": time.time_ns(),
            "version": _pg.__version__,
            "src_sha": src_sha[:16],
        },
    )
    return ids


def _materialise(
    files: dict[str, bytes],
    key: str,
    *,
    tr: _trace.Tracer | None,
    fr: Any,
    parents: list[_Par],
    lenient: bool,
    user_qual: str | None = None,
    def_line: int | None = None,
) -> tuple[Any, str, _Inst, dict[str, Any]]:
    """Rebuild the stored object, graft its trace under ``fr`` and remap
    its makers. Everything that can fail does so before the graft mints a
    frame (:class:`_GraftMismatch` leaves the live trace untouched).
    ``lenient`` (``steps.load``) maps an unresolvable external maker to
    ``None`` instead of refusing."""
    try:
        doc = json.loads(files["step.json"].decode("utf-8"))
        if doc.get("format") != _FORMAT or doc.get("key") != key:
            raise _GraftMismatch("step.json: format or key differs")
        kind = doc["kind"]
        tj = doc.get("trace")
        if tr is not None and not lenient:
            if tj is None or tj.get("schema") != _ts.SCHEMA_VERSION:
                raise _GraftMismatch(
                    "stored without a compatible trace (schema "
                    f"{(tj or {}).get('schema')!r} vs {_ts.SCHEMA_VERSION})"
                )
        obj = _DECODE[kind](files, doc)
    except _GraftMismatch:
        raise
    except (KeyError, ValueError, TypeError, IndexError, AttributeError) as exc:
        raise _GraftMismatch(f"unreadable entry: {exc}") from exc
    use_trace = (
        tr is not None and tj is not None and tj.get("schema") == _ts.SCHEMA_VERSION
    )
    nodes = _jdec(tj["nodes"]) if use_trace else []
    ext_ids: list[int | None] = []
    if use_trace:
        assert tr is not None
        for item in doc.get("ext", ()):
            rid: int | None = None
            if item[0] == "s":
                pi, rel = item[1], item[2]
                if pi < len(parents) and parents[pi].inst is not None:
                    inst = parents[pi].inst
                    assert inst is not None
                    if rel < len(inst.ids):
                        rid = inst.ids[rel]
            elif item[0] == "f":
                f2 = tr.by_key.get(item[1])
                rid = f2.id if f2 is not None else None
            if rid is None and not lenient:
                raise _GraftMismatch(f"cannot resolve external maker {item!r}")
            ext_ids.append(rid)
        seen: set[int | None] = set()

        def collect(m: int | None) -> int | None:
            seen.add(m)
            return m

        _map_makers(obj, kind, collect)
        for m in seen:
            if m is None or m == -1:
                continue
            if m >= len(nodes) or (m <= -2 and -2 - m >= len(ext_ids)):
                raise _GraftMismatch(f"maker code {m} outside the stored subtree")
        ids = tr.graft(fr, nodes, user_qual=user_qual, def_line=def_line)

        def dec(m: int | None) -> int | None:
            if m is None or m == -1:
                return None
            return ids[m] if m >= 0 else ext_ids[-2 - m]

        _map_makers(obj, kind, dec)
    else:
        ids = []
        _map_makers(obj, kind, lambda _m: None)
    out = _finalize(obj, kind, key, doc["name"])
    inst = _make_inst(out, kind, key, doc["name"], ids)
    _stamp(out, inst)
    return out, kind, inst, doc


# ── the decorator ────────────────────────────────────────────────────────


def _src_sha(fn: Callable[..., Any]) -> str:
    try:
        src = inspect.getsource(fn)
    except (OSError, TypeError):
        code = fn.__code__
        src = repr((code.co_code.hex(), code.co_consts, code.co_names))
    return _sha(src)


def _warn(msg: str) -> None:
    warnings.warn(f"steps: {msg}", UserWarning, stacklevel=3)


def step(fn: Callable[..., Any] | None = None, *, name: str | None = None) -> Any:
    """Cache a pure build function (see the module docstring). ``name``
    overrides ``fn.__name__`` (the key and the ``step.<name>`` frame)."""

    def deco(f: Callable[..., Any]) -> Callable[..., Any]:
        sname = name or f.__name__
        sig = inspect.signature(f)
        src_sha = _src_sha(f)
        qual = f"step.{sname}"
        stem = os.path.splitext(os.path.basename(f.__code__.co_filename))[0]
        user_qual = f"{stem}.{f.__code__.co_qualname}"
        def_line = f.__code__.co_firstlineno
        argnames = _trace._argnames(f)

        @functools.wraps(f)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return _call(
                f,
                sname,
                qual,
                sig,
                src_sha,
                user_qual,
                def_line,
                argnames,
                args,
                kwargs,
            )

        wrapper.__dict__["__step__"] = {"name": sname, "src_sha": src_sha}
        return wrapper

    return deco(fn) if fn is not None else deco


def _call(
    f: Callable[..., Any],
    sname: str,
    qual: str,
    sig: inspect.Signature,
    src_sha: str,
    user_qual: str,
    def_line: int,
    argnames: tuple[str, ...],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> Any:
    bound = sig.bind(*args, **kwargs)
    bound.apply_defaults()
    parents: list[_Par] = []
    seen: dict[int, int] = {}
    params = {n: _pcanon(v, parents, seen) for n, v in bound.arguments.items()}
    key = _sha(
        _dumps([sname, params, [p.key for p in parents], _pg.__version__, src_sha])
    )
    tr = _trace.current()
    be = _backend()
    rec: dict[str, Any] | None = None
    fr = None
    if tr is not None:
        occ = sum(1 for r in tr.steps_log if r["name"] == sname)
        rec = {
            "name": sname,
            "key": key,
            "occ": occ,
            "hit": None,
            "wall_s": 0.0,
            "saved_s": 0.0,
        }
        tr.steps_log.append(rec)
        fr = tr._enter(
            qual,
            "core",
            _trace._call_args(argnames, args, kwargs),
            sys._getframe(2),
            sys._getframe(1),
        )
    t0 = time.perf_counter()
    try:
        files = (
            be.get(key)
            if be is not None and not os.environ.get("PRECIS_STEPS_FRESH")
            else None
        )
        if files is not None:
            try:
                result, _kind, _inst, doc = _materialise(
                    files,
                    key,
                    tr=tr,
                    fr=fr,
                    parents=parents,
                    lenient=False,
                    user_qual=user_qual,
                    def_line=def_line,
                )
            except _GraftMismatch:
                files = None  # a miss: rebuild below and overwrite the entry
            else:
                assert be is not None
                be.bump(key)
                if fr is not None:
                    fr.step = {"key": key, "hit": True}
                if rec is not None:
                    rec.update(
                        hit=True,
                        saved_s=float(doc.get("wall_s", 0.0)),
                        wall_s=time.perf_counter() - t0,
                    )
                return result
        if fr is not None:
            fr.step = {"key": key, "hit": False}
        t1 = time.perf_counter()
        result = f(*args, **kwargs)
        wall = time.perf_counter() - t1
        if tr is not None and fr is not None:
            tr._finish(fr)
            fr.ret = _trace._arg_digest(result)
        kind = _kind_of(result)
        out = result
        if kind is None:
            _warn(
                f"{sname}: returned a {type(result).__name__}, not a Geom, "
                "SceneSpec, DSL text or Board — not cached"
            )
        elif be is not None:
            try:
                ids = _store(
                    be,
                    key=key,
                    name=sname,
                    kind=kind,
                    obj=result,
                    params=params,
                    parents=parents,
                    wall_s=wall,
                    src_sha=src_sha,
                    tr=tr,
                    fr=fr,
                )
            except _Unstorable as exc:
                _warn(f"{sname}: output not cacheable — {exc}")
            except OSError as exc:
                _warn(f"{sname}: cache write failed ({exc}) — not cached")
            else:
                out = _finalize(result, kind, key, sname)
                _stamp(out, _make_inst(out, kind, key, sname, ids))
        if rec is not None:
            rec.update(hit=False, wall_s=wall)
        return out
    except BaseException as exc:
        if tr is not None:
            if rec is not None and rec["hit"] is None:
                with contextlib.suppress(ValueError):
                    tr.steps_log.remove(rec)
            if fr is not None and exc not in tr._raised:
                fr.error = type(exc).__name__
                tr._raised.add(exc)
        raise
    finally:
        if tr is not None and fr is not None:
            tr._leave(fr)


# ── reading the cache ────────────────────────────────────────────────────


def _resolve_key(be: _Mem | _Disk, key: str) -> str:
    if len(key) == 64 and be.get(key) is not None:
        return key
    cands = [k for k, _u in be.uses() if k.startswith(key)]
    if len(key) < 6 or len(cands) != 1:
        raise BadInput(
            f"steps.load: key {key!r} matches {len(cands)} cached steps",
            next="pass the full key from steps.ls(), or a unique prefix "
            "of at least 6 characters",
        )
    return cands[0]


def load(key: str) -> Any:
    """The object cached under ``key`` (a full key or a unique prefix of
    ``steps.ls()``). With a live tracer the stored subtree is grafted
    under a ``step.load`` frame and makers remap as for a hit (an
    unresolvable external maker becomes ``None``); without one every
    maker is ``None``. The result is a valid input to further steps."""
    be = _backend()
    if be is None:
        raise BadInput("steps.load: the step cache is off", next="configure()")
    full = _resolve_key(be, key)
    files = be.get(full)
    if files is None:
        raise BadInput(f"steps.load: no cached step {key!r}")
    tr = _trace.current()
    fr = None
    if tr is not None:
        fr = tr._enter(
            "step.load", "core", {"key": full[:16]}, sys._getframe(1), sys._getframe(0)
        )
    try:
        try:
            obj, _kind, _inst, _doc = _materialise(
                files, full, tr=tr, fr=fr, parents=[], lenient=True
            )
        except _GraftMismatch as exc:
            raise BadInput(f"steps.load: {full[:12]} is unreadable — {exc}") from exc
        be.bump(full)
        if fr is not None:
            fr.step = {"key": full, "hit": True}
        return obj
    finally:
        if tr is not None and fr is not None:
            tr._leave(fr)


def ls(name: str | None = None) -> list[dict[str, Any]]:
    """The cached steps, oldest use first: ``{key, name, params_digest,
    wall_s, hits, bytes, version, src_sha}`` (``name`` filters)."""
    be = _backend()
    if be is None:
        return []
    rows = []
    for k, u in be.uses():
        if name is not None and u.get("name") != name:
            continue
        rows.append(
            {
                "key": k,
                "name": u.get("name"),
                "params_digest": u.get("params_digest"),
                "wall_s": u.get("wall_s"),
                "hits": u.get("hits", 0),
                "bytes": u.get("bytes", 0),
                "version": u.get("version"),
                "src_sha": u.get("src_sha"),
                "_used": u.get("used_ns", 0),
            }
        )
    rows.sort(key=lambda r: r["_used"])
    for r in rows:
        del r["_used"]
    return rows


def clear() -> int:
    """Drop every cached step (this cache only); returns how many."""
    be = _backend()
    return be.clear() if be is not None else 0


def recipe(path: str | os.PathLike[str] = "recipe.json") -> dict[str, Any]:
    """The run's recipe: ``recipe.json`` (a JSON object) beside the calling
    script, else in the working directory. A script reads its knobs from
    here so editing one number re-runs from the first step that uses it."""
    p = Path(path)
    cands = [p]
    if not p.is_absolute():
        caller = Path(sys._getframe(1).f_code.co_filename)
        cands = [caller.parent / p, Path.cwd() / p]
    for c in cands:
        if c.is_file():
            try:
                data = json.loads(c.read_text(encoding="utf-8"))
            except ValueError as exc:
                raise BadInput(f"steps.recipe: {c} is not valid JSON: {exc}") from exc
            if not isinstance(data, dict):
                raise BadInput(f"steps.recipe: {c} must hold a JSON object")
            return data
    raise BadInput(
        f"steps.recipe: {path} not found (looked in "
        f"{', '.join(str(c.parent) for c in cands)})",
        next="write recipe.json next to main.py",
    )


__all__ = ["clear", "configure", "load", "ls", "recipe", "step"]
