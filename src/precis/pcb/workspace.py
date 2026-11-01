"""PCB run envelopes, shared by local intake and sandbox harvest.

Only the finished manifest and its sibling trace cross this boundary.
Finished PCB runs import through ``put(args={'run': ...})`` or
``precis pcb import-run OUT_DIR``; successful sandbox harvest uses that
same judged door automatically. The sibling trace is mandatory and the
producing job is bound when present (local runs use the board's chunks).
Offline building remains preview-only: an import is an explicit database
operation. Identical construction does not append features again; a
changed existing board refuses rather than pretending the batch extend
path implements workspace replacement/retirement. Service requests and
plane assignments still need the full workspace importer.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from precis.errors import BadInput
from precis.pcb.frames import vet_trace

MAX_RUN_FILE_BYTES = 2_000_000
RUN_NEXT = (
    "put(kind='pcb', id='<slug>', args={'run': "
    "{'manifest.json': <text>, 'trace.json': <text>}})"
)


def read_run(out_dir: Path) -> dict[str, str]:
    """Read only the two regular, contained run files; never follow links."""
    files: dict[str, str] = {}
    for name in ("manifest.json", "trace.json"):
        path = out_dir / name
        if path.is_symlink() or not path.is_file():
            raise BadInput(f"PCB run needs a regular {name}", next=RUN_NEXT)
        if path.stat().st_size > MAX_RUN_FILE_BYTES:
            raise BadInput(f"PCB run {name} exceeds the 2 MB cap", next=RUN_NEXT)
        try:
            files[name] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise BadInput(f"PCB run cannot read {name}", next=RUN_NEXT) from exc
    return files


def run_batch(raw: Any) -> dict[str, Any]:
    """Validate a finished run before its board or trace can be written."""
    if not isinstance(raw, dict) or set(raw) != {"manifest.json", "trace.json"}:
        raise BadInput("PCB run requires manifest.json and trace.json", next=RUN_NEXT)
    for name, text in raw.items():
        if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_RUN_FILE_BYTES:
            raise BadInput(f"PCB run {name} must be text under 2 MB", next=RUN_NEXT)
    try:
        manifest = json.loads(raw["manifest.json"])
    except ValueError as exc:
        raise BadInput("PCB run manifest.json is not JSON", next=RUN_NEXT) from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("kind") != "pcb"
        or manifest.get("schema") != 1
        or manifest.get("status", "succeeded") != "succeeded"
        or not isinstance(manifest.get("board"), dict)
    ):
        raise BadInput(
            "PCB run needs a successful schema-1 pcb manifest", next=RUN_NEXT
        )
    if manifest.get("requests"):
        raise BadInput(
            "PCB run requests are not supported by this import",
            next="run the board without requests; use put(kind='pcb', "
            "id='<slug>', args={'op':'place','placer':'topo','route':True})",
        )
    batch = dict(manifest["board"])
    allowed = {
        "components",
        "nets",
        "connections",
        "measures",
        "features",
        "footprints",
        "generators",
        "meta",
        "net_classes",
        "roles",
        "profile",
        "planes",
    }
    if set(batch) - allowed:
        raise BadInput(
            f"PCB run board has unsupported keys: {sorted(set(batch) - allowed)}",
            next=RUN_NEXT,
        )
    # Plane assignments are not batch-put fields. Refuse rather than lose them.
    if batch.pop("planes", None):
        raise BadInput(
            "PCB run plane assignments need workspace service import", next=RUN_NEXT
        )
    doc, _root = vet_trace(raw["trace.json"])
    return {**batch, "trace": doc}
