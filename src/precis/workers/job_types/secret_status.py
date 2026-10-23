"""``secret_status`` job_type — run one credential-status check pass.

Deterministic in-process work (no LLM — the Q5 rule holds: code sees keys,
no model ever does). Runs :func:`precis.secret_status.record_status`: the
fixed-endpoint auth probes plus the no-decrypt presence check, persisting
one status entry per secret to ``app_state`` — the blob ``/secrets``
renders. Infra-class secrets (``svc/infra/*``) get a presence check only;
they are never probed.

Who mints it: the ``secret_status`` scheduler cadence (periodic,
``origin='background'``) and the /secrets page's "Check now" POST
(``origin='human'``) — both through
:func:`precis.workers.secret_check.mint_status_job`, which refuses a
second mint while one is pending. No secrets ever reach the job row's
meta, chunks, or logs — ``meta.status`` carries counts only.
"""

from __future__ import annotations

import logging
from typing import Any

from precis.workers.job_types import JobTypeSpec

log = logging.getLogger(__name__)

_PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}


def _dispatch(ctx: Any, spec: JobTypeSpec) -> None:
    """Plugin dispatcher invoked by ``job_inproc`` for a claimed job."""
    from precis import secret_status

    try:
        snap = secret_status.record_status(ctx.store)
    except Exception as exc:  # a failed check must not wedge the lane
        # Type name only — str(exc)/traceback can echo a key-bearing URL.
        log.warning("secret_status job: record_status raised (%s)", type(exc).__name__)
        ctx.record_failure(f"secret_status: record_status raised: {type(exc).__name__}")
        return

    counts: dict[str, int] = {}
    for entry in snap.entries.values():
        counts[entry.state] = counts.get(entry.state, 0) + 1
    ctx.set_meta(
        status={
            "checked_at": snap.checked_at.isoformat() if snap.checked_at else None,
            **counts,
        }
    )
    ctx.append_chunk(
        "job_summary",
        "secret_status: " + ", ".join(f"{n} {s}" for s, n in sorted(counts.items())),
    )


SPEC = JobTypeSpec(
    name="secret_status",
    params_schema=_PARAMS_SCHEMA,
    compatible_executors=frozenset({"job_inproc"}),
    requires=frozenset(),  # deterministic in-process — no executor capabilities
    description=(
        "Probe configured registry secrets and write the per-secret "
        "validity snapshot /secrets renders (infra class: presence only)."
    ),
    dispatch=_dispatch,
    # A queued second check adds nothing — the pending run refreshes the
    # same blob. (Belt for dispatch-pass mints; mint_status_job already
    # refuses its own duplicates.)
    dedupe_pending=True,
)


def load() -> JobTypeSpec:
    return SPEC


__all__ = ["SPEC", "load"]
