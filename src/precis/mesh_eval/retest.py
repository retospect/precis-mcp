"""Folded-key agreement between two discovery runs — the ported
``compare_runs.py``.

This is the categorise lane's reference number: the frontier model's own
test-retest agreement on the same hubs, which is the ceiling a local
model's agreement-with-frontier can be held to
(``local-mesh-upkeep.md`` §1, the auto-apply bar's first leg;
``taxonomy-bootstrap.md``'s 0.711 gate is the same statistic on the
folded keys). Reads each run's ``discovered.jsonl``, restricts both to
the hubs they share, and reports mention-level agreement raw and folded
plus a hub-bootstrapped CI — the same numbers the scratch script
reported (0.794 folded on the packed pair).
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from precis.taxonomy import normalise as norm
from precis.taxonomy.types import Anchor, DiscoveredTerm, Mention

if TYPE_CHECKING:
    from precis.taxonomy.config import MeasurandAliases


def load_terms(dump: Path) -> list[DiscoveredTerm]:
    """``discovered.jsonl`` rows → ``DiscoveredTerm``s (as compare_runs did)."""
    terms: list[DiscoveredTerm] = []
    with (dump / "discovered.jsonl").open(encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            m = row["mention"]
            a = m["anchor"]
            mention = Mention(
                anchor=Anchor(
                    source_ref_id=a["ref_id"],
                    start=a["start"],
                    end=a["end"],
                    source_kind=a.get("source_kind", "ref_text"),
                    scheme=a.get("scheme", 1),
                ),
                kind=m["kind"],
                literal=m["literal"],
                raw_unit=m.get("raw_unit"),
                context=m.get("context", ""),
                marker=m.get("marker"),
            )
            terms.append(
                DiscoveredTerm(
                    mention=mention,
                    measurand=row["measurand"],
                    half=row["half"],
                    dimension_text=row.get("dimension_text"),
                    reference_state=row.get("reference_state"),
                    convention=row.get("convention"),
                    normalisation_basis=row.get("normalisation_basis"),
                    subject_label=row.get("subject_label", ""),
                    required_conditions=tuple(row.get("required_conditions") or ()),
                )
            )
    return terms


@dataclass(frozen=True, slots=True)
class RetestResult:
    """Mention-level agreement of two runs over their shared hubs."""

    hubs_a: int
    hubs_b: int
    hubs_shared: int
    n_both: int
    raw_agreement: float
    folded_agreement: float
    dimension_agreement: float
    per_hub_folded: float
    #: (p10, p50, p90) of the hub-bootstrapped folded agreement.
    bootstrap: tuple[float, float, float]
    #: Top folded-key disagreements ``(key_a, key_b, count)``.
    disagreements: tuple[tuple[str, str, int], ...]


def folded_agreement(
    dump_a: str | Path,
    dump_b: str | Path,
    aliases: MeasurandAliases | None = None,
    *,
    boot: int = 2000,
    seed: int = 20261002,
    top: int = 12,
) -> RetestResult:
    """Folded-key test-retest between two run dumps over shared hubs.

    Same computation as the scratch ``compare_runs.py``: shared-hub
    restriction, per-anchor pairing, ``_lexical_key`` raw and
    ``alias_key`` folded agreement, per-hub mean, and a hub-resampled
    bootstrap over mention-weighted agreement.
    """
    runs = {"a": load_terms(Path(dump_a)), "b": load_terms(Path(dump_b))}
    hubs = {k: {t.mention.anchor.source_ref_id for t in v} for k, v in runs.items()}
    shared = hubs["a"] & hubs["b"]
    sliced = {
        k: [t for t in v if t.mention.anchor.source_ref_id in shared]
        for k, v in runs.items()
    }

    def _anchor(t: DiscoveredTerm) -> tuple[int, int, int]:
        a = t.mention.anchor
        return (a.source_ref_id, a.start, a.end)

    by_a = {_anchor(t): t for t in sliced["a"]}
    by_b = {_anchor(t): t for t in sliced["b"]}
    both = sorted(set(by_a) & set(by_b))
    n = len(both)
    if not n:
        return RetestResult(
            len(hubs["a"]),
            len(hubs["b"]),
            len(shared),
            0,
            0.0,
            0.0,
            0.0,
            0.0,
            (0.0, 0.0, 0.0),
            (),
        )

    raw_same = folded_same = dim_same = 0
    from collections import Counter

    diffs: Counter[tuple[str, str]] = Counter()
    per_hub_fold: dict[int, list[bool]] = {}
    for anc in both:
        ta, tb = by_a[anc], by_b[anc]
        raw_same += norm._lexical_key(ta.measurand) == norm._lexical_key(tb.measurand)
        fa = norm.alias_key(ta.measurand, aliases)
        fb = norm.alias_key(tb.measurand, aliases)
        folded_same += fa == fb
        dim_same += (ta.dimension_text or "") == (tb.dimension_text or "")
        if fa != fb:
            diffs[(fa, fb)] += 1
        per_hub_fold.setdefault(anc[0], []).append(fa == fb)

    hub_mean = (
        sum(sum(v) / len(v) for v in per_hub_fold.values()) / len(per_hub_fold)
        if per_hub_fold
        else 0.0
    )
    rng = random.Random(seed)
    hub_ids = sorted(per_hub_fold)
    boots: list[float] = []
    for _ in range(boot):
        sample = [hub_ids[rng.randrange(len(hub_ids))] for _ in hub_ids]
        agree = sum(sum(per_hub_fold[h]) for h in sample)
        total = sum(len(per_hub_fold[h]) for h in sample)
        boots.append(agree / total)
    boots.sort()

    def _q(frac: float) -> float:
        return boots[min(boot - 1, int(frac * boot))] if boots else 0.0

    return RetestResult(
        hubs_a=len(hubs["a"]),
        hubs_b=len(hubs["b"]),
        hubs_shared=len(shared),
        n_both=n,
        raw_agreement=raw_same / n,
        folded_agreement=folded_same / n,
        dimension_agreement=dim_same / n,
        per_hub_folded=hub_mean,
        bootstrap=(_q(0.10), _q(0.50), _q(0.90)),
        disagreements=tuple((a, b, c) for (a, b), c in diffs.most_common(top)),
    )


def format_retest(r: RetestResult) -> str:
    lines = [
        f"hubs: A {r.hubs_a}, B {r.hubs_b}, shared {r.hubs_shared}",
        f"mentions both runs discovered: {r.n_both}",
        f"agreement: raw {r.raw_agreement:.3f}, folded "
        f"{r.folded_agreement:.3f}, dimension {r.dimension_agreement:.3f}",
        f"per-hub mean folded {r.per_hub_folded:.3f}; bootstrap p10 "
        f"{r.bootstrap[0]:.3f} p50 {r.bootstrap[1]:.3f} p90 {r.bootstrap[2]:.3f}",
    ]
    if r.disagreements:
        lines.append("top folded-key disagreements (A -> B):")
        for a, b, c in r.disagreements:
            lines.append(f"  {c:3d}  {a}  ->  {b}")
    return "\n".join(lines)


__all__ = ["RetestResult", "folded_agreement", "format_retest", "load_terms"]
