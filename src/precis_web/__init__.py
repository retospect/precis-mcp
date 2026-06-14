"""precis_web — the cluster web surface for precis-mcp.

A FastAPI service that imports the ``precis`` package directly and
renders server-side (Jinja + HTMX). It exposes four tabs over the
Tailscale LAN (no auth in cut 1):

* **Tasks**   — the hierarchical todo-tree (consumes the existing
  ``kind='todo'`` handler through the in-process runtime).
* **Papers**  — search the corpus and read a paper's PDF in-browser
  (streamed from the NFS-mounted ``corpus_dir``).
* **Console** — call the seven verbs interactively (precis-query).
* **Status**  — corpus / ingest / worker health.

See ``docs/design/precis-web-build.md`` for the build decisions and
``docs/design/precis-web-plan.md`` for the architectural pattern.

The package is an optional install extra (``precis-mcp[web]``). The
``precis web`` CLI subcommand imports it lazily, so a base install
without FastAPI keeps working.
"""

from __future__ import annotations

from precis_web.app import create_app

__all__ = ["create_app"]
