"""Figure clearance — is a draft's figure cleared to ship? (ADR 0034 §4)

One source of truth for the rule, shared by the web reader (a top
warning / an all-clear note) and the export job (a hard gate — an
uncleared figure fails the export, like a bare ``\\cite`` fails review).

The rule by ``origin``:

* ``original``    — ours; always cleared.
* ``own_graph``   — generated from data; cleared (the data-supplement
  check arrives with the graph recipe — ADR 0035 — so it is optimistic
  until ``figure_data`` exists).
* ``third_party`` — reused under a publisher permission: cleared iff the
  permission is **granted** and **not past ``expires_at``**.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from precis.utils import handle_registry


def figure_status(fig: dict[str, Any]) -> tuple[bool, str]:
    """``(cleared, reason)`` for one figure's ``meta.figure`` payload.
    ``reason`` is empty when cleared, else a short human explanation."""
    origin = fig.get("origin")
    if origin == "third_party":
        perm = fig.get("permission") or {}
        status = perm.get("status")
        if status != "granted":
            return (
                False,
                f"third-party figure, permission not granted ({status or 'none'})",
            )
        exp = str(perm.get("expires_at") or "").strip()
        if exp:
            try:
                if date.fromisoformat(exp) < date.today():
                    return False, f"third-party permission expired {exp}"
            except ValueError:
                pass  # unparseable date — don't fail the export on it
        return True, ""
    # original / own_graph — cleared (own_graph data-supplement gate is a
    # later slice with the graph recipe).
    return True, ""


@dataclass(frozen=True, slots=True)
class FigureClear:
    """One figure's clearance verdict, for display / the gate."""

    dc: str  # the dc<chunk_id> handle
    caption: str
    origin: str | None
    cleared: bool
    reason: str


@dataclass(frozen=True, slots=True)
class ClearanceSummary:
    """A draft's figure-clearance roll-up."""

    total: int
    uncleared: list[FigureClear] = field(default_factory=list)

    @property
    def all_clear(self) -> bool:
        return self.total > 0 and not self.uncleared


def draft_figure_clearance(store: Any, ref_id: int) -> ClearanceSummary:
    """Walk a draft's figure chunks and roll up their clearance.

    Clearance is ``origin × medium`` (ADR 0057): the per-figure verdict comes
    from the source resolver, so an **asset-less** figure (no blob, no canvas,
    no recipe) counts as *uncleared* ("no image yet") instead of silently
    shipping — while a real blob / drawn canvas stays cleared per its origin.
    """
    # Lazy import breaks the cycle: figure_source imports figure_status here.
    from precis.utils.figure_source import resolve_figure_source

    uncleared: list[FigureClear] = []
    total = 0
    for c in store.reading_order(ref_id):
        if c.chunk_kind != "figure":
            continue
        total += 1
        src = resolve_figure_source(store, c)
        if not src.cleared:
            fig = (c.meta or {}).get("figure", {})
            uncleared.append(
                FigureClear(
                    dc=handle_registry.format_handle("draft", c.chunk_id, chunk=True),
                    caption=(c.text or "").splitlines()[0] if c.text else "",
                    origin=fig.get("origin"),
                    cleared=False,
                    reason=src.reason,
                )
            )
    return ClearanceSummary(total=total, uncleared=uncleared)
