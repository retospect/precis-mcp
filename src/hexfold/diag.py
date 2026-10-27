"""S2 diagnostics adapter: shared Diag values back to hexfold Findings.

The domain conversion belongs beside report so precis_diag stays entirely
stdlib-only. Import this adapter explicitly; hexfold.report remains usable
without the workspace wheel's shared diagnostics package.
"""

from __future__ import annotations

from precis_diag import Diag, Status

from .report import Finding, Severity

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
