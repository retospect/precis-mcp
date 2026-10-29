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

When the failed exception carried a structured report — serialized to
``manifest.error.report`` by :func:`precis_geom.out._error_entry` (a
hexfold ``BuildError`` carries one, gr522031) — the RunResult is one
Diag per finding instead of the single ``cad.run-failed``: the
finding's code/message/``where``/``span``/``data``/``fix`` carry
verbatim (codes are the domain's API), every finding shares the anchor
of the op that raised, and the report shape is read as plain data so
the adapter stays domain-free — the reverse direction lives in
``hexfold.diag``. A report whose findings are all below ``error``
still appends the ``cad.run-failed`` Diag: a failed run's RunResult is
never ``ok``.

A domain exception can also carry the record's own code and entity
refs — ``_error_entry`` serializes ``diag_code``/``diag_refs`` onto
``manifest.error`` (gr522027: the pcb builder names e.g.
``part:<refdes>`` this way) — and the run-failed Diag takes them over
``cad.run-failed`` verbatim.
"""

from __future__ import annotations

import re
from typing import Any

from precis_diag import Anchor, Diag, FrameSite, RunResult, Status

#: The one ``next`` text the importer's failed-run refusal and this
#: adapter share.
NEXT = (
    "fix the script and re-run — a failed run's out/ is a record of "
    "where it stopped, not importable state"
)

#: The ``<domain>.<code>`` shape ``Diag.__post_init__`` enforces — a
#: serialized ``error.code`` can carry anything (nothing validates it
#: upstream), so a malformed one falls back to ``cad.run-failed``
#: instead of refusing the whole record.
_CODE_RE = re.compile(r"[a-z][a-z0-9_-]*\.[A-Za-z0-9][A-Za-z0-9_.-]*")


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
    raw_code = str(err.get("code") or "") if isinstance(err, dict) else ""
    run_failed = Diag(
        status=Status.ERROR,
        # gr522027: a domain error's own ``code``/``refs`` (what
        # ``_error_entry`` reads off ``diag_code``/``diag_refs``)
        # replace the generic record — the pcb builder names its
        # entities this way.
        code=raw_code if _CODE_RE.fullmatch(raw_code) else "cad.run-failed",
        message=f"manifest.json: run failed{detail}",
        anchor=anchor,
        refs=tuple(str(r) for r in err["refs"])
        if isinstance(err, dict) and isinstance(err.get("refs"), list)
        else (),
        next=NEXT,
        data=(("exception", str(err["type"])),) if has_type else (),
    )
    diags = _finding_diags(err, anchor)
    if diags:
        # A failed run is never ok: a report of only INFO/WARN findings
        # still carries the run-level error after its per-finding Diags.
        if all(d.status < Status.ERROR for d in diags):
            diags = (*diags, run_failed)
        return RunResult(diags=diags, run=run)
    return RunResult(diags=(run_failed,), run=run)


def _finding_diags(err: Any, anchor: Anchor) -> tuple[Diag, ...]:
    """``manifest.error.report.findings`` → one Diag each, all anchored
    on the op that raised. Reads the serialized report as plain data;
    a malformed finding loses the whole conversion (the caller falls
    back to the single run-failed Diag) rather than minting a partial
    or invalid record."""
    if not isinstance(err, dict):
        return ()
    report = err.get("report")
    if not isinstance(report, dict):
        return ()
    findings = report.get("findings")
    if not isinstance(findings, list) or not findings:
        return ()
    if any(not isinstance(f, dict) for f in findings):
        return ()
    try:
        return tuple(_finding_diag(f, anchor) for f in findings)
    except (ValueError, TypeError, KeyError, IndexError):
        return ()


def _finding_diag(f: dict[str, Any], anchor: Anchor) -> Diag:
    """One serialized finding dict (``code``, ``severity``, ``message``,
    ``where``, ``span``, ``data``, ``fix``) → its Diag — the wire twin
    of :func:`precis_diag.diag_from_finding`."""
    try:
        status = Status[str(f.get("severity") or "INFO").upper()]
    except KeyError:
        status = Status.INFO
    span = f.get("span")
    data = f.get("data")
    return Diag(
        status=status,
        code=str(f.get("code") or ""),
        message=str(f.get("message") or ""),
        anchor=anchor,
        where=str(f.get("where") or ""),
        span=(int(span[0]), int(span[1]))
        if isinstance(span, (list, tuple)) and len(span) == 2
        else None,
        data=tuple((str(k), v) for k, v in data.items())
        if isinstance(data, dict)
        else (),
        next=str(f.get("fix") or ""),
    )


__all__ = ["NEXT", "failed_run_result"]
