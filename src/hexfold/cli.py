"""hexfold CLI: check / build / canon / xyz.

Exit codes: 0 clean, 1 findings at ERROR, 2 parse failure.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .build import build
from .canon import canonical_json
from .check import check
from .report import ParseError, Profile, Severity
from .stick import stick


def _read(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def _profile(strict: bool) -> Profile:
    return Profile.STRICT if strict else Profile.DEFAULT


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

    args = ap.parse_args(argv)
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
