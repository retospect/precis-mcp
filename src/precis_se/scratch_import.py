"""``python -m precis_se.scratch_import`` — import a scratch ``out/`` run
into a dev ``se`` design (gr514239).

The workspace dev loop writes ``scratch/out/<run>/`` (``precis_ws.out``)
whose ``manifest.json`` + ``trace.json`` used to need a throwaway pytest
to inspect through ``get`` views. This command imports such a tree into
the dev DB's ``se`` kind in one step::

    scripts/scratch-import scratch/out/foo
    # then: get(kind='se', id='foo', view='trace')

Re-running against the same ``--design`` re-imports into it — the
replace/retire echo is the dogfood loop. **Dev DB only**: the ``--dsn``
(default ``$PRECIS_DATABASE_URL``, which ``scripts/dev`` already points
at ``precis_dev``) must name ``precis_dev`` or a ``precis_test*`` clone —
anything else exits 2 before a connection is opened.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from psycopg.conninfo import conninfo_to_dict

from precis.dispatch import Hub
from precis.store import Store
from precis.workers._mint import mint_background_job
from precis_se.handler import SeHandler
from precis_se.workspace_import import ImportResult, import_workspace_run


def import_scratch_run(
    store: Store,
    *,
    out_dir: str | Path,
    design: str,
    workspace: str = "scratch",
) -> ImportResult:
    """Import ``out_dir`` into the ``se`` design ``design`` (created when
    absent — an existing design takes the replace/retire path) under a
    fresh ``job`` ref, and return the import's echo data."""
    if store.get_ref(kind="se", id=design) is None:
        store.insert_ref(kind="se", slug=design, title=design, meta={})
    # The run record is a terminal ``succeeded`` job (never claimed):
    # minted through the origin door so the job-origin contract holds.
    with store.atomic() as conn:
        job = mint_background_job(
            store,
            conn=conn,
            job_type="ws_run",
            executor="job_inproc",
            title=f"scratch import {design}",
            status="succeeded",
        )
    return import_workspace_run(
        store,
        workspace=workspace,
        design=design,
        out_dir=out_dir,
        run_job=int(job.id),
    )


def _view_args(pairs: list[str]) -> dict[str, Any] | None:
    """``--arg k=json`` repeats → the ``se.get`` args dict; a value that
    is not JSON is taken literally (``atom=p/s1/s1`` needs no quotes)."""
    args: dict[str, Any] = {}
    for kv in pairs:
        k, eq, v = kv.partition("=")
        if not eq or not k:
            return None
        try:
            args[k] = json.loads(v)
        except json.JSONDecodeError:
            args[k] = v
    return args


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="scratch-import",
        description="import a scratch out/<run> tree into a dev se design",
    )
    p.add_argument("out_dir", help="the run's out/ directory")
    p.add_argument(
        "--design",
        default=None,
        help="se design slug to import into (default: out_dir basename)",
    )
    p.add_argument("--workspace", default="scratch", help="workspace slug")
    p.add_argument(
        "--dsn",
        default=None,
        help="dev DB DSN (default: $PRECIS_DATABASE_URL)",
    )
    p.add_argument("--view", default=None, help="print this se view after import")
    p.add_argument(
        "--arg",
        action="append",
        default=[],
        metavar="K=JSON",
        help="se.get arg (repeatable; non-JSON values pass through verbatim)",
    )
    ns = p.parse_args(argv)

    design = ns.design or Path(ns.out_dir).name
    dsn = ns.dsn or os.environ.get("PRECIS_DATABASE_URL", "")
    if not dsn:
        print(
            "scratch-import: no --dsn and PRECIS_DATABASE_URL unset — "
            "run via scripts/scratch-import (scripts/dev sets the dev DSN)",
            file=sys.stderr,
        )
        return 2
    # Dev-DB guard BEFORE Store.connect — this command must never touch
    # prod (gr514239).
    try:
        dbname = str(conninfo_to_dict(dsn).get("dbname") or "")
    except Exception as exc:
        print(f"scratch-import: bad DSN: {exc}", file=sys.stderr)
        return 2
    if dbname != "precis_dev" and not dbname.startswith("precis_test"):
        print(
            f"scratch-import is dev-DB only: dbname {dbname!r} is not "
            "precis_dev / precis_test_* — run via scripts/scratch-import "
            "(scripts/dev sets the dev DSN)",
            file=sys.stderr,
        )
        return 2
    view_args = _view_args(ns.arg)
    if view_args is None:
        print(
            f"scratch-import: --arg wants K=JSON; got {ns.arg!r}",
            file=sys.stderr,
        )
        return 2

    store = Store.connect(dsn)
    body = None
    try:
        res = import_scratch_run(
            store, out_dir=ns.out_dir, design=design, workspace=ns.workspace
        )
        if ns.view is not None:
            body = (
                SeHandler(hub=Hub(store=store))
                .get(id=design, view=ns.view, args=view_args)
                .body
            )
    finally:
        store.close()
    print(res.echo())
    if body is not None:
        print(body)
    print(f"get(kind='se', id='{design}', view='trace')")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
