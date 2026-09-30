"""Wire the five stages into one rerunnable pass over a snapshot.

Nothing here decides anything — every rule lives in the stage modules and
every number in the campaign config. This module exists so that "run the
procedure" is one call with one set of inputs, which is what makes the output
reproducible by someone who was not in the room: a campaign name, a salt, and
an output directory.

The discovery stage costs money, so it is the only stage that can be skipped
(``--stage census``). Everything before it is deterministic and everything
after it is arithmetic.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from precis.taxonomy import census, discovery, freeze, normalise, select
from precis.taxonomy.config import PROCEDURE_VERSION, CampaignConfig
from precis.taxonomy.discovery import CallRecord
from precis.taxonomy.normalise import MergeSuggestion
from precis.taxonomy.types import (
    DiscoveredTerm,
    HubCount,
    ListEntry,
    Mention,
    Snapshot,
    TermNode,
)


@dataclass(frozen=True, slots=True)
class RunResult:
    """Everything one pass produced, including what it refused.

    The rejected and warning collections are first-class rather than logged
    because a generated list is only trustworthy if it can say why something
    is missing.
    """

    snapshot: Snapshot
    census_digest: str
    mentions: tuple[Mention, ...]
    terms: tuple[DiscoveredTerm, ...]
    warnings: tuple[str, ...]
    nodes: tuple[TermNode, ...]
    suggestions: tuple[MergeSuggestion, ...]
    stability: float
    entries: tuple[ListEntry, ...]
    rejected: tuple[tuple[str, str], ...]
    #: :func:`select.unit_key_ceiling` on the same terms — what the
    #: stability is read against at probe size.
    unit_ceiling: float = 1.0
    #: One row per discovery call, failed calls included (``responses.jsonl``).
    responses: tuple[CallRecord, ...] = ()

    @property
    def stability_ratio(self) -> float:
        """Stability as a fraction of the unit-key ceiling (the probe number)."""
        if self.unit_ceiling <= 0:
            return 1.0 if self.stability <= 0 else float("inf")
        return self.stability / self.unit_ceiling

    def summary(self) -> str:
        promoted = sum(1 for n in self.nodes if n.status == "systematic")
        return (
            f"snapshot {self.snapshot.row_count} rows "
            f"sha {self.snapshot.sha256[:12]}\n"
            f"mentions {len(self.mentions)} (digest {self.census_digest[:12]})\n"
            f"discovered rows {len(self.terms)}, warnings {len(self.warnings)}\n"
            f"{metering_line(self.responses)}\n"
            f"nodes {len(self.nodes)} ({promoted} systematic), "
            f"merge suggestions {len(self.suggestions)}\n"
            f"A/B vocabulary stability {self.stability:.3f} "
            f"({self.stability_ratio:.2f} of the unit-key ceiling "
            f"{self.unit_ceiling:.3f})\n"
            f"entries {len(self.entries)}, rejected {len(self.rejected)}"
        )


def metering_line(responses: Sequence[CallRecord]) -> str:
    """One line of what the discovery calls cost, ``?`` where unreported.

    A sum is over the rows that reported the field; a field nobody reported
    prints ``?`` rather than ``0`` — a zero here would read as "free", and
    the whole point of the row is that the run is not.
    """
    failed = sum(1 for r in responses if r.error is not None)
    wall = sum(r.duration_s for r in responses)

    def total(values: Sequence[float]) -> str:
        return "?" if not values else f"{sum(values):g}"

    cost = [r.cost_usd for r in responses if r.cost_usd is not None]
    fields = (
        ("in", [r.input_tokens for r in responses if r.input_tokens is not None]),
        ("out", [r.output_tokens for r in responses if r.output_tokens is not None]),
        (
            "cache-read",
            [r.cache_read_tokens for r in responses if r.cache_read_tokens is not None],
        ),
        (
            "cache-write",
            [
                r.cache_creation_tokens
                for r in responses
                if r.cache_creation_tokens is not None
            ],
        ),
    )
    tokens = " / ".join(f"{name} {total(values)}" for name, values in fields)
    return (
        f"discovery calls {len(responses)} ({failed} failed), "
        f"cost ${total(cost)}, tokens {tokens}, wall {wall:.0f} s"
    )


def run_census(
    config: CampaignConfig, *, limit: int | None = None
) -> tuple[tuple[dict[str, object], ...], tuple[Mention, ...], str]:
    """Stage 1 against the snapshot the config pins.

    Verifies the snapshot identity first: scanning a file whose row count or
    sha256 disagrees with the config would produce numbers attributed to a
    corpus state that never existed.
    """
    path = config.snapshot_path
    census.verify_snapshot(path, config.snapshot)
    rows = census.load_snapshot_rows(path)
    if limit is not None:
        rows = rows[:limit]
    mentions = census.scan_snapshot(rows, config)
    return rows, mentions, census.census_digest(mentions)


def mentions_by_ref(
    mentions: Sequence[Mention],
    *,
    kinds: tuple[str, ...] = ("value",),
) -> dict[int, tuple[Mention, ...]]:
    """Group mentions by source row, keeping only the kinds worth labelling.

    Defaults to ``value`` only. Stage 1 also emits ``reference_state`` and
    ``normalisation_basis`` mentions, and handing those to discovery would ask
    the model to name a measurand for a phrase that by construction has none
    (`types.MentionKind`) — spending a paid call to manufacture a row that then
    pollutes stages 3 and 4. The qualifier mentions are not lost: they are
    structurally captured by stage 1 with their config marker, and the model
    still sees them in the row's text.
    """
    grouped: dict[int, list[Mention]] = defaultdict(list)
    for mention in mentions:
        if mention.kind not in kinds:
            continue
        grouped[mention.anchor.source_ref_id].append(mention)
    return {ref: tuple(items) for ref, items in sorted(grouped.items())}


def split_sides(raw: object) -> tuple[str, ...]:
    """Split a possibly multi-valued tag field into its individual values.

    Production rows carry pipe-joined tags (``mode:dft|mode:mixed``,
    ``side:h2-generation|side:stability-feed`` on 84 hubs). Treating the
    compound string as one opaque side means such a hub counts toward neither
    real side, silently undercounting both against the per-side join threshold.
    """
    text = str(raw or "").strip()
    if not text:
        return ("unknown",)
    return tuple(sorted({p.strip() for p in text.split("|") if p.strip()}))


def papers_by_ref(
    rows: Sequence[Mapping[str, object]], config: CampaignConfig
) -> dict[int, frozenset[int]]:
    """Map each scanned row to the paper refs it came from.

    Empty when the campaign sets no ``paper_field``, which means the rows are
    themselves papers and the row id already is the paper id.
    """
    field_name = config.snapshot.paper_field
    if not field_name:
        return {}
    ref_field = config.snapshot.ref_field
    out: dict[int, frozenset[int]] = {}
    for row in rows:
        try:
            ref = int(str(row.get(ref_field, "")))
        except ValueError:
            continue
        raw = row.get(field_name) or ()
        ids: set[int] = set()
        for item in raw if isinstance(raw, (list, tuple)) else ():
            try:
                ids.add(int(item))
            except (TypeError, ValueError):
                continue
        out[ref] = frozenset(ids)
    return out


def attribute_papers(
    nodes: Sequence[TermNode], papers: Mapping[int, frozenset[int]]
) -> tuple[TermNode, ...]:
    """Replace each node's ``paper_ref_ids`` with the real source papers.

    `normalise` fills that field from the mention row ids, which is right when
    the scanned rows are papers and wrong here: these rows are claim hubs, and
    the usage test promotes on ">=3 independent papers". Three hubs of one
    paper must not read as three papers. A no-op when the campaign gives no
    paper field.
    """
    if not papers:
        return tuple(nodes)
    out: list[TermNode] = []
    for node in nodes:
        found: set[int] = set()
        for anchor in node.anchors:
            found |= papers.get(anchor.source_ref_id, frozenset())
        out.append(replace(node, paper_ref_ids=frozenset(found)))
    return tuple(out)


def hub_counts(
    nodes: Sequence[TermNode],
    rows: Sequence[Mapping[str, object]],
    config: CampaignConfig,
    *,
    side_field: str = "mode",
) -> dict[tuple[str, ...], HubCount]:
    """Hub evidence per node, keyed by the node's full compare identity.

    Computed from each node's own anchors rather than from the discovered rows,
    because a bare measurand key is **not** unique: AC5 requires two `TOF`
    nodes, and reference-state / convention / normalisation-basis splits each
    produce siblings under one key. Keyed or counted by key, every sibling
    would read the union of the others' evidence.

    ``total`` counts hubs, not mentions — three numbers in one sentence are one
    hub's worth of evidence for the ``>=30 hubs`` threshold.
    """
    ref_field = config.snapshot.ref_field
    sides_by_ref: dict[int, tuple[str, ...]] = {}
    for row in rows:
        try:
            ref = int(str(row.get(ref_field, "")))
        except ValueError:
            continue
        sides_by_ref[ref] = split_sides(row.get(side_field))

    papers = papers_by_ref(rows, config)
    out: dict[tuple[str, ...], HubCount] = {}
    for node in nodes:
        refs = sorted({a.source_ref_id for a in node.anchors})
        side_counts: Counter[str] = Counter()
        for ref in refs:
            for side in sides_by_ref.get(ref, ("unknown",)):
                side_counts[side] += 1
        paper_ids: set[int] = set()
        for ref in refs:
            paper_ids |= papers.get(ref, frozenset())
        out[node.identity()] = HubCount(
            total=len(refs),
            by_side=dict(sorted(side_counts.items())),
            ref_ids=tuple(refs),
            paper_ref_ids=frozenset(paper_ids),
        )
    return out


def run_pipeline(
    config: CampaignConfig,
    client: discovery.DiscoveryClient,
    *,
    salt: str,
    limit: int | None = None,
    join_sides: tuple[str, str] | None = None,
    side_field: str = "mode",
    on_call: Callable[[CallRecord], None] | None = None,
) -> RunResult:
    """Stages 1-4. Freezing is a separate, explicit act (see :func:`freeze_run`).

    ``on_call`` is forwarded to :func:`discovery.discover` so a caller can
    stream the call records to disk as they arrive; the result carries the
    full tuple regardless.
    """
    rows, mentions, digest = run_census(config, limit=limit)
    by_ref = mentions_by_ref(mentions)
    halves = discovery.split_halves(sorted(by_ref), salt=salt)
    responses: list[CallRecord] = []

    def collect(record: CallRecord) -> None:
        responses.append(record)
        if on_call is not None:
            on_call(record)

    terms, warnings = discovery.discover(
        rows, by_ref, config, client, halves=halves, on_call=collect
    )
    registry = census.build_registry(config)
    nodes, suggestions = normalise.normalise(terms, registry, config)
    # Papers before promotion: the usage test's paper count is only meaningful
    # once the nodes carry real paper refs rather than hub refs.
    nodes = attribute_papers(nodes, papers_by_ref(rows, config))
    nodes = select.promote(nodes, config.thresholds)
    stability = select.vocabulary_stability(terms, config)
    unit_ceiling = select.unit_key_ceiling(terms)
    counts = hub_counts(nodes, rows, config, side_field=side_field)
    entries, rejected = select.select_entries(
        nodes, counts, config.thresholds, config, join_sides=join_sides
    )
    return RunResult(
        snapshot=config.snapshot,
        census_digest=digest,
        mentions=mentions,
        terms=terms,
        warnings=warnings,
        nodes=nodes,
        suggestions=suggestions,
        stability=stability,
        entries=entries,
        rejected=rejected,
        unit_ceiling=unit_ceiling,
        responses=tuple(responses),
    )


def freeze_run(result: RunResult, config: CampaignConfig, directory: Path) -> Path:
    """Write the next ``list.vN.yaml``, refusing to touch an existing one.

    Gated on stability: a run below the signed threshold must not produce a
    frozen list at all, because the list would then be the artifact of a
    prompt that is known not to reproduce. The fix is the prompt, not the
    threshold (``taxonomy-bootstrap.md`` §Thresholds).
    """
    if result.stability < config.thresholds.min_stability:
        raise ValueError(
            f"A/B vocabulary stability {result.stability:.3f} is below the "
            f"signed threshold {config.thresholds.min_stability} — fix the "
            "discovery prompt and rerun; do not freeze this list"
        )
    directory.mkdir(parents=True, exist_ok=True)
    version = freeze.next_version(directory)
    lst = freeze.freeze(
        result.entries,
        campaign=config.campaign,
        snapshot=result.snapshot,
        thresholds=config.thresholds,
        version=version,
        stability=result.stability,
        rejected=result.rejected,
    )
    return freeze.write_list(lst, directory, census_digest=result.census_digest)


def write_stage_outputs(result: RunResult, directory: Path) -> tuple[Path, ...]:
    """Dump the intermediate stages as JSONL so a run can be audited.

    These are working files, not the artifact: the artifact is the frozen
    list. They exist because "why did this term not make it" is answered by
    the mention, not by the summary.
    """
    directory.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, payload in (
        ("mentions.jsonl", [m.to_json() for m in result.mentions]),
        ("discovered.jsonl", [t.to_json() for t in result.terms]),
        ("nodes.jsonl", [n.to_json() for n in result.nodes]),
        (
            "suggestions.jsonl",
            [
                {
                    "left": s.left,
                    "right": s.right,
                    "similarity": s.similarity,
                    "reason": s.reason,
                }
                for s in result.suggestions
            ],
        ),
        ("rejected.jsonl", [{"key": k, "reason": r} for k, r in result.rejected]),
        ("warnings.jsonl", [{"warning": w} for w in result.warnings]),
        ("responses.jsonl", [r.to_json() for r in result.responses]),
    ):
        path = directory / name
        with path.open("w", encoding="utf-8") as handle:
            for row in payload:
                handle.write(json.dumps(row, sort_keys=True, ensure_ascii=False))
                handle.write("\n")
        written.append(path)
    return tuple(written)


def procedure_version() -> int:
    return PROCEDURE_VERSION
