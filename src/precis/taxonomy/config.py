"""Campaign configuration — the only place a domain is allowed to appear.

``taxonomy-bootstrap.md`` AC8: no chemistry string outside the campaign YAML.
The stage modules import this and nothing domain-specific; a sociology
campaign ships a different YAML with different reference-state patterns and
currency codes, and no code changes.

Shipped campaign configs live in ``precis/data/taxonomy/campaigns/`` and load
by name; a path loads an unshipped one from the campaign scratch directory.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import Any, Final

import yaml

from precis.taxonomy.types import Snapshot, Thresholds

#: Bumped when a stage changes what it emits for unchanged input. Recorded in
#: every frozen list, so "same numbers, different procedure" is detectable.
#: 2 (2026-09-30): stage 3 folds measurand synonyms and canonicalises the
#: qualifier fields through the campaign vocabularies (blocker 4).
PROCEDURE_VERSION: Final[int] = 2

_CAMPAIGN_DIR: Final[str] = "taxonomy/campaigns"


@dataclass(frozen=True, slots=True)
class PhraseRule:
    """A campaign phrase that qualifies nearby values.

    ``pattern`` is a regex from the config; it is compiled case-insensitively
    because a reference electrode is written ``vs RHE`` and ``vs. rhe`` in the
    same corpus.
    """

    id: str
    pattern: re.Pattern[str]
    label: str = ""


@dataclass(frozen=True, slots=True)
class DomainClass:
    """One curated node of the ``material-class`` axis (or its analogue)."""

    id: str
    label: str
    parents: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MeasurandAliases:
    """Stage-3 synonym families for the measurand string (blocker 4).

    Both maps go canonical → variants, every string already in
    :func:`~precis.taxonomy.normalise.alias_key` form (lowercase, hyphen
    joined) so a rule reads the way the key it matches reads.

    ``species`` names the chemical species a measurand may carry as a
    qualifier (``NH3 yield rate`` / ``yield rate for NH3`` / ``ammonia yield
    rate``). :func:`~precis.taxonomy.normalise.fold_aliases` rewrites every
    variant to its canonical token and moves the species to the tail of
    the key, so the three spellings share one key. Two *different* species
    never fold — ``nh3`` and ``nh4`` stay two keys by design
    (``taxonomy-bootstrap.md`` blocker 4: the species is part of which
    quantity it is).

    ``phrases`` names whole-phrase synonyms (``production-rate`` ≡
    ``yield-rate``), matched on token boundaries inside the key.
    """

    species: dict[str, tuple[str, ...]] = field(default_factory=dict)
    phrases: dict[str, tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class QualifierVocabulary:
    """Canonical values for the model's free-text qualifier fields.

    Stage 2 answers ``reference_state`` / ``convention`` /
    ``normalisation_basis`` in prose (``"RHE"``, ``"reversible hydrogen
    electrode (RHE)"``, ``"electrode geometric area"``); stage 3 groups on
    those strings, so every spelling is a node split. Each map goes
    canonical id → variants, compared in ``alias_key`` form.

    ``reference_state`` and ``normalisation_basis`` are **open**: a value
    outside the list is kept (as its ``alias_key``) and the node carries a
    note, because dropping an unlisted reference electrode would merge two
    non-comparable potentials. ``convention`` is **closed**: a value outside
    the list is dropped to ``None`` with a note, because what the model
    puts there unprompted is sign and direction prose (``"cathodic
    (negative)"``, ``"closer to zero is more favorable"``) — a description
    of the number, not a convention in the AC5 sense that would make two
    values non-comparable.
    """

    reference_state: dict[str, tuple[str, ...]] = field(default_factory=dict)
    convention: dict[str, tuple[str, ...]] = field(default_factory=dict)
    normalisation_basis: dict[str, tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CampaignConfig:
    """Everything the stages need that is not universal.

    Nothing here is consulted by the number grammar itself — stage 1 finds
    numbers and asks pint whether the following token is a unit. These rules
    only add mention *types* (reference states, normalisation bases), extra
    unit definitions, and the curated axis nodes.
    """

    campaign: str
    config_version: int
    snapshot: Snapshot
    snapshot_path: Path
    thresholds: Thresholds
    unit_definitions: tuple[str, ...] = ()
    currency_codes: frozenset[str] = frozenset()
    glued_unit_denylist: frozenset[str] = frozenset()
    """Unit candidates to refuse when they sit glued to a number with no space.

    pint's SI-prefix machinery makes plausible-looking units out of domain
    jargon (`D` resolves to debye, so `2D` reads as a dipole moment), and a
    campaign knows its own jargon. Deliberately a campaign knob, not a library
    list: which single letters are labels rather than units is exactly the kind
    of thing that differs between a catalysis corpus and a sociology one."""
    reference_states: tuple[PhraseRule, ...] = ()
    normalisation_bases: tuple[PhraseRule, ...] = ()
    required_conditions_default: tuple[str, ...] = ()
    required_conditions_by_measurand: dict[str, tuple[str, ...]] = field(
        default_factory=dict
    )
    categorical_qualifiers: dict[str, tuple[str, ...]] = field(default_factory=dict)
    domain_axis: str = "material-class"
    domain_classes: tuple[DomainClass, ...] = ()
    domain_tag_prefix: str = ""
    domain_tag_separator: str = "-"
    site_classes: tuple[str, ...] = ()
    measurand_aliases: MeasurandAliases = field(default_factory=MeasurandAliases)
    qualifier_vocabulary: QualifierVocabulary = field(
        default_factory=QualifierVocabulary
    )

    def required_conditions(self, measurand_key: str) -> tuple[str, ...]:
        """Conditions a value of this measurand must carry.

        Replaces the pilot's hardcoded temperature guard with a per-measurand
        rule (``norr-her-meta.md`` decisions log, 2026-09-28): a rule the
        registry holds, not a chemistry patch in the extractor.
        """
        specific = self.required_conditions_by_measurand.get(measurand_key, ())
        merged = list(self.required_conditions_default)
        for condition in specific:
            if condition not in merged:
                merged.append(condition)
        return tuple(merged)


def load_campaign(name_or_path: str | Path) -> CampaignConfig:
    """Load a campaign config by shipped name or by filesystem path.

    A bare name (``"norr-her-meta"``) resolves against the packaged campaign
    directory; anything containing a separator or ending ``.yaml`` is read as
    a path, so an in-progress campaign can live in the scratch directory
    without being committed.
    """
    raw = _read_config_text(name_or_path)
    data = yaml.safe_load(raw)
    if not isinstance(data, dict):
        raise ValueError(f"campaign config is not a mapping: {name_or_path!r}")
    return _parse(data)


def _read_config_text(name_or_path: str | Path) -> str:
    text = str(name_or_path)
    looks_like_path = (
        isinstance(name_or_path, Path) or "/" in text or text.endswith(".yaml")
    )
    if looks_like_path:
        return Path(text).expanduser().read_text(encoding="utf-8")
    resource = resources.files("precis.data").joinpath(f"{_CAMPAIGN_DIR}/{text}.yaml")
    return resource.read_text(encoding="utf-8")


def _parse(data: dict[str, Any]) -> CampaignConfig:
    snap = data.get("snapshot") or {}
    missing = [k for k in ("row_count", "sha256", "pulled_at") if k not in snap]
    if missing:
        raise ValueError(
            "campaign snapshot must pin its identity — missing "
            + ", ".join(missing)
            + " (a list read without its snapshot identity is unreproducible)"
        )
    pulled_at = _utc_z(snap["pulled_at"])
    snapshot = Snapshot(
        source=str(snap.get("source", "")),
        row_count=int(snap["row_count"]),
        sha256=str(snap["sha256"]),
        pulled_at=pulled_at,
        text_field=str(snap.get("text_field", "text")),
        ref_field=str(snap.get("ref_field", "ref_id")),
        paper_field=str(snap.get("paper_field", "")),
    )
    conditions = data.get("required_conditions") or {}
    domain = data.get("domain_classes") or {}
    return CampaignConfig(
        campaign=str(data.get("campaign", "unnamed")),
        config_version=int(data.get("config_version", 1)),
        snapshot=snapshot,
        snapshot_path=Path(str(snap.get("path", ""))).expanduser(),
        thresholds=_parse_thresholds(data.get("thresholds") or {}),
        unit_definitions=tuple(str(d) for d in data.get("unit_definitions") or ()),
        currency_codes=frozenset(str(c) for c in data.get("currency_codes") or ()),
        glued_unit_denylist=frozenset(
            str(c) for c in data.get("glued_unit_denylist") or ()
        ),
        reference_states=_parse_phrases(data.get("reference_states") or ()),
        normalisation_bases=_parse_phrases(data.get("normalisation_bases") or ()),
        required_conditions_default=tuple(
            str(c) for c in conditions.get("default") or ()
        ),
        required_conditions_by_measurand={
            str(k): tuple(str(c) for c in v)
            for k, v in (conditions.get("by_measurand") or {}).items()
        },
        categorical_qualifiers={
            str(k): tuple(str(v) for v in vals)
            for k, vals in (data.get("categorical_qualifiers") or {}).items()
        },
        domain_axis=str(domain.get("axis", "material-class")),
        domain_classes=tuple(
            DomainClass(
                id=str(node["id"]),
                label=str(node.get("label", node["id"])),
                parents=tuple(str(p) for p in node.get("parents") or ()),
            )
            for node in domain.get("nodes") or ()
        ),
        domain_tag_prefix=str(domain.get("tag_prefix", "")),
        domain_tag_separator=str(domain.get("tag_separator", "-")),
        site_classes=tuple(str(s) for s in data.get("site_classes") or ()),
        measurand_aliases=_parse_measurand_aliases(data.get("measurand_aliases") or {}),
        qualifier_vocabulary=_parse_qualifier_vocabulary(
            data.get("qualifier_vocabulary") or {}
        ),
    )


def _parse_synonym_map(section: str, raw: Any) -> dict[str, tuple[str, ...]]:
    """``canonical: [variants...]`` → dict, refusing a variant that is also a
    canonical or that appears under two canonicals — either would make the
    fold order-dependent, and the whole point of the map is determinism."""
    if not isinstance(raw, dict):
        raise ValueError(f"{section} must be a mapping of canonical -> variants")
    out: dict[str, tuple[str, ...]] = {}
    seen: dict[str, str] = {}
    for canonical, variants in raw.items():
        canonical = str(canonical)
        values = tuple(str(v) for v in (variants or ()))
        for variant in values:
            owner = seen.get(variant)
            if owner is not None and owner != canonical:
                raise ValueError(
                    f"{section}: variant {variant!r} listed under both "
                    f"{owner!r} and {canonical!r}"
                )
            if variant in raw and variant != canonical:
                raise ValueError(
                    f"{section}: {variant!r} is both a canonical and a variant of "
                    f"{canonical!r}"
                )
            seen[variant] = canonical
        out[canonical] = values
    return out


def _parse_measurand_aliases(raw: Any) -> MeasurandAliases:
    if not isinstance(raw, dict):
        raise ValueError("measurand_aliases must be a mapping")
    return MeasurandAliases(
        species=_parse_synonym_map(
            "measurand_aliases.species", raw.get("species") or {}
        ),
        phrases=_parse_synonym_map(
            "measurand_aliases.phrases", raw.get("phrases") or {}
        ),
    )


def _parse_qualifier_vocabulary(raw: Any) -> QualifierVocabulary:
    if not isinstance(raw, dict):
        raise ValueError("qualifier_vocabulary must be a mapping")
    return QualifierVocabulary(
        reference_state=_parse_synonym_map(
            "qualifier_vocabulary.reference_state", raw.get("reference_state") or {}
        ),
        convention=_parse_synonym_map(
            "qualifier_vocabulary.convention", raw.get("convention") or {}
        ),
        normalisation_basis=_parse_synonym_map(
            "qualifier_vocabulary.normalisation_basis",
            raw.get("normalisation_basis") or {},
        ),
    )


def _utc_z(raw: object) -> str:
    """Normalise a config timestamp to ``…Z``, refusing anything not UTC.

    PyYAML resolves an unquoted ISO timestamp to a ``datetime`` before this
    code sees it, so a config may hand over either form. Timestamps are UTC
    and labelled (`docs/conventions/time.md`); a naive datetime or a local
    offset is a config bug, not something to coerce.
    """
    if isinstance(raw, datetime):
        if raw.tzinfo is None or raw.utcoffset() != timedelta(0):
            raise ValueError(f"snapshot.pulled_at must be UTC, got {raw.isoformat()}")
        return raw.strftime("%Y-%m-%dT%H:%M:%SZ")
    text = str(raw)
    if not text.endswith("Z"):
        raise ValueError(
            f"snapshot.pulled_at must be UTC with a Z suffix, got {text!r}"
        )
    return text


def _parse_thresholds(raw: dict[str, Any]) -> Thresholds:
    """Apply campaign overrides, refusing any that loosen a default.

    Signing off thresholds only means something if a campaign cannot quietly
    relax them: a looser knob has to be a new signed default, not a line in a
    campaign file.
    """
    defaults = Thresholds()
    tighter_is_larger = {
        "min_papers",
        "min_hubs",
        "min_join_side",
        "min_stability",
    }
    tighter_is_smaller = {"max_escape_rate"}
    kwargs: dict[str, Any] = {}
    for key, value in raw.items():
        if key not in tighter_is_larger | tighter_is_smaller | {
            "require_both_halves",
            "require_single_dimension",
        }:
            raise ValueError(f"unknown threshold {key!r}")
        current = getattr(defaults, key)
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(
                f"threshold {key} must be a finite number, got {value!r} — "
                "a non-finite bound silently disables the gate it is supposed "
                "to tighten (NaN compares False against every bound, so it "
                "passes this check and then never fires downstream)"
            )
        if key in tighter_is_larger and value < current:
            raise ValueError(
                f"campaign may only tighten {key}: {value} is looser than the "
                f"signed default {current}"
            )
        if key in tighter_is_smaller and value > current:
            raise ValueError(
                f"campaign may only tighten {key}: {value} is looser than the "
                f"signed default {current}"
            )
        if key in {"require_both_halves", "require_single_dimension"} and not value:
            raise ValueError(f"campaign may not switch off {key}")
        kwargs[key] = value
    return Thresholds(**kwargs)


def _parse_phrases(rows: Any) -> tuple[PhraseRule, ...]:
    out: list[PhraseRule] = []
    for row in rows:
        if "pattern" in row:
            patterns = [row["pattern"]]
        else:
            patterns = list(row.get("patterns") or ())
        if not patterns:
            raise ValueError(f"phrase rule {row.get('id')!r} has no pattern")
        joined = "|".join(f"(?:{p})" for p in patterns)
        out.append(
            PhraseRule(
                id=str(row["id"]),
                pattern=re.compile(joined, re.IGNORECASE),
                label=str(row.get("label", "")),
            )
        )
    return tuple(out)
