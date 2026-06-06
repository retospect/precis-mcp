"""``precis serve-embeddings`` — run the HTTP embedding service.

The server side of ADR 0020. Loads a real (or mock) embedder in-process
and serves it over the wire schema in :mod:`precis.embedder_wire`, so
torch-free ``serve`` / ``worker`` processes can embed remotely via
``PRECIS_EMBEDDER=remote`` + ``PRECIS_EMBEDDER_URL``.

On macOS this runs natively (uv venv + launchd) so the model reaches
Metal/MPS, which a container cannot; on Linux it runs in the CUDA
``embedder`` image. Same command, both places.

Examples::

    precis serve-embeddings                       # bge-m3 on 127.0.0.1:8181
    precis serve-embeddings --port 9000
    precis serve-embeddings --embedder mock       # CI / smoke, no weights
    precis serve-embeddings --revision $HF_SHA    # pin the served revision
"""

from __future__ import annotations

import argparse
import os

from precis.embedder_wire import DEFAULT_PORT


def add_parser(sub: argparse._SubParsersAction) -> None:
    """Register the ``precis serve-embeddings`` subcommand."""
    p = sub.add_parser(
        "serve-embeddings",
        help="Run the HTTP embedding service (server side of the remote embedder).",
        description=(
            "Load an embedder in-process and serve it over HTTP "
            "(/healthz, /readyz, /model, /embed, /metrics) for "
            "torch-free serve/worker processes to call via "
            "PRECIS_EMBEDDER=remote. See ADR 0020."
        ),
    )
    p.add_argument(
        "--host",
        default=os.environ.get("PRECIS_EMBEDDER_HOST", "127.0.0.1"),
        help="Bind address (default: 127.0.0.1 — loopback only).",
    )
    p.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PRECIS_EMBEDDER_PORT", str(DEFAULT_PORT))),
        help=f"Bind port (default: {DEFAULT_PORT}).",
    )
    p.add_argument(
        "--embedder",
        default=os.environ.get("PRECIS_EMBEDDER_BACKEND", "bge-m3"),
        choices=["bge-m3", "mock"],
        help="Which model to load and serve (default: bge-m3). "
        "'mock' serves the deterministic test embedder (no weights).",
    )
    p.add_argument(
        "--revision",
        default=os.environ.get("PRECIS_EMBEDDER_REVISION"),
        help="Model revision (HF commit SHA) reported on /model. Pinned "
        "by deployment; surfaced so the client's boundary check can "
        "detect drift.",
    )
    p.add_argument(
        "--max-inflight",
        type=int,
        default=int(os.environ.get("PRECIS_EMBEDDER_MAX_INFLIGHT", "4")),
        help="Concurrent embed requests admitted before returning 429 "
        "(default: 4).",
    )
    p.add_argument(
        "--no-warm",
        action="store_true",
        help="Skip background warmup; /readyz reports ready immediately. "
        "Mainly for the mock embedder in tests.",
    )


def run(args: argparse.Namespace) -> None:
    """Build the embedder and run the service (blocking)."""
    # Imported lazily: `serve` pulls torch via BgeM3Embedder, and we
    # don't want `precis --help` to drag it in.
    from precis.embedder import make_embedder
    from precis.embedder_service import serve

    embedder = make_embedder(args.embedder)
    serve(
        embedder,
        host=args.host,
        port=args.port,
        revision=args.revision,
        max_inflight=args.max_inflight,
        warm=not args.no_warm,
    )
