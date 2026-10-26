"""Design knowledge-tree manifests — slice S0 (format) of the design graph.

Authoring form for the design knowledge graph (gr516958, gr516957): YAML
manifests under ``src/precis/data/design-tree/`` hold one or more goal
roots, each a nested tree of node records (``children:`` maps a child's
path key to its record). This module owns the schema — the field
vocabularies, the ``function``/``branch`` pilot aliases for
``goal``/``method``, and the two graph invariants the importer and the
export rely on:

- the ``specialises`` backbone (primary parents + ``also_under`` extra
  parents) is a DAG — multi-parent is legal, a cycle is refused;
- every ``also_under`` or ``links`` target names a node that exists in
  the forest (``links`` mint ``related-to`` edges, which *may* cycle —
  the walk never descends them);
- the shared-vocabulary references resolve the same way: an ``axes``
  key names an ``axis/<key>`` node, an issue's ``failure_mode`` names a
  ``failure/<name>`` node (both mint ``related-to`` edges with
  ``{as: axis|mode}`` meta), and an ``asks`` entry names a
  ``criterion/<name>`` node (vocabulary only — no edge rides on it).

Everything here is pure: no DB, no store — ``precis.cli.conventions``
turns a validated :class:`DesignTree` into finding refs and edges, and
:func:`export_rules` renders the flat ``design_rules.json`` that
``precis_se.joints`` (``params.leaf``) and the DRC slices read. The export
is generated from the manifests, never from the live graph. A node
``conflicts`` block — sources genuinely disagree on a topic — rides the
export verbatim; its ``rules:`` names the local rule ids it covers, and
the export marks those rules (and every forest rule sharing a marked
rule's ``table_ref``) ``conflicted`` so DRC never enforces them.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import cache, lru_cache
from importlib import resources
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Vocabulary (SPEC §2.1–2.2)
# ---------------------------------------------------------------------------

#: Canonical node roles. ``rule`` is a legal *dtree* role (imported rule
#: refs carry ``dt:rule``) though manifests hang rules off their node
#: rather than declaring them as nodes.
ROLES = frozenset(
    {
        "goal",
        "method",
        "leaf",
        "process",
        "system",
        "axis",
        "principle",
        "failure-mode",
        "criterion",
        "rule",
    }
)

#: Pilot names the draft manifests still use; normalised on load.
ROLE_ALIASES = {"function": "goal", "branch": "method"}

COVERAGE = frozenset({"stub", "shallow", "deep"})

#: ``rule.equipment`` — the equipment class a rule is scoped to.
EQUIPMENT = frozenset(
    {
        "desktop-fdm",
        "industrial-fdm",
        "powder-bed",
        "sla",
        "cnc-mill",
        "lathe",
        "injection-mould",
        "sheet",
        "cast",
        "bought",
        "any",
    }
)

#: ``rule.confidence`` — claim verification tier. The last three are the
#: molecular-domain additions; ``shop-rule`` is never DRC-enforced.
CONFIDENCE = frozenset(
    {
        "standard",
        "handbook",
        "secondary",
        "excerpt-only",
        "shop-rule",
        "unverified",
        "measured",
        "computed",
        "proposed",
    }
)

SOURCE_QUALITY = frozenset({"primary", "secondary", "weak", "none", "n/a"})

#: ``principle`` (SPEC §2.2, join tree only) and ``directness`` vocab.
PRINCIPLE = frozenset({"none", "material-bond", "form", "force"})
DIRECTNESS = frozenset({"direct", "indirect"})

#: Node fields the schema knows (SPEC §2.2 + the equipment/conflicts
#: rulings).
_NODE_FIELDS = frozenset(
    {
        "id",
        "role",
        "domain",
        "scale",
        "equipment",
        "coverage",
        "def",
        "summary",
        "asks",
        "when",
        "avoid_when",
        "variants",
        "tradeoffs",
        "howto",
        "checks",
        "issues",
        "axes",
        "implies",
        "also_under",
        "sources",
        "conflicts",
        "links",
        "children",
        "rules",
        "principle",
        "directness",
        "applies",
        "literature",
        "authored",
        "note",
    }
)

_RULE_FIELDS = frozenset(
    {
        "id",
        "text",
        "value",
        "unit",
        "equipment",
        "confidence",
        "source_url",
        "standard",
        "source_quality",
        "corrected",
        "table_ref",
        "machine_specific",
        "measured_on",
        "note",
        "enforcers",
    }
)

_ISSUE_FIELDS = frozenset({"issue", "fix", "failure_mode"})
_ENFORCER_FIELDS = frozenset({"path", "what"})

#: ``implies`` on a leaf: the joint a connect realising this leaf
#: carries — ``class`` + optional ``mechanism`` over the
#: ``precis_se.joints`` vocabulary (checked against it by the test
#: suite, not here: joints.py already imports this module), plus an
#: optional integer ``rank`` ordering the candidates inside a crowded
#: class+mechanism tier (lower = more general default; the hint picks
#: at most 3).
_IMPLIES_FIELDS = frozenset({"class", "mechanism", "rank"})

#: ``conflicts`` record (final ruling): sources genuinely disagree on
#: ``topic``; ``values`` lists the disputed values with the source
#: attribution inlined into each string; ``reading`` says how to read
#: the disagreement; ``rules`` names the local rule ids the conflict
#: covers (the export marks those rules ``conflicted`` so DRC never
#: enforces them). All four keys are required and no others are allowed.
_CONFLICT_FIELDS = frozenset({"topic", "values", "reading", "rules"})

#: Rule ids that would collide with the minted issue/fix keys
#: ``<node>#i<n>`` / ``<node>#f<n>``.
_RULE_ID_RESERVED = re.compile(r"^[if][0-9]+$")

_STR_LIST_FIELDS = frozenset(
    {
        "asks",
        "variants",
        "tradeoffs",
        "howto",
        "checks",
        "sources",
        "links",
        "also_under",
        "literature",
    }
)

#: ``related-to`` link meta ``as`` value by target role (SPEC §2.3).
RELATED_AS = {
    "axis": "axis",
    "failure-mode": "mode",
    "principle": "principle",
    "process": "process",
    "system": "system",
}


class DesignTreeError(ValueError):
    """A manifest is malformed; the import/export refuses."""


@dataclass(frozen=True)
class Node:
    """One manifest node. ``record`` is the raw manifest mapping (minus
    ``children``); ``parents`` are the primary parent first, then
    ``also_under`` extra parents in manifest order.
    ``domain``/``scale`` are *effective* values — manifests declare them
    on roots and they inherit down the specialises DAG."""

    id: str
    role: str  # normalised (aliases resolved)
    record: dict[str, Any]
    parents: tuple[str, ...] = ()
    manifest: str = ""  # source file name, for provenance/error text
    domain: str | None = None
    scale: str | None = None
    equipment: str | None = None  # effective; declared on processes only


@dataclass(frozen=True)
class Edge:
    """One graph edge between dtree keys (node ids or derived ``x#y``
    / ``src:`` / ``enf:`` keys). ``relation`` is the closed-set slug;
    ``meta`` merges into the link meta (e.g. ``{'as': 'axis'}``)."""

    src: str
    dst: str
    relation: str
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Item:
    """A mintable record: one finding ref per item, keyed ``meta.dtree.id``."""

    key: str
    role: str  # dtree role -> ``dt:<role>`` tag + ``meta.dtree.role``
    title: str
    fields: dict[str, Any]  # record rendered into the body chunk
    node_id: str  # owning node id (== key for nodes) — carries the domain


class DesignTree:
    """A validated forest: ``nodes`` by id, plus the flat item/edge lists
    the importer mints refs/links from."""

    def __init__(
        self,
        nodes: dict[str, Node],
        items: list[Item],
        edges: list[Edge],
    ) -> None:
        self.nodes = nodes
        self.items = items
        self.edges = edges

    def leaves(self) -> list[str]:
        """Sorted ids of ``role='leaf'`` nodes — the ``params.leaf``
        vocabulary ``design_rules.json`` carries."""
        return sorted(n.id for n in self.nodes.values() if n.role == "leaf")


# ---------------------------------------------------------------------------
# Loading + validation
# ---------------------------------------------------------------------------


def load_files(paths: list[Path] | tuple[Path, ...]) -> DesignTree:
    """Parse and validate a set of manifest files into one forest.

    Top-level mapping entries whose value is a node record (a dict with
    ``id``/``role``/``children``) are goal roots; anything else is
    manifest header prose and ignored.
    """
    import yaml

    nodes: dict[str, Node] = {}
    for path in paths:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise DesignTreeError(f"{path}: top level must be a mapping")
        for key, raw in data.items():
            if not isinstance(raw, dict) or not (
                {"id", "role", "children"} & raw.keys()
            ):
                continue  # manifest header (status/ai_use/version)
            _walk(raw, parent=None, key=str(key), source=path.name, out=nodes)
    return _finish(nodes)


def load_dir(directory: Path) -> DesignTree:
    """Load every ``*.yaml`` in a manifest directory."""
    paths = sorted(Path(directory).glob("*.yaml"))
    if not paths:
        raise DesignTreeError(f"{directory}: no *.yaml manifests")
    return load_files(tuple(paths))


def _walk(
    raw: dict[str, Any],
    *,
    parent: Node | None,
    key: str,
    source: str,
    out: dict[str, Node],
) -> None:
    where = f"{source}:{key}"
    node_id = raw.get("id")
    if not isinstance(node_id, str) or not node_id.strip():
        raise DesignTreeError(f"{where}: nonempty string 'id' required")
    node_id = node_id.strip()
    if parent is not None and node_id != f"{parent.id}/{key}":
        raise DesignTreeError(
            f"{where}: id {node_id!r} must equal its path "
            f"{parent.id}/{key} — the id is the stable path slug"
        )
    if node_id in out:
        raise DesignTreeError(f"{where}: duplicate id {node_id!r}")
    role_raw = raw.get("role")
    if not isinstance(role_raw, str) or not role_raw.strip():
        raise DesignTreeError(f"{where} ({node_id}): nonempty string 'role' required")
    role = ROLE_ALIASES.get(role_raw.strip(), role_raw.strip())
    if role not in ROLES:
        raise DesignTreeError(
            f"{where} ({node_id}): role must be one of "
            f"{', '.join(sorted(ROLES | set(ROLE_ALIASES)))}; got {role_raw!r}"
        )
    unknown = set(raw) - _NODE_FIELDS
    if unknown:
        raise DesignTreeError(
            f"{where} ({node_id}): unknown field(s) {', '.join(sorted(unknown))}"
        )
    _vet_fields(node_id, role, raw, where)
    node = Node(
        id=node_id,
        role=role,
        record={k: v for k, v in raw.items() if k != "children"},
        parents=(parent.id,) if parent else (),
        manifest=source,
    )
    out[node_id] = node
    for child_key, child_raw in (raw.get("children") or {}).items():
        if not isinstance(child_raw, dict):
            raise DesignTreeError(
                f"{source}:{node_id}/children/{child_key}: must be a mapping"
            )
        _walk(child_raw, parent=node, key=str(child_key), source=source, out=out)


def _vet_fields(node_id: str, role: str, raw: dict[str, Any], where: str) -> None:
    """Type/vocabulary checks on the optional fields of one node."""
    equip = raw.get("equipment")
    if equip is not None:
        if role not in ("process", "leaf"):
            raise DesignTreeError(
                f"{where} ({node_id}): equipment is only allowed on "
                "role=process or role=leaf nodes"
            )
        if equip not in EQUIPMENT:
            raise DesignTreeError(
                f"{where} ({node_id}): equipment must be one of "
                f"{', '.join(sorted(EQUIPMENT))}; got {equip!r}"
            )
    cov = raw.get("coverage")
    if cov is not None and cov not in COVERAGE:
        raise DesignTreeError(
            f"{where} ({node_id}): coverage must be one of "
            f"{', '.join(sorted(COVERAGE))}; got {cov!r}"
        )
    for name in (
        "domain",
        "scale",
        "def",
        "summary",
        "principle",
        "directness",
        "applies",
        "authored",
        "note",
    ):
        val = raw.get(name)
        if val is not None and not isinstance(val, str):
            raise DesignTreeError(f"{where} ({node_id}): {name} must be a string")
    for name in _STR_LIST_FIELDS:
        val = raw.get(name)
        if val is not None and (
            not isinstance(val, list) or not all(isinstance(x, str) for x in val)
        ):
            raise DesignTreeError(
                f"{where} ({node_id}): {name} must be a list of strings"
            )
    for name in ("when", "avoid_when"):
        val = raw.get(name)
        if val is not None and not (
            isinstance(val, str)
            or (isinstance(val, list) and all(isinstance(x, str) for x in val))
        ):
            raise DesignTreeError(
                f"{where} ({node_id}): {name} must be a string or list of strings"
            )
    for name in ("axes", "implies"):
        val = raw.get(name)
        if val is not None and not isinstance(val, dict):
            raise DesignTreeError(f"{where} ({node_id}): {name} must be a mapping")
    implies = raw.get("implies")
    if implies is not None:
        if role != "leaf":
            raise DesignTreeError(
                f"{where} ({node_id}): implies is only allowed on "
                "role=leaf nodes — it names the joint a connect "
                "realising this leaf carries"
            )
        bad_implies = set(implies) - _IMPLIES_FIELDS
        if bad_implies:
            raise DesignTreeError(
                f"{where} ({node_id}): implies unknown field(s) "
                f"{', '.join(sorted(bad_implies))}"
            )
        klass = implies.get("class")
        if not isinstance(klass, str) or not klass.strip():
            raise DesignTreeError(
                f"{where} ({node_id}): implies 'class' must be a "
                "nonempty string (a precis_se.joints kinematic class)"
            )
        mech = implies.get("mechanism")
        if mech is not None and (not isinstance(mech, str) or not mech.strip()):
            raise DesignTreeError(
                f"{where} ({node_id}): implies 'mechanism' must be a "
                "nonempty string when present"
            )
        rank = implies.get("rank")
        if rank is not None and (isinstance(rank, bool) or not isinstance(rank, int)):
            raise DesignTreeError(
                f"{where} ({node_id}): implies 'rank' must be an integer when present"
            )
    prin = raw.get("principle")
    if prin is not None:
        if node_id != "join" and not node_id.startswith("join/"):
            raise DesignTreeError(
                f"{where} ({node_id}): principle is only allowed on the join tree"
            )
        if prin not in PRINCIPLE:
            raise DesignTreeError(
                f"{where} ({node_id}): principle must be one of "
                f"{', '.join(sorted(PRINCIPLE))}; got {prin!r}"
            )
    direct = raw.get("directness")
    if direct is not None and direct not in DIRECTNESS:
        raise DesignTreeError(
            f"{where} ({node_id}): directness must be one of "
            f"{', '.join(sorted(DIRECTNESS))}; got {direct!r}"
        )
    # domain/scale become tags (domain:<d>) — vet tag-safety now so bad
    # input fails as a DesignTreeError before any DB work. Lowercase is
    # required: Tag.open lowercases values silently, so an uppercase
    # declaration would drift from the minted tag.
    from precis.errors import BadInput
    from precis.store.types import Tag

    for name in ("domain", "scale"):
        val = raw.get(name)
        if val is None:
            continue
        if val != val.strip().lower():
            raise DesignTreeError(
                f"{where} ({node_id}): {name} must be lowercase; got {val!r}"
            )
        try:
            Tag.parse_strict(val)
        except BadInput as exc:
            raise DesignTreeError(
                f"{where} ({node_id}): {name} must be a tag-safe slug: {exc}"
            ) from exc
    for n, src in enumerate(raw.get("sources") or [], 1):
        if not src.strip():
            raise DesignTreeError(
                f"{where} ({node_id}): sources {n} must be a nonempty string"
            )
    for n, issue in enumerate(raw.get("issues") or [], 1):
        if (
            not isinstance(issue, dict)
            or not isinstance(issue.get("issue"), str)
            or not issue["issue"].strip()
        ):
            raise DesignTreeError(
                f"{where} ({node_id}): issue {n} needs a nonempty string 'issue'"
            )
        bad = set(issue) - _ISSUE_FIELDS
        if bad:
            raise DesignTreeError(
                f"{where} ({node_id}): issue {n} unknown field(s) "
                f"{', '.join(sorted(bad))}"
            )
        for name in ("fix", "failure_mode"):
            if issue.get(name) is not None and not isinstance(issue[name], str):
                raise DesignTreeError(
                    f"{where} ({node_id}): issue {n} {name!r} must be a string"
                )
    seen_rules: set[str] = set()
    for n, rule in enumerate(raw.get("rules") or [], 1):
        _vet_rule(node_id, rule, n, seen_rules, where)
    conflicts = raw.get("conflicts")
    if conflicts is not None:
        if not isinstance(conflicts, list):
            raise DesignTreeError(
                f"{where} ({node_id}): conflicts must be a list of records"
            )
        for n, c in enumerate(conflicts, 1):
            _vet_conflict(node_id, c, n, where, seen_rules)


def _vet_conflict(node_id: str, c: Any, n: int, where: str, rule_ids: set[str]) -> None:
    """One ``conflicts`` entry — a ``{topic, values, reading, rules}``
    record, all four required and no other keys. ``rules`` names the
    node's local rule ids the conflict covers."""
    if not isinstance(c, dict):
        raise DesignTreeError(
            f"{where} ({node_id}): conflict {n} must be a mapping "
            "{topic, values, reading, rules}"
        )
    bad = set(c) - _CONFLICT_FIELDS
    if bad:
        raise DesignTreeError(
            f"{where} ({node_id}): conflict {n} unknown field(s) "
            f"{', '.join(sorted(bad))}"
        )
    for name in ("topic", "reading"):
        if not isinstance(c.get(name), str) or not c[name].strip():
            raise DesignTreeError(
                f"{where} ({node_id}): conflict {n} needs a nonempty string '{name}'"
            )
    values = c.get("values")
    if not isinstance(values, list) or not all(isinstance(x, str) for x in values):
        raise DesignTreeError(
            f"{where} ({node_id}): conflict {n} values must be a list of strings"
        )
    rules = c.get("rules")
    if (
        not isinstance(rules, list)
        or not rules
        or not all(isinstance(r, str) for r in rules)
    ):
        raise DesignTreeError(
            f"{where} ({node_id}): conflict {n} needs a nonempty 'rules' "
            "list of local rule ids"
        )
    unknown = [r for r in rules if r not in rule_ids]
    if unknown:
        raise DesignTreeError(
            f"{where} ({node_id}): conflict {n} rules {unknown} name no "
            f"rule on {node_id}"
        )


#: ``table_ref`` may also name a standard table shipped as data under
#: ``precis.data`` (the numbers live in one file, referenced never
#: restated — ISO 2768-1 today). Extend as more tables land.
_STANDARD_TABLES = frozenset({"iso_2768_1.json"})


@lru_cache(maxsize=1)
def _capability_row_ids() -> frozenset[str]:
    """The ``table_ref`` vocabulary: the ``mode`` of every row in the
    packaged ``design-tree/capabilities.json`` (per-rule published
    claims), every ``mode`` in ``se_capabilities.json`` — and each
    ``mode.field`` pair, for a rule whose figure *is* one promoted
    bound (``fdm/*.min_wall``) — plus the standard-table file names in
    :data:`_STANDARD_TABLES`. Cached — every ``machine_specific`` rule
    in the manifests hits this lookup."""
    rows = set(_STANDARD_TABLES)
    try:
        data = json.loads(
            resources.files("precis.data")
            .joinpath("design-tree")
            .joinpath("capabilities.json")
            .read_text(encoding="utf-8")
        )
    except FileNotFoundError:
        data = {}
    rows.update(str(r["mode"]) for r in data.get("rows") or [] if r.get("mode"))
    try:
        se = json.loads(
            resources.files("precis.data")
            .joinpath("se_capabilities.json")
            .read_text(encoding="utf-8")
        )
    except FileNotFoundError:
        se = {}
    for r in se.get("rows") or []:
        mode = r.get("mode")
        if not mode:
            continue
        rows.add(str(mode))
        rows.update(f"{mode}.{field}" for field in (r.get("fields") or {}))
    return frozenset(rows)


def _vet_rule(
    node_id: str,
    rule: Any,
    n: int,
    seen: set[str],
    where: str,
) -> None:
    if not isinstance(rule, dict):
        raise DesignTreeError(f"{where} ({node_id}): rule {n} must be a mapping")
    rid = rule.get("id")
    if not isinstance(rid, str) or not rid.strip():
        raise DesignTreeError(
            f"{where} ({node_id}): rule {n} needs a nonempty string 'id'"
        )
    if _RULE_ID_RESERVED.match(rid):
        raise DesignTreeError(
            f"{where} ({node_id}): rule id {rid!r} is reserved — "
            "i<n>/f<n> collide with the issue/fix item keys"
        )
    if rid in seen:
        raise DesignTreeError(f"{where} ({node_id}): duplicate rule id {rid!r}")
    seen.add(rid)
    if not isinstance(rule.get("text"), str) or not rule["text"].strip():
        raise DesignTreeError(
            f"{where} ({node_id})#{rid}: nonempty string 'text' required"
        )
    bad = set(rule) - _RULE_FIELDS
    if bad:
        raise DesignTreeError(
            f"{where} ({node_id})#{rid}: unknown field(s) {', '.join(sorted(bad))}"
        )
    equip = rule.get("equipment")
    if equip is not None and equip not in EQUIPMENT:
        raise DesignTreeError(
            f"{where} ({node_id})#{rid}: equipment must be one of "
            f"{', '.join(sorted(EQUIPMENT))}; got {equip!r}"
        )
    conf = rule.get("confidence")
    if conf is not None and conf not in CONFIDENCE:
        raise DesignTreeError(
            f"{where} ({node_id})#{rid}: confidence must be one of "
            f"{', '.join(sorted(CONFIDENCE))}; got {conf!r}"
        )
    qual = rule.get("source_quality")
    if qual is not None and qual not in SOURCE_QUALITY:
        raise DesignTreeError(
            f"{where} ({node_id})#{rid}: source_quality must be one of "
            f"{', '.join(sorted(SOURCE_QUALITY))}; got {qual!r}"
        )
    if "machine_specific" in rule and not isinstance(rule["machine_specific"], bool):
        raise DesignTreeError(
            f"{where} ({node_id})#{rid}: machine_specific must be a bool"
        )
    if "measured_on" in rule and not isinstance(rule["measured_on"], str):
        raise DesignTreeError(
            f"{where} ({node_id})#{rid}: measured_on must be a string"
        )
    tref = rule.get("table_ref")
    if tref is not None:
        if not isinstance(tref, str):
            raise DesignTreeError(
                f"{where} ({node_id})#{rid}: table_ref must be a string"
            )
        rows = _capability_row_ids()
        if not rows:
            raise DesignTreeError(
                f"{where} ({node_id})#{rid}: table_ref {tref!r} — the "
                "design-tree/capabilities.json registry is empty; drop "
                "table_ref until it carries rows"
            )
        if tref not in rows:
            raise DesignTreeError(
                f"{where} ({node_id})#{rid}: table_ref {tref!r} names no "
                "design-tree/capabilities.json row, se_capabilities.json "
                "row[.field], or standard table; "
                f"options: {', '.join(sorted(rows))}"
            )
    for enf in rule.get("enforcers") or []:
        if (
            not isinstance(enf, dict)
            or set(enf) - _ENFORCER_FIELDS
            or not isinstance(enf.get("path"), str)
            or not isinstance(enf.get("what"), str)
        ):
            raise DesignTreeError(
                f"{where} ({node_id})#{rid}: enforcer needs path + what"
            )


def _finish(nodes: dict[str, Node]) -> DesignTree:
    """Second pass: resolve also_under/links, check the DAG, and flatten
    to items+edges."""
    extra_parents: dict[str, list[str]] = {}  # child -> extra parents
    for node in nodes.values():
        for target in node.record.get("also_under") or []:
            if target not in nodes:
                raise DesignTreeError(
                    f"{node.id}: also_under target {target!r} does not exist"
                )
            if target == node.id or target in node.parents:
                raise DesignTreeError(
                    f"{node.id}: also_under {target!r} duplicates its "
                    "primary parent or itself"
                )
            extra_parents.setdefault(node.id, []).append(target)
        for target in node.record.get("links") or []:
            if target not in nodes:
                raise DesignTreeError(
                    f"{node.id}: links target {target!r} does not exist"
                )
        for key in node.record.get("axes") or {}:
            if f"axis/{key}" not in nodes:
                raise DesignTreeError(
                    f"{node.id}: axes key {key!r} names no axis node "
                    f"(expected axis/{key})"
                )
        for issue in node.record.get("issues") or []:
            fm = (issue.get("failure_mode") or "").strip()
            if fm and f"failure/{fm}" not in nodes:
                raise DesignTreeError(
                    f"{node.id}: issue failure_mode {fm!r} names no "
                    f"failure-mode node (expected failure/{fm})"
                )
        for ask in node.record.get("asks") or []:
            name = ask.split("{", 1)[0].strip()
            if f"criterion/{name}" not in nodes:
                raise DesignTreeError(
                    f"{node.id}: asks entry {ask!r} names no criterion "
                    f"node (expected criterion/{name})"
                )

    # rebuild nodes with all parents, then check the DAG is acyclic
    final: dict[str, Node] = {}
    for nid, node in nodes.items():
        parents = list(node.parents) + [
            p for p in extra_parents.get(nid, []) if p not in node.parents
        ]
        final[nid] = Node(
            id=node.id,
            role=node.role,
            record=node.record,
            parents=tuple(parents),
            manifest=node.manifest,
        )
    _check_acyclic(final)
    final = _inherit(final)

    items, edges = _flatten(final)
    return DesignTree(final, items, edges)


def _check_acyclic(nodes: dict[str, Node]) -> None:
    """Refuse a cycle in the specialises/also_under backbone (DFS from
    every node; ``related-to`` links are not followed)."""
    WHITE, GREY, BLACK = 0, 1, 2
    colour = {nid: WHITE for nid in nodes}

    def visit(nid: str, trail: list[str]) -> None:
        colour[nid] = GREY
        for parent in nodes[nid].parents:
            if colour[parent] == GREY:
                cycle = trail[trail.index(parent) :] + [parent]
                raise DesignTreeError("specialises cycle: " + " -> ".join(cycle))
            if colour[parent] == WHITE:
                visit(parent, [*trail, parent])
        colour[nid] = BLACK

    for nid in nodes:
        if colour[nid] == WHITE:
            visit(nid, [nid])


def _inherit(nodes: dict[str, Node]) -> dict[str, Node]:
    """Fill each node's effective ``domain``/``scale``: its own record
    first, else the first parent that resolves one (roots carry them in
    the real manifests). Runs after :func:`_check_acyclic` — the walk is
    guaranteed to terminate."""
    memo: dict[tuple[str, str], str | None] = {}

    def resolve(nid: str, name: str) -> str | None:
        if (nid, name) not in memo:
            node = nodes[nid]
            value = node.record.get(name)
            if value is None:
                if name == "equipment":
                    vals: list[str] = []
                    for parent in node.parents:
                        v = resolve(parent, name)
                        if v is not None and v not in vals:
                            vals.append(v)
                    if len(vals) > 1:
                        advice = (
                            "declare 'equipment' explicitly on this node"
                            if node.role in ("process", "leaf")
                            else "pick one parent — equipment cannot be "
                            f"declared on role={node.role} nodes"
                        )
                        raise DesignTreeError(
                            f"{nid}: parents disagree on equipment "
                            f"({', '.join(vals)}); {advice}"
                        )
                    value = vals[0] if vals else None
                else:
                    for parent in node.parents:
                        value = resolve(parent, name)
                        if value is not None:
                            break
            memo[(nid, name)] = value
        return memo[(nid, name)]

    out: dict[str, Node] = {}
    for nid, node in nodes.items():
        out[nid] = Node(
            id=node.id,
            role=node.role,
            record=node.record,
            parents=node.parents,
            manifest=node.manifest,
            domain=resolve(nid, "domain"),
            scale=resolve(nid, "scale"),
            equipment=resolve(nid, "equipment"),
        )
    return out


def _flatten(nodes: dict[str, Node]) -> tuple[list[Item], list[Edge]]:
    """Turn the validated forest into the mintable item/edge lists.

    Items: one per node, one per rule (``<node>#<rid>``), per issue
    (``<node>#i<n>``), per fix (``<node>#f<n>``), per distinct source
    (``src:<text>``) and per distinct enforcer (``enf:<path>``).
    """
    items: list[Item] = []
    edges: list[Edge] = []
    have_item: set[str] = set()
    have_edge: set[tuple[str, str, str]] = set()

    def item(key: str, role: str, title: str, fields: dict, node_id: str) -> None:
        if key in have_item:
            return
        have_item.add(key)
        items.append(Item(key, role, _one_line(title), fields, node_id))

    def edge(src: str, dst: str, rel: str, meta: dict | None = None) -> None:
        if (src, dst, rel) in have_edge:
            return
        have_edge.add((src, dst, rel))
        edges.append(Edge(src, dst, rel, dict(meta or {})))

    def source_ref(text: str, node_id: str) -> str:
        key = f"src:{text}"
        item(key, "source", text, {"source": text}, node_id)
        return key

    for nid in nodes:
        node = nodes[nid]
        rec = node.record
        item(nid, node.role, rec.get("summary") or rec.get("def") or nid, rec, nid)
        for parent in node.parents:
            edge(nid, parent, "specialises")
        for target in rec.get("links") or []:
            meta = {}
            as_role = RELATED_AS.get(nodes[target].role)
            if as_role:
                meta["as"] = as_role
            edge(nid, target, "related-to", meta)
        for key, val in (rec.get("axes") or {}).items():
            if str(val).strip().lower().startswith("n/a"):
                continue  # not relevant here — an edge reads as relevant
            edge(nid, f"axis/{key}", "related-to", {"as": "axis"})
        for issue in rec.get("issues") or []:
            fm = (issue.get("failure_mode") or "").strip()
            if fm:
                edge(nid, f"failure/{fm}", "related-to", {"as": "mode"})

        for s in rec.get("sources") or []:
            edge(nid, source_ref(s, nid), "cites")
        for n, issue in enumerate(rec.get("issues") or [], 1):
            ikey = f"{nid}#i{n}"
            item(ikey, "issue", issue["issue"], dict(issue), nid)
            edge(ikey, nid, "qualifies")
            fix = (issue.get("fix") or "").strip()
            if fix:
                fkey = f"{nid}#f{n}"
                item(fkey, "fix", fix, {"fix": fix}, nid)
                edge(fkey, ikey, "fixes")
        for rule in rec.get("rules") or []:
            rkey = f"{nid}#{rule['id']}"
            item(rkey, "rule", rule["text"], dict(rule), nid)
            edge(rkey, nid, "part-of")
            url = rule.get("source_url")
            if isinstance(url, str) and url.strip():
                edge(rkey, source_ref(url, nid), "cites")
            for enf in rule.get("enforcers") or []:
                ekey = f"enf:{enf['path']}"
                item(
                    ekey,
                    "enforcer",
                    f"Enforcer: {enf['path']}",
                    {"path": enf["path"], "what": enf["what"]},
                    nid,
                )
                edge(ekey, rkey, "tests")
    return items, edges


def _one_line(text: Any) -> str:
    """First line of a title candidate, trimmed."""
    return str(text).strip().splitlines()[0].strip()


# ---------------------------------------------------------------------------
# Export (src/precis/data/design_rules.json) — from manifests, not the DB
# ---------------------------------------------------------------------------

_EXPORT_FILE = "design_rules.json"


def export_rules(tree: DesignTree) -> dict[str, Any]:
    """Render the flat DRC/leaf vocabulary the packaged
    ``design_rules.json`` carries: per-node role/coverage/implies plus its
    rules, and the sorted leaf/axis/principle id lists. A ``conflicts``
    record's ``rules:`` names the local rule ids it covers; the export
    marks those rules ``conflicted`` — plus every rule in the forest
    sharing a marked rule's ``table_ref`` (the same disputed capability
    row, wherever the rule lives) — so DRC never enforces them."""
    marked: set[str] = set()  # '<node>#<rid>' keys a conflict names
    for nid, node in tree.nodes.items():
        for c in node.record.get("conflicts") or []:
            for rid in c["rules"]:
                marked.add(f"{nid}#{rid}")
    marked_refs = {
        r["table_ref"]
        for nid, node in tree.nodes.items()
        for r in node.record.get("rules") or []
        if f"{nid}#{r['id']}" in marked and r.get("table_ref")
    }
    nodes: dict[str, Any] = {}
    for nid in sorted(tree.nodes):
        node = tree.nodes[nid]
        rec = node.record
        entry: dict[str, Any] = {"role": node.role}
        if node.domain is not None:
            entry["domain"] = node.domain
        if node.scale is not None:
            entry["scale"] = node.scale
        if node.equipment is not None:
            entry["equipment"] = node.equipment
        if rec.get("coverage") is not None:
            entry["coverage"] = rec["coverage"]
        if rec.get("implies") is not None:
            entry["implies"] = rec["implies"]
        if node.parents:
            entry["parents"] = list(node.parents)
        conflicts = rec.get("conflicts") or []
        if conflicts:
            entry["conflicts"] = [
                dict(c) if isinstance(c, dict) else c for c in conflicts
            ]
        rules = [
            {k: v for k, v in r.items() if k != "enforcers"}
            for r in rec.get("rules") or []
        ]
        if rules:
            for r in rules:
                r["id"] = f"{nid}#{r['id']}"
                # rules under a process node default to its equipment
                if r.get("equipment") is None and node.equipment is not None:
                    r["equipment"] = node.equipment
                if r["id"] in marked or r.get("table_ref") in marked_refs:
                    r["conflicted"] = True
            entry["rules"] = rules
        nodes[nid] = entry
    return {
        "version": 1,
        "nodes": nodes,
        "leaves": [
            n.id
            for n in sorted(tree.nodes.values(), key=lambda n: n.id)
            if n.role == "leaf"
        ],
        "axes": sorted(n.id for n in tree.nodes.values() if n.role == "axis"),
        "principles": sorted(
            n.id for n in tree.nodes.values() if n.role == "principle"
        ),
    }


def export_json(tree: DesignTree) -> str:
    """Canonical serialised export (deterministic: sorted keys, trailing
    newline) — the bytes ``design_rules.json`` must equal."""
    return json.dumps(export_rules(tree), indent=2, sort_keys=True) + "\n"


def packaged_data_dir() -> Path:
    """``src/precis/data`` — where the export and (post-S1) the
    ``design-tree/`` manifests live."""
    return Path(__file__).resolve().parent / "data"


@cache
def load_export() -> dict[str, Any]:
    """Read the packaged ``design_rules.json`` (empty graph when absent —
    ``params.leaf`` refuses every id honestly). Cached: the export is
    over a megabyte and ``joints`` asks for it per ``params.leaf``."""
    try:
        text = (
            resources.files("precis.data")
            .joinpath(_EXPORT_FILE)
            .read_text(encoding="utf-8")
        )
    except FileNotFoundError:
        return {"version": 1, "nodes": {}, "leaves": [], "axes": [], "principles": []}
    return dict(json.loads(text))


@cache
def leaf_ids() -> frozenset[str]:
    """The ``params.leaf`` vocabulary — leaf node ids from the export.
    Cached: tests that monkeypatch :func:`load_export` must
    ``cache_clear()`` this to re-read."""
    return frozenset(load_export().get("leaves") or [])


def main() -> None:
    """``python -m precis.design_tree <manifest-dir-or-file...>`` — write
    the canonical export to stdout. Regenerate ``design_rules.json`` with::

        uv run python -m precis.design_tree src/precis/data/design-tree \
            > src/precis/data/design_rules.json

    (until S1 lands the packaged manifests the shipped file is the
    canonical empty export, and the sync test fails on any drift either
    way)."""
    import sys

    paths = [Path(a) for a in sys.argv[1:]]
    if not paths or any(not p.exists() for p in paths):
        raise SystemExit(
            "usage: python -m precis.design_tree <manifest-dir-or-file...>"
        )
    files = (
        sorted(paths[0].glob("*.yaml"))
        if len(paths) == 1 and paths[0].is_dir()
        else paths
    )
    if not files:
        raise SystemExit(f"{paths[0]}: no *.yaml manifests")
    sys.stdout.write(export_json(load_files(tuple(files))))


if __name__ == "__main__":
    main()


__all__ = [
    "CONFIDENCE",
    "COVERAGE",
    "EQUIPMENT",
    "RELATED_AS",
    "ROLES",
    "ROLE_ALIASES",
    "SOURCE_QUALITY",
    "DesignTree",
    "DesignTreeError",
    "Edge",
    "Item",
    "Node",
    "export_json",
    "export_rules",
    "leaf_ids",
    "load_dir",
    "load_export",
    "load_files",
    "packaged_data_dir",
]
