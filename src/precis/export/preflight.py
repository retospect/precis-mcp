"""Draft export preflight — the one shared gate every export path runs.

Both the retraction check (``precis.export.retraction``) and the figure
clearance check (``precis.utils.figure_clearance``) were each bolted onto
individual export entry points ad hoc, so the same draft in the same state
blocked or sailed through depending on which button the user pressed: the
"download PDF" link checked neither gate, the ``.docx``/PDF-job routes
checked only retraction, and only the two worker jobs checked figure
clearance. This module is the single draft-level preflight the report
asked for — one verdict, computed the same way, applied by every path
(``/drafts/{ident}/pdf``, ``/export.docx``, ``/export.pdf`` and the
``draft_export`` / ``remarkable_send`` jobs), with the per-check overrides
(``ignore_retractions``, ``placeholder_figures``) as the only way past.

Both gates fail **open**: a checker that cannot run produces no verdict
rather than wedging every export on a check the user can't act on (the
retraction gate already made this trade deliberately — see
``draft_retraction_report``'s callers; the same logic applies to a figure
walk that throws). The failure is logged loudly instead.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from precis.store.store import Store
    from precis.utils.figure_clearance import FigureClear

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ExportPreflight:
    """The combined verdict of both draft-level export gates."""

    #: The raw retraction report (or ``None`` if the walk couldn't run).
    retraction_report: Any | None = None
    #: Retracted cites currently *blocking* the export (empty when clean or
    #: when the block was overridden via ``ignore_retractions``).
    retraction_blocked: list[Any] = field(default_factory=list)
    #: Retracted cites the caller chose to ship anyway — recorded so the
    #: sources appendix can note the override.
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
        return bool(self.retraction_blocked) or bool(self.figures_blocked)


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
) -> ExportPreflight:
    """Run both draft-level export gates and return one combined verdict.

    ``ignore_retractions`` moves any blocking retracted cite from
    ``retraction_blocked`` to ``retraction_override``; ``placeholder_figures``
    waives every uncleared figure into ``figures_waived`` (image-less ones
    print as placeholders, real-image ones are withheld, never embedded). The caller inspects
    :attr:`ExportPreflight.blocked` and renders / records the appropriate
    stop; the overrides are the only way past."""
    from precis.utils.figure_clearance import partition_uncleared

    report = _safe_retraction_report(store, ref)
    retraction_blocked: list[Any] = []
    retraction_override: list[Any] = []
    if report is not None and report.blocks_export:
        if ignore_retractions:
            retraction_override = list(report.retracted)
        else:
            retraction_blocked = list(report.retracted)

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
        retraction_blocked=retraction_blocked,
        retraction_override=retraction_override,
        clearance_total=total,
        figures_blocked=figures_blocked,
        figures_waived=figures_waived,
    )


__all__ = ["ExportPreflight", "draft_export_preflight"]
