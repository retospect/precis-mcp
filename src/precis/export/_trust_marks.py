"""Shared trust-mark bookkeeping for the draft exporters (docx / latex).

Both :mod:`precis.export.docx` and :mod:`precis.export.latex` resolve a
finding-backed citation's trust via the ONE shared derivation
(:func:`precis.taproot.trust.claim_trust`) and need the same three things
out of it, in the same shape, so the two output formats never drift:

* the resolved :class:`~precis.taproot.trust.TrustState`, cached so a
  finding cited many times in one export is resolved exactly once;
* the accumulated set of non-clean marks, for the end-matter "Unverified
  claims" list each exporter renders in its own markup;
* the set of findings rendered clean *only* because of an author's
  override, for the single ``ref_events`` export-record row
  (docs/proposals/finding-trust-surfaces.md §2).

Pure bookkeeping — no rendering, no I/O beyond the read-only ``store``
calls :func:`~precis.taproot.trust.claim_trust` itself makes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from precis.taproot.trust import TrustState, claim_trust

#: The louder mark's fixed text (Motivation / AC 2) — identical wording in
#: both exporters, only the surrounding markup (bold/red run vs
#: ``\textbf``) differs.
UNSUPPORTED_MARK_TEXT = "[UNSUPPORTED — cited source does not back this claim]"


#: Per-label bracket lead word for the inline mark (``unsupported`` uses
#: its own louder fixed text). The two middle states read as calm notes,
#: not problems: an author-declared-unobtainable source, or one backed by
#: its abstract only.
_MARK_LEAD: dict[str, str] = {
    "abstract": "abstract-only",
    "vouched": "author-vouched",
    "unverified": "unverified",
}


def mark_text(state: TrustState) -> str:
    """The inline bracket text for any non-clean, non-unsupported state —
    ``[abstract-only: …]`` / ``[author-vouched: …]`` / ``[unverified: …]``.
    ``unsupported`` has its own louder :data:`UNSUPPORTED_MARK_TEXT`; a
    caller branches that out before reaching here."""
    lead = _MARK_LEAD.get(state.label, "unverified")
    suffix = f": {state.note}" if state.note else ""
    return f"[{lead}{suffix}]"


@dataclass
class TrustTracker:
    """Per-export cache + accumulator, threaded through one export's
    citation-rendering pass via the format-specific ``_Ctx``."""

    store: Any
    _cache: dict[int, TrustState] = field(default_factory=dict)
    #: finding_ref_id → (claim title, state), insertion order = first
    #: citation order — every non-clean mark, for the end-matter list.
    marks: dict[int, tuple[str, TrustState]] = field(default_factory=dict)
    #: finding_ref_id → ``{finding_ref_id, note, by, at}``, only for
    #: findings rendered clean via override — the ``ref_events``
    #: export-record payload (§2).
    overridden: dict[int, dict[str, Any]] = field(default_factory=dict)

    def resolve(self, finding_ref_id: int) -> TrustState:
        """The finding's :class:`TrustState`, resolved once and cached.
        Also records it into :attr:`marks` / :attr:`overridden` as
        appropriate — callers don't need to inspect the label themselves
        to keep the end-matter list / export record in sync."""
        state = self._cache.get(finding_ref_id)
        if state is not None:
            return state
        state = claim_trust(self.store, finding_ref_id)
        self._cache[finding_ref_id] = state
        # The end-matter list is the *problem* roll-up — only the genuinely
        # unverified / unsupported. A folded override (``abstract``/
        # ``vouched``) is author-declared, not a problem, so it stays OUT of
        # that list (it still carries a calm inline body mark). It IS
        # recorded in the export-record audit. A dedicated calm "declared-
        # unobtainable sources" end-matter section is a filed follow-on.
        if state.label in ("unverified", "unsupported"):
            self.marks[finding_ref_id] = (self._title_of(finding_ref_id), state)
        if state.overridden:
            self.overridden[finding_ref_id] = self._override_meta(finding_ref_id, state)
        return state

    def _ref(self, finding_ref_id: int) -> Any:
        return self.store.fetch_refs_by_ids([finding_ref_id]).get(finding_ref_id)

    def _title_of(self, finding_ref_id: int) -> str:
        ref = self._ref(finding_ref_id)
        return (getattr(ref, "title", None) or None) or f"finding {finding_ref_id}"

    def _override_meta(self, finding_ref_id: int, state: TrustState) -> dict[str, Any]:
        ref = self._ref(finding_ref_id)
        override = ((getattr(ref, "meta", None) or {}) if ref is not None else {}).get(
            "unacquirable_override"
        ) or {}
        return {
            "finding_ref_id": finding_ref_id,
            "note": override.get("note", state.note),
            "by": override.get("by"),
            "at": override.get("at"),
        }


def unverified_claims_entries(trust: Any) -> list[tuple[str, str, str]]:
    """The end-matter "Unverified claims" problem list, as ``(title, tag,
    detail)`` triples in insertion (first-citation) order — one per finding
    that rendered unverified/unsupported (the folded ``abstract``/``vouched``
    states are excluded, see :meth:`TrustTracker.resolve`). ``tag`` is
    ``'UNSUPPORTED'`` or ``'unverified'``; ``detail`` is ``''`` or
    ``' — <note>'``. Shared so ``export/docx.py`` and ``export/latex.py``
    never drift on the wording; each renders these into its own markup.
    Empty when nothing was marked."""
    if trust is None or not trust.marks:
        return []
    entries: list[tuple[str, str, str]] = []
    for title, state in trust.marks.values():
        tag = "UNSUPPORTED" if state.label == "unsupported" else "unverified"
        detail = f" — {state.note}" if state.note else ""
        entries.append((title, tag, detail))
    return entries


def record_override_event(store: Any, ref: Any, trust: Any) -> None:
    """Append the ONE ``ref_events`` audit row for this export, when at
    least one finding rendered via an author's override — i.e. folded to the
    calm ``abstract``/``vouched`` state (§2) — never when nothing was
    overridden (AC 3/6: no row on a plain-clean export). Shared so the docx
    and latex exporters write the identical event shape."""
    if trust is None or not trust.overridden:
        return
    store.append_event(
        ref.id,
        source="export",
        event="export_override",
        payload={"overridden": list(trust.overridden.values())},
    )


__all__ = [
    "UNSUPPORTED_MARK_TEXT",
    "TrustTracker",
    "mark_text",
    "record_override_event",
    "unverified_claims_entries",
]
