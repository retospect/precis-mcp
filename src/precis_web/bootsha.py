"""The commit this web process booted on (gr496849).

A resumed deploy once left ``com.precis.web`` running r48's imported modules
while the venv and templates on disk were r49's, and every ``/pcb/<slug>``
page returned 500. Nothing in the deploy could tell, because the process
never said which build it was. ``/healthz`` now carries :data:`BOOT_SHA` in
the ``X-Precis-Boot-Sha`` header; ``deploy/redeploy-precis.yml`` compares it
with the pinned deploy sha before and after the bounce.

Frozen at import (process start) on purpose: the value answers "which commit
did *this process* load", so a venv that moves underneath a live process
shows up as a mismatch instead of reading as current.
"""

from __future__ import annotations

import json
from importlib.metadata import distribution

#: Response header on ``/healthz`` that carries the boot sha.
BOOT_SHA_HEADER = "X-Precis-Boot-Sha"

#: What the header says when the install records no commit (editable or local
#: ``pip install .``); never matches a deploy sha, so a deploy treats it as
#: stale and bounces.
UNKNOWN = "unknown"


def read_installed_sha() -> str:
    """The commit PEP 610 ``direct_url.json`` records for the installed wheel."""
    try:
        raw = distribution("precis-mcp").read_text("direct_url.json")
        commit = (json.loads(raw or "{}").get("vcs_info") or {}).get("commit_id")
    except Exception:  # metadata access must never break app import
        return UNKNOWN
    return str(commit) if commit else UNKNOWN


BOOT_SHA: str = read_installed_sha()
