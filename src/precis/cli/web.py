"""``precis web`` — launch the precis-web FastAPI service (uvicorn).

The web surface (Tasks / Papers / Console / Status) lives in the
optional ``precis_web`` package, pulled in by the ``precis-mcp[web]``
extra. This subcommand imports it lazily so a base install without
FastAPI keeps the rest of the CLI working; a missing extra surfaces
as a clear install hint rather than an ImportError traceback.

The process presents ``PRECIS_SOURCE=web:reto`` to the handler guards
(owner authority over the todo tree) unless the operator overrides it.
No auth in cut 1 — bind loopback and reach it over Tailscale.
"""

from __future__ import annotations

import argparse
import os
import sys


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
        help="Caller identity for handler guards (default web:reto).",
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
    os.environ.setdefault("PRECIS_SOURCE", args.source or "web:reto")

    cfg = WebConfig.from_env()
    host = args.host or cfg.host
    port = args.port or cfg.port

    app = create_app(web_config=cfg)
    print(f"precis web: serving on http://{host}:{port}", file=sys.stderr)
    uvicorn.run(app, host=host, port=port)
