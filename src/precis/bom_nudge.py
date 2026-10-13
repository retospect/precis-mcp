"""The BOM consolidation nudge — "x and y are similar, can they be
combined?" asked on both bom views, answered by the human
(docs/backlog/bom-consolidation-nudge.md, ruled 2026-10-06).

Not a rewrite and not a skill: a **footer** on ``view='bom'`` of the
``component`` kind (assembly-tree rollup) and the ``pcb`` kind (the JLCPCB
BOM response — the exported .csv itself is untouched; the nudge lives only
in the MCP response text). It never mutates the design, the links or the
DB — rendering is a pure read.

Grouping needs the system to know what a part is, so attribute ROLES are
declared as data (migration 0205): which specs are *identity* (must match
for two rows to be one part: thread, package, pin count), *envelope* (one
part covers another: power rating at least, tolerance at most), *free*
(the design may just change it: resistance to an E-series neighbour, screw
length) and *tool* (the value implies a driver/tap/die/reflow pass, not a
part). For the component kind the roles live on ``component_specs``; for
jlcparts-parametric pcb classes — which have no spec rows — on
``component_categories.spec_roles.jlcparts``.

Three line shapes, then suppression:

- **group line** — one per cluster of leaves sharing every identity value
  and differing only in free/envelope attributes ("resistors 0603: 10k x4,
  12k x3, 8k x1 — one resistance? ..."). The proposed target prefers a
  member already in the house set (:data:`standard_parts.json`), then the
  plurality; merge feasibility of a value change is ONE cheap LLM call
  (:func:`ask_merge_verdict`, stubbed in tests) whose one-sentence verdict
  and cited inputs print verbatim — it never asserts when the design
  states no intent.
- **tool line** — one per tool-role attribute with >1 distinct value
  across the view ("fasteners drive type: hex x6, torx x2 — all hex? (one
  driver)").
- **kept-distinct** — the human's standing answer:
  ``link(rel='kept-distinct', meta={'reason': ...})`` between two part
  refs, or a ``kept-distinct:<a>:<b>[:<reason>]`` tag on the BOM root,
  suppresses the group's proposal and prints the reason instead. An
  accepted merge is an ordinary edit; the nudge disappears because the
  group does.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_STD_PARTS_PATH = Path(__file__).resolve().parent / "data" / "standard_parts.json"

#: jlcparts attribute name → the display unit its values carry; drives
#: engineering-format display ('10k' not '10000') for pcb bom lines.
_JLC_UNITS: dict[str, str] = {
    "Resistance": "ohm",
    "Capacitance": "F",
    "Inductance": "H",
    "Rated Voltage": "V",
    "Voltage - Clamping": "V",
    "Power(Watts)": "W",
    "Power": "W",
    "Tolerance": "%",
    "Current": "A",
    "Operating Temperature": "°C",
}

#: LLM verdict call knobs (same shape as ``precis.pcb.argue``).
LLM_SOURCE = "bom-consolidation"
LLM_MAX_USD = 0.10
LLM_TIMEOUT_S = 60.0


# ---------------------------------------------------------------------------
# roles
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RoleDecl:
    """One attribute's parsed role set (the ``spec_roles`` jsonb shape)."""

    identity: bool = False
    free: bool = False
    envelope_rule: str | None = None  # at_least | at_most | within
    envelope_margin: float | None = None
    tool_kind: str | None = None
    tool_ref: str | None = None


def parse_roles(raw: Any) -> RoleDecl | None:
    """Parse a ``spec_roles`` jsonb value; ``None`` when no role is set."""
    if not isinstance(raw, dict) or not raw:
        return None
    env = raw.get("envelope")
    rule = margin = None
    if isinstance(env, dict):
        rule = str(env.get("rule") or "") or None
        margin = env.get("margin")
    elif env is True:
        rule = "at_least"
    tool = raw.get("tool")
    tool_kind = tool_ref = None
    if isinstance(tool, dict):
        tool_kind = str(tool.get("kind") or "") or None
        tool_ref = str(tool.get("ref") or "") or None
    elif isinstance(tool, str):
        tool_kind = tool
    elif tool is True:
        tool_kind = "tool"
    decl = RoleDecl(
        identity=bool(raw.get("identity")),
        free=bool(raw.get("free")),
        envelope_rule=rule,
        envelope_margin=float(margin) if margin is not None else None,
        tool_kind=tool_kind,
        tool_ref=tool_ref,
    )
    if not (decl.identity or decl.free or decl.envelope_rule or decl.tool_kind):
        return None
    return decl


@dataclass
class ClassRoles:
    """A part class's resolved role table."""

    class_id: str
    name: str  # "Resistor" → rendered "resistors"
    attrs: dict[str, RoleDecl] = field(default_factory=dict)

    @property
    def label(self) -> str:
        base = self.name.strip().lower() or self.class_id
        return base if base.endswith("s") else base + "s"

    def role_attrs(self, *kinds: str) -> list[str]:
        out = []
        for name, decl in self.attrs.items():
            if (
                ("identity" in kinds and decl.identity)
                or ("free" in kinds and decl.free)
                or ("envelope" in kinds and decl.envelope_rule)
                or ("tool" in kinds and decl.tool_kind)
            ):
                out.append(name)
        return out


def spec_roles_for_category(
    spec_rows: dict[str, Any], category_id: str | None
) -> dict[str, RoleDecl]:
    """``{spec_id: RoleDecl}`` for the role-bearing specs applicable to one
    component category (universal + category-scoped)."""
    out: dict[str, RoleDecl] = {}
    for spec_id, row in spec_rows.items():
        if row["category_id"] is not None and row["category_id"] != category_id:
            continue
        decl = parse_roles(row["spec_roles"])
        if decl is not None:
            out[spec_id] = decl
    return out


# ---------------------------------------------------------------------------
# items
# ---------------------------------------------------------------------------


@dataclass
class BomItem:
    """One BOM line item for grouping: a component-kind leaf, or one pcb
    component type (its instances already folded into ``qty``)."""

    key: str  # slug / lcsc / component_id — identity for kept-distinct tags
    label: str
    qty: int
    class_id: str
    class_label: str
    attrs: dict[str, Any]  # role-attr name -> raw value
    nums: dict[str, float]  # the numeric ones, for delta math
    units: dict[str, str | None]
    tokens: frozenset[str] = frozenset()  # slug/label/lcsc, normalized
    ref_ids: frozenset[int] = frozenset()  # leaf ref_ids (component kind only)
    datasheet: str | None = None
    usage_key: Any = None  # opaque token the caller's usage_fn consumes


def _norm(v: Any) -> str:
    return re.sub(r"\s+", " ", str(v).strip()).casefold()


def _fmt_num(v: float, unit: str | None) -> str:
    """Engineering-ish display: 10000 ohm → '10k ohm', 0.0000001 F →
    '100n F', 16 mm → '16 mm'."""
    if unit in ("ohm", "F", "H"):
        for scale, prefix in (
            (1e9, "G"),
            (1e6, "M"),
            (1e3, "k"),
            (1.0, ""),
            (1e-3, "m"),
            (1e-6, "µ"),
            (1e-9, "n"),
            (1e-12, "p"),
        ):
            if abs(v) >= scale:
                # '10k' for ohms (the bare value is the engineering idiom),
                # '100nF' / '22µH' for the rest.
                return f"{v / scale:g}{prefix}{'' if unit == 'ohm' else unit}"
        return f"{v:g}{unit}"
    return f"{v:g} {unit}" if unit else f"{v:g}"


def _fmt_attr(item: BomItem, attr: str) -> str:
    if attr in item.nums:
        return _fmt_num(item.nums[attr], item.units.get(attr))
    return str(item.attrs.get(attr, "—"))


def _parse_si(text: Any) -> float | None:
    """'10kΩ' → 10000.0, '100nF' → 1e-7, '±1%' → 1.0, '0.1W' → 0.1 — the
    jlcparts attribute-string → number half of the pcb-side grouping."""
    if isinstance(text, (int, float)):
        return float(text)
    m = re.fullmatch(
        r"[±~]?([+-]?\d+(?:\.\d+)?)\s*(T|G|M|k|m|µ|u|n|p)?\s*[A-Za-zΩ%°]*",
        str(text).strip(),
    )
    if not m or m.group(0).strip() == "":
        return None
    scale = {
        "T": 1e12,
        "G": 1e9,
        "M": 1e6,
        "k": 1e3,
        "m": 1e-3,
        "µ": 1e-6,
        "u": 1e-6,
        "n": 1e-9,
        "p": 1e-12,
    }.get(m.group(2) or "", 1.0)
    return float(m.group(1)) * scale


# ---------------------------------------------------------------------------
# item builders
# ---------------------------------------------------------------------------


def component_items(
    store: Any, leaves: dict[int, int]
) -> tuple[list[BomItem], dict[str, ClassRoles]]:
    """BomItems for the component-kind bom (``_bom_leaves`` output), plus
    the resolved class-role tables actually used."""
    spec_rows = store.component_role_specs()
    cat_rows = {c["category_id"]: c for c in store.component_category_list()}
    items: list[BomItem] = []
    used: dict[str, ClassRoles] = {}
    for leaf_id, qty in leaves.items():
        ref = store.get_ref(kind="component", id=leaf_id)
        if ref is None:
            continue
        meta = ref.meta or {}
        cat = str(meta.get("category") or "")
        role_map = spec_roles_for_category(spec_rows, cat)
        if not role_map:
            continue
        cat_row = cat_rows.get(cat)
        class_name = str((cat_row or {}).get("name") or cat or "part")
        roles = used.setdefault(
            cat, ClassRoles(class_id=cat, name=class_name, attrs=role_map)
        )
        roles.attrs = role_map  # identical per category; last write wins
        current = store.component_current_spec_values(leaf_id)
        attrs: dict[str, Any] = {}
        nums: dict[str, float] = {}
        units: dict[str, str | None] = {}
        for spec_id, decl in role_map.items():
            row = current.get(spec_id)
            if row is None:
                continue
            spec_row = spec_rows.get(spec_id)
            units[spec_id] = (
                spec_row["canonical_unit"] if spec_row is not None else None
            )
            if row["value_num"] is not None:
                attrs[spec_id] = row["value_num"]
                nums[spec_id] = float(row["value_num"])
            elif row["value_text"] is not None:
                attrs[spec_id] = row["value_text"]
            elif row["value_bool"] is not None:
                attrs[spec_id] = row["value_bool"]
        slug = str(ref.slug or ref.id)
        items.append(
            BomItem(
                key=slug,
                label=f"{ref.title} ({slug})",
                qty=qty,
                class_id=cat,
                class_label=roles.label,
                attrs=attrs,
                nums=nums,
                units=units,
                tokens=frozenset({_norm(slug), _norm(ref.title)}),
                ref_ids=frozenset({int(ref.id)}),
            )
        )
    return items, used


def pcb_items(store: Any, ref_id: int) -> tuple[list[BomItem], dict[str, ClassRoles]]:
    """BomItems for the pcb-kind bom: one per distinct component TYPE
    (label+footprint+lcsc), quantities summed over instances. Parametrics
    come from ``parts.params``; classes resolve through the
    ``spec_roles.jlcparts`` declarations on ``component_categories``."""
    classes: list[tuple[ClassRoles, dict[str, Any]]] = []
    for cat in store.component_role_categories().values():
        block = (cat["spec_roles"] or {}).get("jlcparts")
        if not isinstance(block, dict):
            continue
        roles = ClassRoles(class_id=cat["category_id"], name=cat["name"])
        for attr, raw in (block.get("params") or {}).items():
            decl = parse_roles(raw)
            if decl is not None:
                roles.attrs[attr] = decl
        for pseudo in block.get("identity") or []:
            roles.attrs[str(pseudo)] = RoleDecl(identity=True)
        for attr, raw in (block.get("tool") or {}).items():
            kind = str(raw.get("kind")) if isinstance(raw, dict) else str(raw)
            prev = roles.attrs.get(attr)
            roles.attrs[attr] = RoleDecl(
                identity=prev.identity if prev else False,
                free=prev.free if prev else False,
                envelope_rule=prev.envelope_rule if prev else None,
                tool_kind=kind,
            )
        classes.append((roles, block))

    # fold instances of the same type into one line item
    merged: dict[tuple[str, str, str], dict[str, Any]] = {}
    for c in store.pcb_bom_components(ref_id):
        key = (
            _norm(c["label"]),
            _norm(c["footprint"]),
            _norm(c["part_lcsc"]),
        )
        if key in merged:
            merged[key]["qty"] += c["qty"]
        else:
            merged[key] = dict(c)

    items: list[BomItem] = []
    used: dict[str, ClassRoles] = {}
    for c in merged.values():
        params = c.get("params") or {}
        pcat = str(params.get("_category") or "")
        matched = next(
            (
                r
                for r, block in classes
                if str(block.get("match") or "").casefold() in pcat.casefold()
                and str(block.get("match") or "")
            ),
            None,
        )
        if matched is None:
            continue
        used[matched.class_id] = matched
        attrs: dict[str, Any] = {}
        nums: dict[str, float] = {}
        units: dict[str, str | None] = {}
        for attr, decl in matched.attrs.items():
            if attr == "package":
                value = c.get("footprint") or c.get("package")
            elif attr == "lcsc":
                value = c.get("part_lcsc")
            elif attr == "pin_count":
                value = params.get(attr)
            else:
                value = params.get(attr)
            if value is None:
                continue
            attrs[attr] = value
            # numeric parsing only where a delta is meaningful — '0603' is
            # a designation, not a quantity.
            if decl.free or decl.envelope_rule:
                num = _parse_si(value)
                if num is not None:
                    nums[attr] = num
            units[attr] = _JLC_UNITS.get(attr)
        items.append(
            BomItem(
                key=str(c.get("part_lcsc") or c["component_id"]),
                label=str(c["label"]),
                qty=int(c["qty"]),
                class_id=matched.class_id,
                class_label=matched.label,
                attrs=attrs,
                nums=nums,
                units=units,
                tokens=frozenset(
                    t
                    for t in (
                        _norm(c["label"]),
                        _norm(c["part_lcsc"]) if c.get("part_lcsc") else None,
                    )
                    if t
                ),
                datasheet=c.get("datasheet_url"),
                usage_key=(
                    str(c["part_lcsc"])
                    if c.get("part_lcsc")
                    else (str(c["label"]), c.get("footprint"))
                ),
            )
        )
    return items, used


# ---------------------------------------------------------------------------
# grouping
# ---------------------------------------------------------------------------


@dataclass
class Group:
    members: list[BomItem]
    roles: ClassRoles
    identity_disp: str
    diff_attrs: list[str]  # free/envelope attrs with >1 distinct values


def group_with_roles(items: list[BomItem], roles: dict[str, ClassRoles]) -> list[Group]:
    clusters: dict[tuple[Any, ...], list[BomItem]] = {}
    for item in items:
        role = roles.get(item.class_id)
        if role is None:
            continue
        identity = sorted(
            (a, _norm(item.attrs.get(a))) for a in role.role_attrs("identity")
        )
        free_sig = frozenset(a for a in role.role_attrs("free") if a in item.attrs)
        key = (item.class_id, tuple(identity), free_sig)
        clusters.setdefault(key, []).append(item)

    out: list[Group] = []
    for (class_id, identity, _sig), members in sorted(
        clusters.items(), key=lambda kv: str(kv[0])
    ):
        if len(members) < 2:
            continue
        # a group with nothing declared at all can't even be described
        if all(not m.attrs for m in members):
            continue
        role = roles[class_id]
        diff_attrs = []
        for attr in role.role_attrs("free", "envelope"):
            vals = {_norm(m.attrs[attr]) for m in members if attr in m.attrs}
            if len(vals) > 1:
                diff_attrs.append(attr)
        identity_disp = " ".join(
            _fmt_attr(members[0], a) for a, _ in identity if a in members[0].attrs
        ) or ", ".join(sorted({m.key for m in members}))
        out.append(
            Group(
                members=sorted(members, key=lambda m: (-m.qty, m.key)),
                roles=role,
                identity_disp=identity_disp,
                diff_attrs=diff_attrs,
            )
        )
    return out


# ---------------------------------------------------------------------------
# house standard set (slice 3)
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def standard_parts() -> dict[str, dict[str, list[Any]]]:
    """The curated house set: ``{class_id: {attr: [allowed values]}}`` —
    ``src/precis/data/standard_parts.json``. Missing file → empty (the
    nudge still proposes a plurality member)."""
    if not _STD_PARTS_PATH.exists():
        return {}
    try:
        raw = json.loads(_STD_PARTS_PATH.read_text(encoding="utf-8"))
    except ValueError:
        log.warning("bom_nudge: standard_parts.json is not valid JSON")
        return {}
    return {
        str(k): v for k, v in (raw.get("classes") or {}).items() if isinstance(v, dict)
    }


def in_house_set(item: BomItem) -> bool:
    """Every attr the class's house entry constrains must be a listed
    value (numeric compare when the value parses)."""
    entry = standard_parts().get(item.class_id)
    if entry is None:
        return False
    for attr, allowed in entry.items():
        if attr.startswith("_") or not isinstance(allowed, list):
            continue
        val = item.nums.get(attr)
        if val is not None:
            nums = [x for x in (_parse_si(v) for v in allowed) if x is not None]
            if not any(abs(val - n) < 1e-9 * max(1.0, abs(n)) for n in nums):
                return False
        else:
            if _norm(item.attrs.get(attr)) not in {_norm(a) for a in allowed}:
                return False
    return True


# ---------------------------------------------------------------------------
# kept-distinct (slice 2)
# ---------------------------------------------------------------------------


def _kept_distinct_tag_pairs(
    store: Any, root_ref_id: int
) -> list[tuple[str, str, str | None]]:
    """``kept-distinct:<a>:<b>[:<reason>]`` open tags on the bom root —
    the pcb-side door (pcb components aren't refs, so no link edge fits),
    and a fine component-side shorthand too."""
    out = []
    for tag in store.tags_for(root_ref_id):
        value = tag.value
        if not value.startswith("kept-distinct"):
            continue
        parts = value.split(":", 3)
        if len(parts) < 3:
            continue
        out.append(
            (_norm(parts[1]), _norm(parts[2]), parts[3] if len(parts) > 3 else None)
        )
    return out


def _kept_pairs(
    store: Any,
    root_ref_id: int,
    group: Group,
) -> str | None:
    """The suppression reason for a group, or ``None``. A ``kept-distinct``
    link between two leaf refs, or a root tag naming two member tokens."""
    ref_ids = (
        set().union(*(m.ref_ids for m in group.members)) if group.members else set()
    )
    if ref_ids:
        members = set(ref_ids)
        for a, b, reason in store.component_kept_distinct_pairs(sorted(members)):
            if {a, b} <= members:
                a_key = next((m.key for m in group.members if a in m.ref_ids), str(a))
                b_key = next((m.key for m in group.members if b in m.ref_ids), str(b))
                return f"{a_key} ↔ {b_key}: {reason or 'kept distinct'}"
    toks: dict[str, str] = {}
    for m in group.members:
        for t in m.tokens or {m.key}:
            toks[t] = m.key
    for a, b, reason in _kept_distinct_tag_pairs(store, root_ref_id):
        if a in toks and b in toks:
            return f"{toks[a]} ↔ {toks[b]}: {reason or 'kept distinct'}"
    return None


# ---------------------------------------------------------------------------
# the LLM verdict seam
# ---------------------------------------------------------------------------


@dataclass
class MergeVerdict:
    text: str
    inputs: list[str]


VerdictFn = Callable[[dict[str, Any]], MergeVerdict]

_PROMPT = """\
A bill of materials lists these {cls} that share identity attributes \
({identity}) and differ only in {diffs}:
{members}

Proposed consolidation: use {proposal} for all of them.
Datasheets: {datasheets}
Design intent: {intent}

Answer in ONE sentence: is the proposal safe? If a specific part position \
or attribute blocks it, name it. Do not hedge with "it depends" — either \
name the blocker or say it looks safe.\
"""


def ask_merge_verdict(ctx: dict[str, Any]) -> MergeVerdict:
    """The one-sentence feasibility verdict — a single SMALL-tier routed
    call. Never raises into the render path: a transport/model failure
    degrades to a marked 'unavailable' verdict (the nudge still renders;
    it must never break the bom view)."""
    from precis.utils.llm.router import LlmRequest, Tier, route

    prompt = _PROMPT.format(**ctx)
    try:
        res = route(
            LlmRequest(
                tier=Tier.SMALL,
                source=LLM_SOURCE,
                prompt=prompt,
                max_usd=LLM_MAX_USD,
                timeout_s=LLM_TIMEOUT_S,
            )
        )
    except Exception as exc:
        return MergeVerdict(
            text=f"verdict unavailable ({type(exc).__name__})",
            inputs=list(ctx["inputs"]),
        )
    if res.error or not (res.text or "").strip():
        return MergeVerdict(
            text=f"verdict unavailable ({res.error or 'empty reply'})",
            inputs=list(ctx["inputs"]),
        )
    return MergeVerdict(
        text=" ".join((res.text or "").split()), inputs=list(ctx["inputs"])
    )


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def _usage_count(store: Any, root_ref_id: int, item: BomItem) -> int:
    """Designs other than this bom's root that use the proposed part."""
    try:
        if item.ref_ids:
            roots: set[int] = set()
            for leaf in item.ref_ids:
                roots.update(store.component_bom_roots_of(leaf))
            roots.discard(root_ref_id)
            return len(roots)
        if item.usage_key is not None:
            if isinstance(item.usage_key, str):
                return store.pcb_part_design_count(
                    exclude_ref_id=root_ref_id,
                    part_lcsc=item.usage_key,
                    label=None,
                    footprint=None,
                )
            label, footprint = item.usage_key
            return store.pcb_part_design_count(
                exclude_ref_id=root_ref_id,
                part_lcsc=None,
                label=label,
                footprint=footprint,
            )
    except Exception:
        log.debug("bom_nudge: usage count failed", exc_info=True)
    return 0


def _live_stock(item: BomItem) -> str | None:
    """A one-line live-stock note for the proposed part — only when a
    supplier adapter is configured (``precis.supply.base.quote``); absent
    otherwise. Follows the 'stock as a selection signal' ruling: it informs
    the proposal, it is never a gate."""
    try:
        from precis import supply

        if supply.unavailable_reason() is not None:
            return None
        result = supply.quote(item.label, limit=3)
    except Exception:
        return None
    if not result.quotes:
        return None
    best = max(result.quotes, key=lambda q: q.quantity or 0)
    return best.line()


def render_footer(
    store: Any,
    root_ref_id: int,
    items: list[BomItem],
    roles: dict[str, ClassRoles],
    *,
    intent: str | None = None,
    verdict_fn: VerdictFn | None = None,
) -> list[str]:
    """The '## Consolidation' block (empty when there is nothing to nudge).

    ``verdict_fn=None`` resolves :func:`ask_merge_verdict` at call time, so
    tests stub the module attribute."""
    ask = verdict_fn or ask_merge_verdict
    groups = group_with_roles(items, roles)
    lines: list[str] = []

    for g in groups:
        suppressed = _kept_pairs(store, root_ref_id, g)
        if suppressed is not None:
            lines.append(f"- kept distinct ({suppressed})")
            continue

        house = [m for m in g.members if in_house_set(m)]
        proposal = (house or g.members)[0]
        usage = _usage_count(store, root_ref_id, proposal)
        usage_txt = (
            f"; {proposal.key} is used in {usage} other "
            f"design{'s' if usage != 1 else ''}"
            if usage
            else ""
        )

        vals = ", ".join(
            f"{_fmt_attr(m, g.diff_attrs[0])} x{m.qty}"
            if g.diff_attrs
            else f"{m.key} x{m.qty}"
            for m in g.members
        )

        if not g.diff_attrs:
            lines.append(
                f"- {g.roles.label} {g.identity_disp}: {vals} — identical on "
                "declared specs; one part?"
            )
            continue

        attr = g.diff_attrs[0]
        uniform = [
            a
            for a in g.roles.role_attrs("identity", "envelope")
            if a != attr
            and len({_norm(m.attrs[a]) for m in g.members if a in m.attrs}) <= 1
            and any(a in m.attrs for m in g.members)
        ]
        deltas = []
        for m in g.members:
            if m is proposal:
                continue
            if attr in m.nums and attr in proposal.nums and m.nums[attr]:
                pct = (proposal.nums[attr] - m.nums[attr]) / m.nums[attr] * 100
                deltas.append(
                    f"{_fmt_attr(m, attr)}→{_fmt_attr(proposal, attr)} is {pct:+.0f}%"
                )
        datasheets = sorted({m.datasheet for m in g.members if m.datasheet})
        inputs = [f"datasheet: {d}" for d in datasheets]
        inputs.append(f"intent: {intent}" if intent else "no stated intent")
        ctx = {
            "cls": g.roles.label,
            "identity": g.identity_disp,
            "diffs": attr,
            "members": "\n".join(
                f"- {m.label} x{m.qty}: "
                + ", ".join(f"{a}={_fmt_attr(m, a)}" for a in sorted(m.attrs))
                for m in g.members
            ),
            "proposal": f"{_fmt_attr(proposal, attr)} ({proposal.key})",
            "datasheets": "; ".join(datasheets) or "none recorded",
            "intent": intent or "none stated",
            "inputs": inputs,
        }
        verdict = ask(ctx)
        bits = "; ".join(
            b
            for b in (
                ", ".join(a.replace("_", " ") for a in uniform) + " identical"
                if uniform
                else "",
                ", ".join(deltas),
            )
            if b
        )
        tail = f"; {bits}" if bits else ""
        stock = _live_stock(proposal)
        stock_txt = f"; in stock: {stock}" if stock else ""
        intent_txt = " (no design intent stated)" if not intent else ""
        lines.append(
            f"- {g.roles.label} {g.identity_disp}: {vals} — one "
            f"{attr.replace('_', ' ').lower()}? propose {_fmt_attr(proposal, attr)}"
            + (" (the house value)" if proposal in house else "")
            + tail
            + usage_txt
            + stock_txt
            + f". verdict: {verdict.text} (inputs: {', '.join(verdict.inputs)})"
            + intent_txt
        )

    # tool lines: one per (class, tool attr) with >1 distinct value
    tool_groups: dict[tuple[str, str], dict[str, int]] = {}
    tool_kinds: dict[tuple[str, str], str] = {}
    tool_labels: dict[tuple[str, str], str] = {}
    for item in items:
        role = roles.get(item.class_id)
        if role is None:
            continue
        for attr in role.role_attrs("tool"):
            if attr not in item.attrs:
                continue
            decl = role.attrs[attr]
            key = (item.class_id, attr)
            tool_groups.setdefault(key, {})
            tool_kinds[key] = decl.tool_kind or "tool"
            tool_labels[key] = item.class_label
            v = _fmt_attr(item, attr)
            tool_groups[key][v] = tool_groups[key].get(v, 0) + item.qty
    for (class_id, attr), val_qty in sorted(tool_groups.items(), key=lambda kv: kv[0]):
        if len(val_qty) < 2:
            continue
        vals = ", ".join(
            f"{v} x{n}" for v, n in sorted(val_qty.items(), key=lambda x: -x[1])
        )
        top = max(val_qty, key=lambda v: val_qty[v])
        lines.append(
            f"- {tool_labels[(class_id, attr)]} {attr.replace('_', ' ')}: "
            f"{vals} — all {top}? (one {tool_kinds[(class_id, attr)]})"
        )

    return ["## Consolidation", *lines] if lines else []


def component_footer(
    store: Any, *, root_ref_id: int, root_meta: dict[str, Any], leaves: dict[int, int]
) -> list[str]:
    """Footer for ``ComponentHandler._render_bom`` — reads only."""
    items, roles = component_items(store, leaves)
    raw = root_meta.get("intent") or root_meta.get("notes")
    return render_footer(
        store, root_ref_id, items, roles, intent=str(raw) if raw else None
    )


def pcb_footer(store: Any, *, root_ref_id: int, root_meta: dict[str, Any]) -> list[str]:
    """Footer for the pcb ``view='bom'`` response — reads only."""
    items, roles = pcb_items(store, root_ref_id)
    raw = root_meta.get("intent") or root_meta.get("notes")
    return render_footer(
        store, root_ref_id, items, roles, intent=str(raw) if raw else None
    )


__all__ = [
    "BomItem",
    "ClassRoles",
    "Group",
    "MergeVerdict",
    "RoleDecl",
    "ask_merge_verdict",
    "component_footer",
    "component_items",
    "group_with_roles",
    "in_house_set",
    "parse_roles",
    "pcb_footer",
    "pcb_items",
    "render_footer",
    "standard_parts",
]
