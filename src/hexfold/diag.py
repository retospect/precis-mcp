"""S2 diagnostics adapter: shared Diag values back to hexfold Findings.

The domain conversion belongs beside report so precis_diag stays entirely
stdlib-only. Import this adapter explicitly; hexfold.report remains usable
without the workspace wheel's shared diagnostics package.

``diags_from_report`` is the forward half (gr522031): a live
:class:`~hexfold.report.Report`'s findings become anchored Diags for
callers holding the object — the wire twin is the manifest path in
:func:`precis_geom.run_diag.failed_run_result`, which reads the
serialized report :func:`precis_geom.out._error_entry` writes on a
failed run and needs no domain import.
"""

from __future__ import annotations

from dataclasses import replace

from precis_diag import Anchor, Diag, Status, diag_from_finding

from .report import Finding, Report, Severity

_SEVERITY_FROM_STATUS = {
    Status.INFO: Severity.INFO,
    Status.WARN: Severity.WARN,
    Status.ERROR: Severity.ERROR,
}


def finding_from_diag(d: Diag) -> Finding:
    """Convert a Diag without silently downgrading ok/not_checked to INFO."""
    if d.status not in _SEVERITY_FROM_STATUS:
        raise ValueError(
            f"finding_from_diag: status {d.status.name.lower()!r} has no "
            "hexfold Severity — ok/not_checked are not findings"
        )
    return Finding(
        code=d.code,
        severity=_SEVERITY_FROM_STATUS[d.status],
        message=d.message,
        where=d.where,
        span=d.span,
        data=d.data,
        fix=d.next,
    )


def diags_from_report(report: Report, *, anchor: Anchor = Anchor()) -> tuple[Diag, ...]:
    """A live :class:`Report`'s findings → anchored Diags.

    ``anchor`` attaches every finding to the op that produced it — for a
    workspace run that is the traced call's frame (the path
    ``run_diag.failed_run_result`` walks); a caller without a trace
    leaves the default empty anchor. Codes, ``where``, ``span``,
    ``data`` and ``fix`` carry verbatim."""
    diags = [diag_from_finding(f) for f in report.findings]
    if anchor != Anchor():
        diags = [replace(d, anchor=anchor) for d in diags]
    return tuple(diags)
