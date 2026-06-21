"""``precis draft …`` — operations on the ``draft`` kind (ADR 0033).

Currently one subcommand:

* ``precis draft export <slug> [--out DIR]`` — render a draft into a
  compilable LaTeX project (``main.tex`` + ``refs.bib`` + a copy of the
  standard ``preamble.tex``). The output is **disposable** — re-export
  from the draft, never hand-edit. Compile with ``latexmk -pdf main.tex``
  (biber + makeglossaries run automatically).

The compile + LLM-repair loop and the Word/pandoc path land as later
subcommands of ``precis draft``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from precis.cli._common import resolve_dsn
from precis.export.latex import export_draft
from precis.store import Store


def add_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("draft", help="Operate on draft documents (export, …).")
    dsub = p.add_subparsers(dest="draft_cmd", required=True)

    ex = dsub.add_parser(
        "export",
        help="Render a draft to a compilable LaTeX project.",
        description=(
            "Resolve a draft's chunks into main.tex + refs.bib + a copy "
            "of the standard preamble. Cross-refs (¶), citations (§/paper:), "
            "and defined abbreviations are all resolved automatically. "
            "Output is disposable — re-export, don't hand-edit."
        ),
    )
    ex.add_argument("slug", help="Draft slug or numeric ref id.")
    ex.add_argument(
        "--out",
        default=None,
        help="Output directory for the LaTeX project. Default: ./export/<slug>/.",
    )
    ex.add_argument(
        "--format",
        choices=("tex",),
        default="tex",
        help="Export format. Only 'tex' so far (docx via pandoc is a later increment).",
    )
    ex.add_argument(
        "--database-url",
        default=None,
        help="Override PRECIS_DATABASE_URL.",
    )


def run(args: argparse.Namespace) -> None:
    if args.draft_cmd == "export":
        _run_export(args)
        return
    print(f"draft: unknown subcommand {args.draft_cmd!r}", file=sys.stderr)
    sys.exit(2)


def _run_export(args: argparse.Namespace) -> None:
    dsn = resolve_dsn(args.database_url)
    store = Store.connect(dsn)
    try:
        key: int | str = int(args.slug) if str(args.slug).isdigit() else args.slug
        ref = store.get_ref(kind="draft", id=key)
        if ref is None:
            print(f"draft export: no draft {args.slug!r}", file=sys.stderr)
            sys.exit(2)
        out = Path(args.out) if args.out else Path("export") / str(ref.slug or ref.id)
        result = export_draft(store, ref, target_dir=out)
    finally:
        store.close()

    for w in result.warnings:
        print(f"draft export: {w}", file=sys.stderr)
    print(
        f"draft export: wrote {result.main_tex}, {result.bib} "
        f"({len(result.cited_slugs)} citation(s), "
        f"{len(result.acronyms)} acronym(s)). "
        f"Compile: latexmk -pdf -cd {result.main_tex}",
        file=sys.stderr,
    )


__all__ = ["add_parser", "run"]
