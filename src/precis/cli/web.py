"""``precis web`` — launch the precis-web FastAPI service (uvicorn).

The web surface (Todo / Papers / Console / Status) lives in the
optional ``precis_web`` package, pulled in by the ``precis-mcp[web]``
extra. This subcommand imports it lazily so a base install without
FastAPI keeps the rest of the CLI working; a missing extra surfaces
as a clear install hint rather than an ImportError traceback.

The process presents ``PRECIS_SOURCE=web:owner`` to the handler guards
(owner authority over the todo tree) unless the operator overrides it.
No auth in cut 1 — bind loopback and reach it over Tailscale.
"""

from __future__ import annotations

import argparse
import copy
import os
import sys
from typing import Any


def _log_config() -> dict[str, Any]:
    """uvicorn's logging config with a leading UTC timestamp per line.

    Stock uvicorn writes bare ``INFO:``/access lines — ``precis-web.log``
    carried no timestamps, which made the 2026-11-19 security audit's
    log review depend on file order (gr735107). Deepcopy the stock
    dictConfig and prepend ``%(asctime)sZ`` to each formatter rather than
    hand-writing one, so a uvicorn upgrade keeps its own handler and
    logger structure. The ``precis`` entry point already pins
    ``logging.Formatter.converter`` to UTC
    (:func:`precis.utils.utc_logging.force_utc_timestamps`), so the stamp
    renders in the fleet's zone — the ``Z`` says so on the line.
    """
    from uvicorn.config import LOGGING_CONFIG

    config = copy.deepcopy(LOGGING_CONFIG)
    for formatter in config["formatters"].values():
        formatter["fmt"] = f"%(asctime)sZ {formatter['fmt']}"
    return config


def add_parser(sub: argparse._SubParsersAction) -> None:
    """Register the ``precis web`` subcommand."""
    p = sub.add_parser(
        "web",
        help="Run the precis web UI (FastAPI; requires the [web] extra).",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--host", default=None, help="Bind host (default 127.0.0.1).")
    p.add_argument("--port", type=int, default=None, help="Bind port (default 9100).")
    p.add_argument(
        "--corpus-dir",
        default=None,
        help="PDF corpus root for the paper viewer (default ~/work/corpus).",
    )
    p.add_argument(
        "--source",
        default=None,
        help="Caller identity for handler guards (default web:owner).",
    )
    p.add_argument(
        "--migrate",
        action="store_true",
        help="Apply pending DB migrations at startup instead of refusing "
        "when the schema is behind (same as PRECIS_AUTO_MIGRATE=1).",
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> None:
    """Build the app and serve it with uvicorn."""
    try:
        import uvicorn

        from precis_web.app import create_app
        from precis_web.config import WebConfig
    except ImportError as exc:  # pragma: no cover — exercised by install state
        print(
            "precis web needs the optional web extra:\n"
            "    uv pip install 'precis-mcp[web]'\n"
            f"(import failed: {exc})",
            file=sys.stderr,
        )
        sys.exit(1)

    # Env feeds both the WebConfig and the runtime/guards. Set before
    # create_app so the lifespan-built runtime and the guard reads see
    # the same values.
    if args.corpus_dir:
        os.environ["PRECIS_CORPUS_DIR"] = args.corpus_dir
    os.environ.setdefault("PRECIS_SOURCE", args.source or "web:owner")
    if args.migrate:
        os.environ["PRECIS_AUTO_MIGRATE"] = "1"

    cfg = WebConfig.from_env()
    host = args.host or cfg.host
    port = args.port or cfg.port

    app = create_app(web_config=cfg)
    print(f"precis web: serving on http://{host}:{port}", file=sys.stderr)
    uvicorn.run(app, host=host, port=port, log_config=_log_config())
