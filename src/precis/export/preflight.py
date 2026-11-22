"""Shared draft export preflight.

Figure clearance gates export. The retraction walk reports stored
coverage and notice status so the PDF route can invalidate a cached
render when notice decisions may have changed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from precis.export import progress

if TYPE_CHECKING:
    from precis.store.store import Store
    from precis.utils.figure_clearance import FigureClear

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ExportPreflight:
    """Stored citation report and the figure clearance decision."""

    #: The raw retraction report (or ``None`` if the walk couldn't run).
    retraction_report: Any | None = None
    #: Retracted cites to include in the legacy sources audit.
    retraction_override: list[Any] = field(default_factory=list)
    #: Total figures walked (for the "N of M" wording).
    clearance_total: int = 0
    #: Uncleared figures still *blocking* the export (after any waiver).
    figures_blocked: list[FigureClear] = field(default_factory=list)
    #: Uncleared figures waived under the opt-in: image-less ones ship as
    #: placeholders, real-image ones are withheld (see ``figures_withheld``).
    figures_waived: list[FigureClear] = field(default_factory=list)

    @property
    def figures_withheld(self) -> list[FigureClear]:
        """Waived figures with a real image: exported as a withheld box."""
        return [f for f in self.figures_waived if not f.assetless]

    @property
    def withheld_handles(self) -> frozenset[str]:
        return frozenset(f.dc for f in self.figures_withheld)

    @property
    def blocked(self) -> bool:
        return bool(self.figures_blocked)


def _safe_retraction_report(store: Store, ref: Any) -> Any | None:
    """``draft_retraction_report`` wrapped defensively — a failure inside the
    walk must never take down the export it gates. Returns ``None`` on any
    exception (fail-open); the caller treats that as "no report available"
    and never blocks on a check that couldn't run."""
    from precis.export.retraction import draft_retraction_report

    try:
        return draft_retraction_report(store, ref)
    except Exception:
        log.error(
            "preflight: retraction report failed for draft=%s — treating as "
            "unavailable (fail-open)",
            getattr(ref, "id", None),
            exc_info=True,
        )
        return None


def _safe_figure_clearance(store: Store, ref_id: int) -> Any | None:
    """``draft_figure_clearance`` wrapped defensively — same fail-open trade
    as the retraction walk above. Returns ``None`` on any exception."""
    from precis.utils.figure_clearance import draft_figure_clearance

    try:
        return draft_figure_clearance(store, ref_id)
    except Exception:
        log.error(
            "preflight: figure clearance failed for draft=%s — treating as "
            "unavailable (fail-open)",
            ref_id,
            exc_info=True,
        )
        return None


def draft_export_preflight(
    store: Store,
    ref: Any,
    *,
    ignore_retractions: bool = False,
    placeholder_figures: bool = False,
    retraction_report: Any | None = None,
) -> ExportPreflight:
    """Read stored retractions and partition figures by clearance.

    Legacy ``ignore_retractions`` requests an audit entry for the cited
    retractions. ``placeholder_figures`` allows uncleared figures to
    export as placeholders.
    """
    from precis.utils.figure_clearance import partition_uncleared

    progress.report("checking cited papers for retractions")
    report = (
        retraction_report
        if retraction_report is not None
        else _safe_retraction_report(store, ref)
    )
    if report is not None:
        progress.report(report.coverage_summary())
    retraction_override: list[Any] = []
    if report is not None and ignore_retractions:
        retraction_override = list(report.retracted)

    progress.report("checking figure permissions")
    clearance = _safe_figure_clearance(store, ref.id)
    total = clearance.total if clearance is not None else 0
    figures_blocked: list[FigureClear] = []
    figures_waived: list[FigureClear] = []
    if clearance is not None and clearance.uncleared:
        figures_blocked, figures_waived = partition_uncleared(
            clearance.uncleared, placeholder_figures=placeholder_figures
        )

    return ExportPreflight(
        retraction_report=report,
        retraction_override=retraction_override,
        clearance_total=total,
        figures_blocked=figures_blocked,
        figures_waived=figures_waived,
    )


__all__ = ["ExportPreflight", "draft_export_preflight"]
