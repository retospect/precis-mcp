"""Text form: parser and emitter for SPEC section 13.

One statement per line, ``#`` comments, whitespace-insensitive except line
breaks.  ``x`` and ``×`` both accepted for counts; the emitter writes ``×``.

Phase-1 grammar subset: header, prov, lattice, origin, instance lines for
sheet/tube/cone/fullerene(C60), hole removal ``- pentagon@site`` /
``- hexagon@site``, defect addition ``+ ring@site[:dir]`` and glyph
addition ``+ sw@site[:dir]`` / ``+ 57@site[:dir]`` (the ``+`` defect syntax
is the conservative reading of "defect placement" the grammar leaves
unspecified), ``xN`` repeat, connect lines and ``frag`` parsed into the AST
but not executed, ``registry``, ``terminate``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .lattice import Site
from .report import ParseError

Span = tuple[int, int]


@dataclass(frozen=True)
class Hole:
    ring: int
    site: Site
    dir: int | None = None  # dir from site toward the ring centre
    source: str | None = None  # menu that generated this hole (None = authored)


@dataclass(frozen=True)
class SiteDefect:
    """An authored defect or glyph on an instance line."""

    kind: str  # ring size as str ("5","7",...) or glyph name ("sw","57")
    site: Site
    dir: int


@dataclass(frozen=True)
class Instance:
    name: str
    kind: str  # sheet | tube | cone | fullerene | cap
    params: tuple[tuple[str, str], ...]
    holes: tuple[Hole, ...] = ()
    defects: tuple[SiteDefect, ...] = ()
    repeat: int = 1
    span: Span = (0, 0)
    source: str | None = None  # menu that generated this instance


@dataclass(frozen=True)
class Connect:
    src: str
    dst: str
    verb: str  # fuse | bond | menu
    menu: str | None = None  # e.g. "7x5 @fit", "[2+2]", "DB-neck(4)"
    k: int | None = None
    order: int | None = None
    expanded: dict[str, Any] | None = None  # menu/collar expansion record
    span: Span = (0, 0)
    source: str | None = None  # menu that generated this connect (None = authored)


@dataclass(frozen=True)
class Frag:
    name: str
    kind: str
    body: str
    line: str
    span: Span = (0, 0)


@dataclass(frozen=True)
class Seam:
    """``seam <name>: A.r1 == B.r1 == C.r1 [k=<phase>] [atoms=sp2]`` (SPEC
    11.3): k >= 3 rims identified as one curve.  ``atoms`` is "sp2" in
    every 0.2 spec; "sp3" is reserved (SPEC 28) and rejected here.
    """

    name: str
    rims: tuple[str, ...]
    k: int = 0
    atoms: str = "sp2"
    span: Span = (0, 0)


@dataclass(frozen=True)
class Spec:
    version: str = "0.2"
    prov: tuple[tuple[str, str], ...] = ()
    lattice: tuple[tuple[str, str], ...] = ()
    origin: str | None = None
    instances: tuple[Instance, ...] = ()
    connects: tuple[Connect, ...] = ()
    registry: tuple[tuple[str, str, Span], ...] = ()
    terminate: tuple[tuple[str, str, Span], ...] = ()
    frags: tuple[Frag, ...] = ()
    seams: tuple[Seam, ...] = ()

    def instance(self, name: str) -> Instance | None:
        for i in self.instances:
            if i.name == name:
                return i
        return None


_SITE = r"\(\s*-?\d+\s*,\s*-?\d+\s*,\s*[ABab]\s*\)"
_IDENT = r"[A-Za-z_][\w.]*"
_KV = r"[\w.\-]+\s*=\s*[^\s#]+"

_PRIM_RE = re.compile(
    rf"^(?P<name>{_IDENT})\s*:\s*(?P<prim>{_IDENT})\s*\((?P<args>[^)]*)\)"
    rf"(?P<rest>.*)$"
)
_HOLE_RE = re.compile(
    rf"-\s*(?P<what>pentagon|hexagon|notch|path3|hex\(\s*\d+\s*\)|\d+)"
    rf"\s*@(?P<site>{_SITE})(?:\s*:\s*d?(?P<dir>\d))?"
)
_DEF_RE = re.compile(
    rf"\+\s*(?P<what>sw|57|pentagon|heptagon|square|octagon|\d+)\s*@"
    rf"(?P<site>{_SITE})(?:\s*:\s*d?(?P<dir>\d))?"
)
_REF = rf"{_IDENT}(?:[./][\w(),\-]+)*"
_CONN_RE = re.compile(
    rf"^(?P<src>{_REF})\s*--\s*(?P<verb>fuse|bond)"
    rf"(?:\{{(?P<menu>[^}}]*)\}})?\s*(?:k\s*=\s*(?P<k>\d+)\s*)?"
    rf"-->\s*(?P<dst>{_REF}(?:\s*@\s*{_REF})?)"
    rf"(?:\s+order\s*=\s*(?P<order>\d+))?\s*$"
)
_MENU_RE = re.compile(
    rf"^(?P<src>{_IDENT})\s*@\s*(?P<dst>{_IDENT})/(?P<site>{_SITE})"
    rf"(?:\s*:\s*d?(?P<dir>\d))?\s*\[(?P<menu>[^\]]+)\]\s*$"
)
_REPEAT_RE = re.compile(r"[x×]\s*(\d+)\s*$")
_SEAM_HEAD_RE = re.compile(rf"^seam\s+(?P<name>{_IDENT})\s*:\s*(?P<rest>.+)$")
_SEAM_K_RE = re.compile(r"\bk\s*=\s*(-?\d+)\b")
_SEAM_ATOMS_RE = re.compile(r"\batoms\s*=\s*(\S+)\b")


def _span(lineno: int, col: int = 1) -> Span:
    return (lineno, col)


def _err(msg: str, lineno: int, col: int = 1) -> ParseError:
    return ParseError(msg, (lineno, col))


def _parse_kv_pairs(text: str) -> tuple[tuple[str, str], ...]:
    out = []
    for tok in text.split():
        if "=" in tok:
            k, v = tok.split("=", 1)
            out.append((k.strip(), v.strip()))
        else:
            out.append(("_", tok))
    return tuple(out)


def _parse_params(argtext: str, lineno: int) -> tuple[tuple[str, str], ...]:
    out = []
    for i, part in enumerate(argtext.split(",")):
        part = part.strip()
        if not part:
            continue
        if "=" in part:
            k, v = part.split("=", 1)
            out.append((k.strip(), v.strip()))
        else:
            out.append((str(i), part))
    return tuple(out)


def parse(text: str) -> Spec:
    version = None
    prov: tuple[tuple[str, str], ...] = ()
    lattice: tuple[tuple[str, str], ...] = ()
    origin = None
    instances: list[Instance] = []
    connects: list[Connect] = []
    registry: list[tuple[str, str, Span]] = []
    terminate: list[tuple[str, str, Span]] = []
    frags: list[Frag] = []
    seams: list[Seam] = []

    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if version is None:
            m = re.fullmatch(r"hexfold\s+(\d+\.\d+)", line)
            if not m:
                raise _err("expected 'hexfold 0.2' header", lineno)
            version = m.group(1)
            if version not in ("0.1", "0.2"):
                raise _err(f"unsupported format version {version}", lineno)
            # 0.1 and 0.2 parse identically in this slice; normalise so the
            # emitter (and the JSON "hexfold" key) always writes 0.2.
            version = "0.2"
            continue
        m = _CONN_RE.match(line)
        if m:
            connects.append(
                Connect(
                    src=m["src"],
                    dst=m["dst"],
                    verb=m["verb"],
                    menu=(m["menu"].strip() if m["menu"] else None),
                    k=int(m["k"]) if m["k"] else None,
                    order=int(m["order"]) if m["order"] else None,
                    span=_span(lineno),
                )
            )
            continue
        sm = _SEAM_HEAD_RE.match(line)
        if sm:
            name = sm["name"]
            rest = sm["rest"]
            k = 0
            km = _SEAM_K_RE.search(rest)
            if km:
                k = int(km.group(1))
                rest = rest[: km.start()] + rest[km.end() :]
            atoms_val = "sp2"
            am = _SEAM_ATOMS_RE.search(rest)
            if am:
                atoms_val = am.group(1)
                rest = rest[: am.start()] + rest[am.end() :]
                if atoms_val != "sp2":
                    raise _err(
                        f"seam atoms= must be 'sp2' (sp3 is reserved), got {atoms_val!r}",
                        lineno,
                    )
            rims = [r.strip() for r in rest.split("==")]
            rims = [r for r in rims if r]
            if len(rims) == 2:
                raise _err(
                    "seam needs k>=3 rims; two rims is a 'fuse', not a 'seam'",
                    lineno,
                )
            if len(rims) < 3:
                raise _err(
                    "seam: expected 'A.r1 == B.r1 == C.r1 [k=..] [atoms=sp2]'",
                    lineno,
                )
            seams.append(Seam(name, tuple(rims), k, atoms_val, _span(lineno)))
            continue
        mm = _MENU_RE.match(line)
        if mm:
            site = mm["site"] + (f":{mm['dir']}" if mm["dir"] else "")
            connects.append(
                Connect(
                    src=mm["src"],
                    dst=f"{mm['dst']}/{site}",
                    verb="menu",
                    menu=mm["menu"].strip(),
                    span=_span(lineno),
                )
            )
            continue
        kw = re.match(rf"^({_IDENT})\s*:(.*)$", line)
        if kw:
            key, rest = kw.group(1), kw.group(2).strip()
            if key == "prov":
                prov = _parse_kv_pairs(rest)
                continue
            if key == "lattice":
                lattice = _parse_kv_pairs(rest)
                continue
            if key == "registry":
                parts = rest.split("==")
                if len(parts) != 2:
                    raise _err("registry: expected 'a == b'", lineno)
                registry.append((parts[0].strip(), parts[1].strip(), _span(lineno)))
                continue
            if key == "terminate":
                parts = rest.split("=", 1)
                if len(parts) != 2:
                    raise _err("terminate: expected 'glob = X'", lineno)
                terminate.append((parts[0].strip(), parts[1].strip(), _span(lineno)))
                continue
            if key == "frag":
                mfrag = re.match(rf"^({_IDENT})\s*=\s*(\w+)\((.*)\)\s*$", rest)
                if not mfrag:
                    raise _err("frag: expected 'name = kind(body)'", lineno)
                frags.append(
                    Frag(
                        mfrag.group(1),
                        mfrag.group(2),
                        mfrag.group(3),
                        rest,
                        _span(lineno),
                    )
                )
                continue
            # instance line
            mi = _PRIM_RE.match(line)
            if not mi:
                raise _err(f"cannot parse instance line: {line!r}", lineno)
            name = mi["name"]
            prim = mi["prim"]
            if prim not in ("sheet", "tube", "cone", "cap", "fullerene", "stack"):
                raise _err(f"unknown primitive {prim!r}", lineno, 2)
            rest2 = mi["rest"].strip()
            rep = _REPEAT_RE.search(rest2)
            repeat = 1
            if rep:
                repeat = int(rep.group(1))
                rest2 = rest2[: rep.start()].strip()
            holes: list[Hole] = []
            defects: list[SiteDefect] = []
            for mh in _HOLE_RE.finditer(rest2):
                w = mh["what"]
                hm = re.fullmatch(r"hex\(\s*(\d+)\s*\)", w)
                code = (
                    -(10 + int(hm.group(1)))
                    if hm
                    else {
                        "pentagon": 5,
                        "hexagon": 6,
                        "notch": -1,
                        "path3": -2,
                    }.get(w)
                    or int(w)
                )
                holes.append(
                    Hole(
                        code,
                        Site.parse(mh["site"]),
                        int(mh["dir"]) if mh["dir"] else None,
                    )
                )
            for md in _DEF_RE.finditer(rest2):
                defects.append(
                    SiteDefect(
                        md["what"],
                        Site.parse(md["site"]),
                        int(md["dir"] or 0),
                    )
                )
            instances.append(
                Instance(
                    name=name,
                    kind=prim,
                    params=_parse_params(mi["args"], lineno),
                    holes=tuple(holes),
                    defects=tuple(defects),
                    repeat=repeat,
                    span=_span(lineno),
                )
            )
            continue
        if kw := re.match(rf"^origin\s+({_IDENT})\s*$", line):
            origin = kw.group(1)
            continue
        raise _err(f"unrecognised statement: {line!r}", lineno)
    if version is None:
        raise ParseError("empty file: missing 'hexfold 0.2' header", (1, 1))
    return Spec(
        version=version,
        prov=prov,
        lattice=lattice,
        origin=origin,
        instances=tuple(instances),
        connects=tuple(connects),
        registry=tuple(registry),
        terminate=tuple(terminate),
        frags=tuple(frags),
        seams=tuple(seams),
    )


# --- emitter ---------------------------------------------------------------


def _fmt_params(inst: Instance) -> str:
    parts = []
    for k, v in inst.params:
        parts.append(v if k.isdigit() else f"{k}={v}")
    return ", ".join(parts)


def _hole_ring_code(raw: object) -> int:
    if isinstance(raw, str) and raw.startswith("hex("):
        return -(10 + int(raw[4:-1]))
    if raw in ("notch", "path3"):
        return {"notch": -1, "path3": -2}[raw]
    return int(str(raw))


def _hole_text(h: Hole) -> str:
    if h.ring <= -10:
        name = f"hex({-10 - h.ring})"
    else:
        name = {5: "pentagon", 6: "hexagon", -1: "notch", -2: "path3"}.get(
            h.ring, str(h.ring)
        )
    suffix = f":{h.dir}" if h.dir is not None else ""
    return f"- {name}@{h.site}{suffix}"


def _defect_text(d: SiteDefect) -> str:
    name = {"5": "pentagon", "7": "heptagon"}.get(d.kind, d.kind)
    suffix = f":{d.dir}" if d.dir else ""
    return f"+ {name}@{d.site}{suffix}"


def to_text(spec: Spec) -> str:
    """Emit the canonical-ish text form of a parsed spec.

    Authored content only: an instance, hole or connect a menu generated
    (``source`` set) is skipped, because the menu line that made it is
    emitted and re-expands on parse — emitting both would duplicate the
    neck/holes/fuses and leaves ``k=-1`` (the internal fit marker) in
    text the grammar refuses (gr345343).
    """
    lines = [f"hexfold {spec.version}"]
    if spec.prov:
        kv = " ".join(f"{k}={v}" for k, v in spec.prov if k != "_")
        lines.append(f"prov: {kv}")
    lines.append("")
    if spec.lattice:
        kv = " ".join(f"{k}={v}" for k, v in spec.lattice if k != "_")
        lines.append(f"lattice: {kv}")
        lines.append("")
    if spec.origin:
        lines.append(f"origin {spec.origin}")
    for inst in spec.instances:
        if inst.source is not None:
            continue
        line = f"{inst.name}: {inst.kind}({_fmt_params(inst)})"
        for h in inst.holes:
            if h.source is not None:
                continue
            line += f"  {_hole_text(h)}"
        for d in inst.defects:
            line += f"  {_defect_text(d)}"
        if inst.repeat != 1:
            line += f"  ×{inst.repeat}"
        lines.append(line)
    for f in spec.frags:
        lines.append(f"frag: {f.line}")
    authored = [c for c in spec.connects if c.source is None]
    if authored:
        lines.append("")
        for c in authored:
            if c.verb == "menu":
                dst, site = c.dst.split("/", 1)
                lines.append(f"{c.src} @ {dst}/{site} [{c.menu}]")
                continue
            menu = f"{{{c.menu}}}" if c.menu else ""
            k = f" k={c.k}" if c.k is not None else ""
            o = f" order={c.order}" if c.order is not None else ""
            lines.append(f"{c.src} --{c.verb}{menu}{k}--> {c.dst}{o}")
    for s in spec.seams:
        line = f"seam {s.name}: {' == '.join(s.rims)}"
        if s.k:
            line += f" k={s.k}"
        if s.atoms != "sp2":
            line += f" atoms={s.atoms}"
        lines.append(line)
    for a, b, _ in spec.registry:
        lines.append(f"registry: {a} == {b}")
    for g, x, _ in spec.terminate:
        lines.append(f"terminate: {g} = {x}")
    return "\n".join(lines) + "\n"


def spec_from_dict(d: dict) -> Spec:
    """Rebuild a Spec from the canonical-JSON spec-level fields."""
    lat = d.get("lattice", {})
    lat_kv = []
    el = lat.get("element", ["C", "C"])
    if el[0] == el[1]:
        lat_kv.append(("element", el[0]))
    else:
        lat_kv.append(("elements", f"({el[0]},{el[1]})"))
    lat_kv.append(("sigma", str(lat.get("sigma_A", "1.42"))))
    prov = tuple(sorted((k, str(v)) for k, v in d.get("prov", {}).items()))
    instances = []
    for name, idata in sorted(d.get("instances", {}).items()):
        params = tuple(sorted((k, str(v)) for k, v in idata.get("params", {}).items()))
        holes = tuple(
            Hole(
                _hole_ring_code(h["ring"]),
                Site.parse(h["site"]),
                int(h["dir"]) if h.get("dir") is not None else None,
                source=h.get("source"),
            )
            for h in idata.get("holes", [])
        )
        defects = tuple(
            SiteDefect(str(df["kind"]), Site.parse(df["site"]), int(df["dir"]))
            for df in idata.get("defects", [])
        )
        instances.append(
            Instance(
                name=name,
                kind=idata["kind"],
                params=params,
                holes=holes,
                defects=defects,
                repeat=int(idata.get("repeat", 1)),
                source=idata.get("source"),
            )
        )
    connects = tuple(
        Connect(
            src=c["src"],
            dst=c["dst"],
            verb=c["verb"],
            menu=c.get("menu"),
            k=c.get("k"),
            order=c.get("order"),
            expanded=c.get("expanded"),
            source=c.get("source"),
        )
        for c in d.get("connects", [])
    )
    frags = tuple(
        Frag(
            name,
            "smiles",
            str(fd.get("smiles", "")),
            f"{name} = smiles({fd.get('smiles', '')})",
        )
        for name, fd in sorted(d.get("frags", {}).items())
    )
    registry = tuple((str(r[0]), str(r[1]), (0, 0)) for r in d.get("registry", []))
    terminate = tuple((str(t[0]), str(t[1]), (0, 0)) for t in d.get("terminate", []))
    seams = tuple(
        Seam(
            str(s["name"]),
            tuple(str(r) for r in s["rims"]),
            int(s.get("k", 0)),
            str(s.get("atoms", "sp2")),
        )
        for s in d.get("seams", [])
    )
    return Spec(
        version=d.get("hexfold", "0.2"),
        prov=prov,
        lattice=tuple(lat_kv),
        origin=d.get("origin"),
        instances=tuple(instances),
        connects=connects,
        registry=registry,
        terminate=terminate,
        frags=frags,
        seams=seams,
    )
