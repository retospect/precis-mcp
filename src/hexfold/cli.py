"""hexfold CLI: check / build / canon / xyz.

Exit codes: 0 clean, 1 findings at ERROR, 2 parse failure.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from . import __version__
from .build import build
from .canon import canonical_json
from .check import check
from .options import Wish, options
from .report import ParseError, Profile, Severity
from .stick import stick

_MEASURE_RE = re.compile(r"^tube\(\s*(\d+)\s*,\s*(\d+)\s*\)$")


def _read(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def _profile(strict: bool) -> Profile:
    return Profile.STRICT if strict else Profile.DEFAULT


def _catalogue_cmd(args: argparse.Namespace) -> int:
    """``hexfold catalogue`` (SPEC §26, slice 6): list -- and optionally
    measure into -- an environment catalogue.  ``file`` is a JSON dump
    (:meth:`hexfold.catalogue.MemoryStore.to_json`), loaded if it exists
    and written back after a ``--measure``; with no ``file`` the store is
    seed rows only, in memory for this run."""
    from .catalogue import (
        BulkCell,
        CatalogueError,
        EdgeMotif,
        MemoryStore,
        measure_environment,
    )

    if args.file:
        try:
            store = MemoryStore.from_json(_read(args.file))
        except FileNotFoundError:
            store = MemoryStore.seeded()
        except OSError as e:
            # a real I/O error on an *existing* file (permissions, a
            # transient mount issue): must not fall back to seed rows --
            # a subsequent --measure write would silently overwrite
            # whatever the file actually held with seed-only data.
            print(f"error: {e}", file=sys.stderr)
            return 2
        except CatalogueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
    else:
        store = MemoryStore.seeded()

    if args.measure:
        m = _MEASURE_RE.match(args.measure.strip())
        if m is None:
            print(
                f"error: cannot parse --measure {args.measure!r}; want 'tube(n,m)'",
                file=sys.stderr,
            )
            return 2
        nm = (int(m[1]), int(m[2]))
        try:
            edge, bulk = measure_environment(nm, rung=args.rung)
        except CatalogueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        store.put(edge, force=args.force)
        store.put(bulk, force=args.force)
        if args.file:
            Path(args.file).write_text(store.to_json(), encoding="utf-8")

    if args.json:
        print(store.to_json())
        return 0
    for row in store.rows():
        k = row.key
        kind = (
            "edge"
            if isinstance(row, EdgeMotif)
            else "bulk"
            if isinstance(row, BulkCell)
            else "seam"
        )
        ident = k.rim_type if k.rim_type is not None else (k.kind or "")
        n_disp = k.N if k.N is not None else "*"
        print(
            f"{k.zone:5} {kind:4} rung={k.rung:6} {ident:2} "
            f"N={n_disp} source={row.source}"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="hexfold")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("check")
    p.add_argument("file")
    p.add_argument("--json", action="store_true")
    p.add_argument("--level", choices=["info", "warn", "error"], default="info")
    p.add_argument("--strict", action="store_true")
    p.add_argument("--geometry", action="store_true")

    p = sub.add_parser("build")
    p.add_argument("file")
    p.add_argument("-o", "--out")
    p.add_argument(
        "--stick",
        action="store_true",
        help="include stick-model coordinates (generated.fidelity=stick)",
    )

    p = sub.add_parser("canon")
    p.add_argument("file")

    p = sub.add_parser("xyz")
    p.add_argument("file")
    p.add_argument("-o", "--out")

    p = sub.add_parser("view")
    p.add_argument("file")
    p.add_argument("-o", "--out")
    p.add_argument("--no-h", action="store_true", help="hide termination atoms")

    p = sub.add_parser(
        "options",
        help="realisable values near a wish at one fit site (SPEC 25.3)",
    )
    p.add_argument("file")
    p.add_argument("handle", help="'<inst>.len' or '<src port>.k'")
    p.add_argument("--target", type=float, help="periods (len) or steps (k)")
    p.add_argument("--band", type=float, help="half-width, same unit as --target")
    p.add_argument("--target-A", dest="target_a", type=float, help="len only")
    p.add_argument("--band-A", dest="band_a", type=float, help="len only")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser(
        "catalogue",
        help="environment-keyed seam/bulk motifs -- list, or measure one in (SPEC §26)",
    )
    p.add_argument(
        "file", nargs="?", help="catalogue JSON dump to load/save (default: seed rows)"
    )
    p.add_argument("--measure", help="measure one environment, e.g. 'tube(8,0)'")
    p.add_argument("--rung", default="stick", choices=["stick", "geo"])
    p.add_argument("--force", action="store_true", help="overwrite an existing row")
    p.add_argument("--json", action="store_true")

    args = ap.parse_args(argv)
    if args.cmd == "catalogue":
        return _catalogue_cmd(args)
    try:
        text = _read(args.file)
    except OSError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if args.cmd == "check":
        try:
            rep = check(text, profile=_profile(args.strict), geometry=args.geometry)
        except ParseError as e:
            print(f"parse error: {e}", file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(rep.to_dict(), sort_keys=True))
        else:
            lvl = {
                "info": Severity.INFO,
                "warn": Severity.WARN,
                "error": Severity.ERROR,
            }[args.level]
            for f in rep.sorted().findings:
                if f.severity >= lvl:
                    print(
                        f"{f.severity.name:5} {f.code} "
                        f"{f.where + ' ' if f.where else ''}{f.message}"
                    )
        return 0 if rep.ok else 1

    try:
        if args.cmd == "build":
            net = build(text, profile=_profile(False), strict=False)
            out = net.to_json(fidelity="stick" if args.stick else "check")
            if args.out:
                Path(args.out).write_text(out, encoding="utf-8")
            else:
                sys.stdout.write(out)
            return 0 if net.report.ok else 1
        if args.cmd == "canon":
            sys.stdout.write(canonical_json(text))
            return 0
        if args.cmd == "xyz":
            net = build(text, profile=_profile(False), strict=False)
            coords = stick(net)
            seed = net.seed_kind
            lines = [
                str(len(net.atoms)),
                f"fidelity=stick seed={seed} lib=hexfold@{__version__}",
            ]
            for a, c in zip(net.atoms, coords, strict=True):
                lines.append(f"{a.element:2} {c[0]: .6f} {c[1]: .6f} {c[2]: .6f}")
            out = "\n".join(lines) + "\n"
            if args.out:
                Path(args.out).write_text(out, encoding="utf-8")
            else:
                sys.stdout.write(out)
            return 0
        if args.cmd == "options":
            wish = Wish(
                target=args.target,
                band=args.band,
                target_A=args.target_a,
                band_A=args.band_a,
            )
            try:
                res = options(text, args.handle, wish)
            except ValueError as e:
                print(f"error: {e}", file=sys.stderr)
                return 2
            if args.json:
                print(json.dumps(res.to_dict(), sort_keys=True))
                return 0
            unit = res.unit
            print(
                f"{res.handle}: applied {res.applied} {unit}; "
                f"{len(res.options)} option(s), {len(res.rejected)} rejected"
            )
            for o in res.options:
                extra = f" = {o.value_A:.2f} A" if o.value_A is not None else ""
                print(
                    f"  {o.value:>4}{extra}  distance {o.distance:g}  "
                    f"seam {o.cost[0]:g} residual {o.cost[1]:g}"
                )
            for o in res.rejected:
                print(f"  {o.value:>4}  rejected: {', '.join(o.errors)}")
            return 0
        if args.cmd == "view":
            try:
                import matplotlib  # noqa: F401
            except ImportError:
                print(
                    "error: view needs matplotlib; install hexfold[view]",
                    file=sys.stderr,
                )
                return 2
            from .view import render

            net = build(text, profile=_profile(False), strict=False)
            render(net, stick(net), args.out, not args.no_h)
            return 0 if net.report.ok else 1
    except ParseError as e:
        print(f"parse error: {e}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
