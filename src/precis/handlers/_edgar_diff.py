"""Quarter-to-quarter section comparison for the ``edgar`` kind.

The "identify and tag the interesting bits, compare quarter to quarter"
capability. Given a filing, this module:

1. **Aligns** it against the prior same-form filing for the same
   company (10-Q vs the previous 10-Q, 10-K vs the previous 10-K),
   section by section — keyed on the canonical section id the parser
   stamped on each block (``Item 1A Risk Factors``, ``Item 7 MD&A``, …).
2. **Identifies the interesting bits**: sections that were added,
   removed, or materially changed, with paragraph-level added/removed
   detail (new risk factors, changed MD&A language, …).
3. **Tags** the current filing so the changes are queryable
   (``changed:item-1a``, ``new-risk-factor``) and can be folded into
   the morning brief.

The compute step is **pure** (no writes, no network) and works on
already-ingested filings, so it's unit-testable. The section labels it
relies on come from ``_edgar_sections`` via ``_edgar_ingest`` — each
block carries ``meta.canonical_id`` + ``meta.section_path``.

Downstream surfaces (spec § comparison): ``view='diff'`` renders the
delta on demand and tags the ref; the phase-2 watch runner additionally
mints ``finding`` + ``news`` refs (see ``mint_diff_findings`` /
``mint_diff_news``).
"""

from __future__ import annotations

import difflib
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from precis.response import Response
from precis.store import Ref, Tag
from precis.store._mappers import _REFS_COLS_ALIASED, _row_to_ref
from precis.utils import handle_registry

log = logging.getLogger(__name__)

#: Below this SequenceMatcher ratio a section counts as "materially
#: changed" (vs cosmetic whitespace / boilerplate drift).
_MATERIAL_CHANGE_RATIO = 0.97

#: Canonical section ids whose changes are high-signal for the brief.
_HIGH_SIGNAL_SECTIONS: frozenset[str] = frozenset(
    {"item-1a", "risk-factors", "item-7", "mdna", "item-3", "item-7a"}
)


@dataclass(frozen=True, slots=True)
class SectionDelta:
    """One section's change between two consecutive filings."""

    canonical_id: str
    section_path: list[str]
    status: str  # 'added' | 'removed' | 'changed'
    similarity: float  # 0..1 (1.0 for added/removed sentinels)
    added_paras: list[str] = field(default_factory=list)
    removed_paras: list[str] = field(default_factory=list)

    @property
    def high_signal(self) -> bool:
        return self.canonical_id in _HIGH_SIGNAL_SECTIONS


@dataclass(frozen=True, slots=True)
class FilingDiff:
    """The material section-level changes of one filing vs its predecessor."""

    current_ref_id: int
    current_slug: str
    prior_ref_id: int
    prior_slug: str
    form: str
    current_period: str | None
    prior_period: str | None
    deltas: list[SectionDelta] = field(default_factory=list)

    @property
    def has_new_risk_factors(self) -> bool:
        return any(
            d.canonical_id in ("item-1a", "risk-factors") and d.added_paras
            for d in self.deltas
        )


# ---------------------------------------------------------------------------
# Prior-filing lookup
# ---------------------------------------------------------------------------


def find_prior_ref(store: Any, ref: Ref) -> Ref | None:
    """Most-recent locally-ingested same-form filing for the same CIK,
    with a period/filed date strictly before ``ref``'s.

    Returns ``None`` when no earlier same-form filing has been ingested.
    """
    meta = ref.meta or {}
    cik = str(meta.get("cik") or "")
    form = str(meta.get("form") or "")
    when = str(meta.get("period_of_report") or meta.get("filed_date") or "")
    if not (cik and form and when):
        return None
    sql = f"""
        SELECT {_REFS_COLS_ALIASED}
        FROM   refs r
        WHERE  r.kind = 'edgar' AND r.deleted_at IS NULL
               AND r.meta->>'cik' = %s
               AND r.meta->>'form' = %s
               AND coalesce(r.meta->>'period_of_report',
                            r.meta->>'filed_date') < %s
               AND r.ref_id <> %s
        ORDER BY coalesce(r.meta->>'period_of_report',
                          r.meta->>'filed_date') DESC
        LIMIT 1
    """
    with store.pool.connection() as conn:
        row = conn.execute(sql, (cik, form, when, ref.id)).fetchone()
    return _row_to_ref(row) if row is not None else None


# ---------------------------------------------------------------------------
# Section grouping + diff compute (pure)
# ---------------------------------------------------------------------------


def _sections_for_ref(
    store: Any, ref_id: int
) -> dict[str, tuple[list[str], list[str]]]:
    """Group a filing's blocks by canonical section id.

    Returns ``{canonical_id: (section_path, [paragraph texts in order])}``.
    """
    blocks = store.list_blocks_for_ref(ref_id)
    out: dict[str, tuple[list[str], list[str]]] = {}
    for b in blocks:
        meta = b.meta or {}
        cid = str(meta.get("canonical_id") or "body")
        path = list(meta.get("section_path") or [])
        if cid not in out:
            out[cid] = (path, [])
        out[cid][1].append(b.text)
    return out


def _norm_para(text: str) -> str:
    """Normalise a paragraph for set-diff (collapse ws, casefold)."""
    return re.sub(r"\s+", " ", text or "").strip().casefold()


Sections = dict[str, tuple[list[str], list[str]]]


def diff_sections(current: Sections, prior: Sections) -> list[SectionDelta]:
    """Pure section-delta computation between two grouped filings.

    ``current`` / ``prior`` map ``canonical_id → (section_path, paras)``
    (the shape :func:`_sections_for_ref` returns). Returns only the
    material deltas (added / removed / changed), sorted by section id.
    The unlabelled ``body`` preamble is never compared.
    """
    deltas: list[SectionDelta] = []
    for cid in sorted(set(current) | set(prior)):
        if cid == "body":
            continue
        cur = current.get(cid)
        pri = prior.get(cid)
        if cur is not None and pri is None:
            deltas.append(
                SectionDelta(
                    canonical_id=cid,
                    section_path=cur[0],
                    status="added",
                    similarity=0.0,
                    added_paras=list(cur[1]),
                )
            )
            continue
        if cur is None and pri is not None:
            deltas.append(
                SectionDelta(
                    canonical_id=cid,
                    section_path=pri[0],
                    status="removed",
                    similarity=0.0,
                    removed_paras=list(pri[1]),
                )
            )
            continue
        assert cur is not None and pri is not None
        cur_paras, pri_paras = cur[1], pri[1]
        ratio = difflib.SequenceMatcher(
            None, "\n".join(pri_paras), "\n".join(cur_paras)
        ).ratio()
        if ratio >= _MATERIAL_CHANGE_RATIO:
            continue
        cur_norm = {_norm_para(p) for p in cur_paras}
        pri_norm = {_norm_para(p) for p in pri_paras}
        added = [p for p in cur_paras if _norm_para(p) not in pri_norm]
        removed = [p for p in pri_paras if _norm_para(p) not in cur_norm]
        # A low raw-text ratio with no paragraph-level set difference is
        # cosmetic (whitespace churn, paragraph reordering) — not a
        # material content change. Skip it.
        if not added and not removed:
            continue
        deltas.append(
            SectionDelta(
                canonical_id=cid,
                section_path=cur[0],
                status="changed",
                similarity=ratio,
                added_paras=added,
                removed_paras=removed,
            )
        )
    return deltas


def compute_diff(store: Any, ref: Ref) -> FilingDiff | None:
    """Compute the material section deltas of ``ref`` vs its predecessor.

    Pure read: no writes, no network. Returns ``None`` when there's no
    prior same-form filing ingested to compare against.
    """
    prior = find_prior_ref(store, ref)
    if prior is None:
        return None

    deltas = diff_sections(
        _sections_for_ref(store, ref.id),
        _sections_for_ref(store, prior.id),
    )

    return FilingDiff(
        current_ref_id=ref.id,
        current_slug=ref.slug or str(ref.id),
        prior_ref_id=prior.id,
        prior_slug=prior.slug or str(prior.id),
        form=str((ref.meta or {}).get("form") or ""),
        current_period=(ref.meta or {}).get("period_of_report"),
        prior_period=(prior.meta or {}).get("period_of_report"),
        deltas=deltas,
    )


# ---------------------------------------------------------------------------
# Tagging
# ---------------------------------------------------------------------------


def diff_tags(diff: FilingDiff) -> list[str]:
    """Open tags describing the material changes (lowercase, ws-free)."""
    tags = [f"changed:{d.canonical_id}" for d in diff.deltas]
    if diff.has_new_risk_factors:
        tags.append("new-risk-factor")
    return tags


def apply_diff_tags(store: Any, ref: Ref, diff: FilingDiff) -> list[str]:
    """Idempotently stamp the change tags on the current filing ref.

    Returns the tags applied. Best-effort per tag — a malformed value
    never aborts the whole set.
    """
    applied: list[str] = []
    for tag_str in diff_tags(diff):
        try:
            store.add_tag(ref.id, Tag.parse(tag_str), set_by="system")
            applied.append(tag_str)
        except Exception:
            log.warning("edgar diff: skipped tag %r", tag_str)
    return applied


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

_MAX_EXAMPLES = 3
_EXAMPLE_CHARS = 300


def render_diff(*, store: Any, ref: Ref, apply_tags: bool = True) -> Response:
    """Render the quarter-to-quarter section diff for a filing.

    Computes the delta, tags the ref (unless ``apply_tags=False``), and
    renders a section-by-section summary with a few example added lines.
    """
    handle = handle_registry.format_handle("edgar", ref.id)
    diff = compute_diff(store, ref)
    if diff is None:
        return Response(
            body=(
                f"# {handle} — no prior filing to compare\n\n"
                "No earlier same-form filing for this company has been "
                "ingested. Ingest the prior one first:\n"
                f"  get(kind='edgar', id='ticker:<symbol>')  # list filings\n"
                f"  get(kind='edgar', id='<prior-accession>') # ingest it"
            )
        )

    if apply_tags:
        apply_diff_tags(store, ref, diff)

    cur_when = diff.current_period or "?"
    pri_when = diff.prior_period or "?"
    lines = [
        f"# {handle} — {diff.form} quarter-to-quarter diff",
        f"_comparing {diff.current_slug} ({cur_when}) "
        f"vs {diff.prior_slug} ({pri_when})_",
        "",
    ]

    if not diff.deltas:
        lines.append("No material section changes detected.")
        return Response(body="\n".join(lines))

    for d in diff.deltas:
        section_name = " ".join(d.section_path) or d.canonical_id
        if d.status == "added":
            lines.append(f"## + {section_name}  (new section)")
        elif d.status == "removed":
            lines.append(f"## − {section_name}  (removed)")
        else:
            pct = round(d.similarity * 100)
            lines.append(
                f"## ~ {section_name}  ({pct}% similar · "
                f"+{len(d.added_paras)} / −{len(d.removed_paras)} paragraphs)"
            )
        for para in d.added_paras[:_MAX_EXAMPLES]:
            snippet = re.sub(r"\s+", " ", para).strip()[:_EXAMPLE_CHARS]
            lines.append(f"  + {snippet}")
        lines.append("")

    return Response(body="\n".join(lines).rstrip())


__all__ = [
    "FilingDiff",
    "SectionDelta",
    "apply_diff_tags",
    "compute_diff",
    "diff_sections",
    "diff_tags",
    "find_prior_ref",
    "render_diff",
]
