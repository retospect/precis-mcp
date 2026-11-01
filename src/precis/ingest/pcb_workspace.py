"""Trusted-side intake of a finished local or sandbox PCB run."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.pcb.workspace import RUN_NEXT, read_run

if TYPE_CHECKING:
    from precis.response import Response
    from precis.store import Store


def import_pcb_run(
    store: Store, out_dir: Path, *, slug: str | None = None, run_job: int | None = None
) -> Response:
    """Use the same judged put as MCP, binding trace and run job together."""
    files = read_run(out_dir)
    if slug is None:
        try:
            manifest = json.loads(files["manifest.json"])
        except ValueError as exc:
            raise BadInput("PCB run manifest.json is not JSON", next=RUN_NEXT) from exc
        slug = manifest.get("slug") if isinstance(manifest, dict) else None
    if not isinstance(slug, str) or not slug.strip():
        raise BadInput("PCB run needs a design slug", next=RUN_NEXT)
    args: dict[str, Any] = {"run": files}
    if run_job is not None:
        args["run_job"] = run_job
    return Hub(store=store).sibling("pcb").put(id=slug, args=args)
