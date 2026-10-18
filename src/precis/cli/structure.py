"""``precis structure`` — structure-kind maintenance.

``index-bundle <source> <path>`` walks a downloaded QMOF or CoRE MOF bundle
(a ``.zip`` or an extracted directory) once and writes the name -> offset
index that ``get(kind='structure', args={'source': ..., 'config_id': ...})``
reads one record through. The index lands in ``PRECIS_STRUCT_BUNDLE_DIR``
(``--index-dir`` overrides). Downloading the bundle is an ops step; this
command only reads it.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def add_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register the ``structure`` subcommand (``index-bundle``)."""
    from precis.structure.importers.mof import CIF_ADAPTERS

    p = sub.add_parser(
        "structure",
        help="Structure-kind maintenance: index a downloaded MOF bundle.",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    psub = p.add_subparsers(dest="structure_cmd", required=True)
    ib = psub.add_parser(
        "index-bundle",
        help="Write the name -> offset index for a downloaded QMOF / CoRE MOF bundle.",
    )
    ib.add_argument("source", choices=CIF_ADAPTERS, help="Which bundle this is.")
    ib.add_argument("path", type=Path, help="The bundle: a .zip or an extracted dir.")
    ib.add_argument(
        "--index-dir",
        type=Path,
        default=None,
        help="Where to write <source>.json (default: PRECIS_STRUCT_BUNDLE_DIR).",
    )
    return p


def run(args: argparse.Namespace) -> None:
    """Dispatch ``precis structure <cmd>``."""
    if args.structure_cmd == "index-bundle":
        _index_bundle(args)


def _index_bundle(args: argparse.Namespace) -> None:
    from precis.config import struct_bundle_index_dir
    from precis.structure.importers import bundle_index

    index_dir = args.index_dir or struct_bundle_index_dir()
    try:
        doc = bundle_index.build_index(args.source, args.path)
    except bundle_index.BundleIndexError as exc:
        print(f"error: {exc}\nnext: {exc.next}", file=sys.stderr)
        raise SystemExit(2) from exc
    entries = doc["entries"]
    if not entries:
        print(
            f"error: no .cif members found in {args.path}; nothing written",
            file=sys.stderr,
        )
        raise SystemExit(2)
    out = bundle_index.write_index(doc, index_dir)
    with_energy = sum(1 for e in entries.values() if "energy" in e)
    print(
        f"indexed {len(entries)} {args.source} record(s) "
        f"({with_energy} with an energy) -> {out}"
    )
