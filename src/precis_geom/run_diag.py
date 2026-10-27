"""``precis_geom.run_diag`` — the CAD run-failure Diag adapter
(gr522025, shared-core-layers S1a).

A failed ``out.run``'s ``manifest.json`` (``status="failed"`` + the
``error`` record :meth:`Run.fail` writes) plus its ``trace.json`` (the
error-marked frames) become one ``precis_diag.RunResult`` whose Diag
anchors on the user-facing op's stable trace ``frame_key`` — the agent goes
from failure to fix site without scraping prose. Both readers of the
record call it: :meth:`precis_geom.out.Run.fail` stashes the result on
the run (``run.result``), and
:func:`precis_se.workspace_import.import_workspace_run` turns it into
the ``BadInput`` refusal (``exc.result``).

The Diag's ``message`` is the verbatim prose the importer's
``_vet_manifest`` raises today — callers matching on ``"run failed"``
do not regress.
"""

from __future__ import annotations

from typing import Any

from precis_diag import Anchor, Diag, FrameSite, RunResult, Status

#: The one ``next`` text the importer's failed-run refusal and this
#: adapter share.
NEXT = (
    "fix the script and re-run — a failed run's out/ is a record of "
    "where it stopped, not importable state"
)


def failed_run_result(
    manifest: dict[str, Any],
    trace_doc: dict[str, Any] | None,
    *,
    run: str = "",
) -> RunResult:
    """``status="failed"`` manifest + trace → a one-Diag ``RunResult``.

    ``run`` is the path to the run's ``trace.json``, or empty if unavailable.
    The first traced frame under the nearest user ancestor identifies
    the op; its descendants form ``anchor.chain``. Without a traced op,
    ``error.at`` pins the actual raise line instead of a sampled def line.
    """
    err = manifest.get("error") or {}
    has_type = isinstance(err, dict) and bool(err.get("type"))
    detail = f" — {err['type']}: {err.get('message', '')}" if has_type else ""
    # out imports this adapter; defer the shared walk until a run fails.
    from precis_geom.out import _error_path

    user, below = _error_path(trace_doc)
    at = err.get("at") if isinstance(err, dict) else None
    at = at if isinstance(at, dict) else {}
    op = below[0] if below else {}
    site = op or at or user or {}
    anchor = Anchor(
        frame_key=str(op.get("frame_key") or ""),
        file=str(site.get("file") or ""),
        line=int(site.get("line") or 0),
        chain=tuple(
            FrameSite(
                frame_key=str(f.get("frame_key") or ""),
                file=str(f.get("file") or ""),
                line=int(f.get("line") or 0),
            )
            for f in below[1:]
        ),
    )
    return RunResult(
        diags=(
            Diag(
                status=Status.ERROR,
                code="cad.run-failed",
                message=f"manifest.json: run failed{detail}",
                anchor=anchor,
                next=NEXT,
                data=(("exception", str(err["type"])),) if has_type else (),
            ),
        ),
        run=run,
    )


__all__ = ["NEXT", "failed_run_result"]
