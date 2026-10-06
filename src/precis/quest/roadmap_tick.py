"""The ``roadmap`` tick body — one role per tick, chosen from the gap set
(docs/backlog/bootstrap-roadmap-quest.md §Design "The roadmap tick").

A roadmap **root** quest (``meta.quest_body == "roadmap"``) is served by
*capability* quests (same marker + ``rubric_objectives``) and *pathway*
quests (marker, no axes); *rungs* are ``meta.rung`` todos serving one
pathway and one capability. Only the root ticks (the 2026-09-29 root-only
ruling): its role selection scans the capabilities that serve it and acts
on ONE of them, then ends. No proposal menu, no ``structure`` schema, no
frontier, no compute dispatch.

**Roles** (:func:`roadmap_role` — a pure graph read, no LLM, no writes, so
the coordinator can pick the model tier BEFORE it builds a client):

* ``bridge`` — an axis with a demand and a cited best supply that falls
  short, and no rung in flight to close it (the ``unmet-capability`` gap).
  Compares the two and mints a rung, mints a new pathway quest, or writes a
  ``dead-end`` entry. Opus-class (:func:`role_tier` → ``frontier``).
* ``supply`` — an axis with a demand and NO cited supply at all. Lit-search
  on the capability, mint finding hubs with quantified claims, write
  ``meta.supply[key]`` citing them. Sonnet-class (``big``), literature
  lane only.
* ``demand`` — an axis with no ``meta.demand`` entry. Read the se part(s)
  serving the root, compute what the part requires on that axis, write
  ``meta.demand[key]`` plus a ``decision`` entry. Sonnet-class (``big``).

Priority: the lowest unmet capability first (it blocks the chain above
it), then supply, then demand. "Lowest" is derived from the graph, not
annotated: a capability consumed by more rungs sits lower in the chain;
ties keep ``serves`` link order. Within a capability, axes keep
``rubric_objectives`` order.

**Deed** (spec §"Deed"): at tick end the ledger is recomputed and diffed
against the signature the PREVIOUS pinned ledger chunk carried
(``meta.signature`` on the ``meta.pinned='capability-ledger'`` chunk of the
root's dossier). Every (capability, key) whose best supply improved in the
axis' ``sense`` gets a code-stamped ``milestone`` on the capability AND on
the root, ``by="system"``. A rung marked ``done`` by a human between ticks
therefore lands its deed on the next tick. The model cannot emit
``milestone`` itself — ``quest/tick.py::_sanitize_model_entry`` already
clamps that on the shared apply path; this module never appends a
model-authored entry of that type either. On the first tick (no prior
chunk) the baseline is the tick's own start state: supplies that already
stood are seeded, not stamped; what the tick itself improves still is.

**Dry vs engaged** (spec §"Stall / halt"): a tick that changes neither the
root's gap count nor any ledger value is *dry*; only a ledger improvement
is *engagement*. Minting a rung changes the gap count (unmet → partial) so
it is not dry, but it is not engagement either — the coordinator carries
its dry streak unchanged across such a tick and resets it only on
``improved``. Near-dup rung titles are gated by the token-Jaccard measure
(:func:`precis.quest.dossier._find_near_dup_node`) over the sibling rungs
on the same capability, BEFORE the todo is minted; never through
``add_attempt`` (that writes into a quest's own attempt ledger and gates
nothing about node creation).

**Rung minting boundary:** ``STATUS:open`` + ``waiting-for:reto``, and NO
``llm_tier`` key — the dispatch worker selects on ``meta ? 'llm_tier'``, so
its absence is the rotation lock (the 2026-09-29 amendment); the status
keeps the rung visible in ordinary todo views. A new pathway quest mints
``STATUS:dormant``.

**Terminal rungs (the benign gate):** a rung whose ``produces`` appears in
no other rung's ``consumes`` is terminal — derived, not annotated. When the
bridge role mints a terminal rung and the root has a *benign* capability
(a capability whose title or axis key names ``benign``), that capability's
demanded axes are appended to the rung's ``consumes`` (required); for an
intermediate rung they are advisory in the prompt only. A stored
``meta.rung.benign == "required"`` is honoured upward (never downward) when
present; ``handlers/_todo_guards.py::_RUNG_ALLOWED_KEYS`` admits the key
with ``"required"`` as its only accepted value.
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any, Literal

from precis.quest import roadmap_ledger as ledger
from precis.quest.dossier import (
    AttemptNode,
    _find_near_dup_node,
    _find_pinned_chunk,
    ensure_dossier,
)
from precis.quest.gaps import Gap, _live_servers, quest_gaps
from precis.quest.logbook import append_entry
from precis.quest.weave_tick import (
    QUEST_BODY_META_KEY,
    QUEST_BODY_ROADMAP,
    mark_roadmap_quest,
)
from precis.utils import handle_registry
from precis.utils.coerce import num
from precis.utils.llm.json_reply import extract_json_object

if TYPE_CHECKING:
    from precis.store import Ref, Store

log = logging.getLogger(__name__)

ROLE_DEMAND = "demand"
ROLE_SUPPLY = "supply"
ROLE_BRIDGE = "bridge"

#: Role → LLM tier string (``router.Tier`` values). Decided 2026-09-28: the
#: role picks the tier, in code, with no knob — ``meta.loop.tier`` is
#: ignored for this body. demand/supply reason over a part or a paper list
#: (Sonnet-class ``big``); bridge weighs a shortfall against every pathway
#: and may mint graph nodes (Opus-class ``frontier``).
ROLE_TIERS: dict[str, str] = {
    ROLE_DEMAND: "big",
    ROLE_SUPPLY: "big",
    ROLE_BRIDGE: "frontier",
}

#: ``meta.pinned`` value of the root dossier's regenerated ledger chunk.
LEDGER_PINNED = "capability-ledger"

#: Seed text of a freshly created ledger chunk — always overwritten on the
#: same tick that creates it, so seeing it after a tick is itself a bug.
_LEDGER_SEED = "_(Capability ledger not yet generated.)_\n"

#: A capability is "the benign capability" (spec §"Terminal vs intermediate
#: rungs") when its title or one of its axis keys carries this token. The
#: spec names the prod node by id (qu454479); a token is the only
#: id-free derivation available without a new meta key.
BENIGN_TOKEN = "benign"

#: Tags every minted rung carries (spec §"Rung minting boundary").
RUNG_TAGS: tuple[str, ...] = ("STATUS:open", "waiting-for:reto")

#: Escalated supply ticks whose outside search errored, in one dry run, before
#: escalation stops (``_run_supply``).
_OUTSIDE_FAIL_BOUND = 2
#: Cap on the paper cards fed to the supply role's second call.
_SUPPLY_MAX_PAPERS = 8
_SUPPLY_CARD_CHARS = 900
#: Open tag on a finding hub whose supply quote raised an uncertainty cue.
_SUPPLY_HELD_TAG = "review:supply-held"
#: Cap on the sibling rungs listed in the bridge prompt.
_BRIDGE_MAX_RUNGS = 30

_SE_SLUG_RE = re.compile(r"\bse:([A-Za-z0-9_.-]+)")
_HUB_ID_RE = re.compile(r"claim hub fi(\d+)")

_LOG_KIND = "quest_log"


# ── role selection ────────────────────────────────────────────────────


@dataclass(frozen=True)
class RoleChoice:
    """The one (role, capability, axis) a tick acts on."""

    role: str  # demand | supply | bridge
    capability_id: int
    capability: str  # "qu<id>"
    capability_title: str
    key: str
    sense: str  # min | max
    unit: str | None
    demanded: float | None
    best_supply: float | None
    best_supply_evidence: tuple[str, ...]
    gap: Gap
    #: Full capability statement for the prompt (``capability_title`` is the
    #: ledger's 60-char stub — see :class:`~precis.quest.roadmap_ledger.LedgerRow`).
    capability_statement: str = ""

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["gap"] = asdict(self.gap)
        return d


def role_tier(role: str | None) -> str:
    """The tier string for ``role`` (``big`` when there is no role — the
    client is then never used, but the coordinator still builds one)."""
    return ROLE_TIERS.get(role or "", "big")


def _is_roadmap_root(store: Store, quest_id: int) -> Ref | None:
    ref = store.get_ref(kind="quest", id=quest_id)
    if ref is None or ref.retired_at is not None:
        return None
    if (ref.meta or {}).get(QUEST_BODY_META_KEY) != QUEST_BODY_ROADMAP:
        return None
    return ref


def _handle(kind: str, ref_id: int) -> str:
    return handle_registry.try_format(kind, ref_id) or f"{kind}:{ref_id}"


def _parse_quest_handle(raw: Any) -> int | None:
    if not isinstance(raw, str):
        return None
    parsed = handle_registry.parse(raw.strip())
    if parsed is None or parsed[0] != "quest" or parsed[1]:
        return None
    return parsed[2]


def _rungs_under(store: Store, capabilities: list[Ref]) -> list[ledger.Rung]:
    """Live ``meta.rung`` todos serving any of ``capabilities`` with their
    resolved ``STATUS`` (deduped across capabilities, link order) — read
    through :func:`roadmap_ledger.rungs_for`, the ledger's own rung reader."""
    by_cap = ledger.rungs_for(store, [c.id for c in capabilities])
    out: list[ledger.Rung] = []
    seen: set[int] = set()
    for cap in capabilities:
        for rung in by_cap.get(cap.id, []):
            if rung.ref.id not in seen:
                seen.add(rung.ref.id)
                out.append(rung)
    return out


def _rung_entries(rung: Ref, where: str) -> list[dict[str, Any]]:
    raw = ((rung.meta or {}).get("rung") or {}).get(where)
    if not isinstance(raw, list):
        return []
    return [e for e in raw if isinstance(e, dict)]


def _consumer_counts(rungs: list[Ref]) -> dict[str, int]:
    """``{capability handle: number of rungs consuming a value from it}`` —
    the chain-depth proxy the priority rule ranks on."""
    counts: dict[str, int] = {}
    for rung in rungs:
        for entry in _rung_entries(rung, "consumes"):
            cap = entry.get("capability")
            if isinstance(cap, str):
                counts[cap] = counts.get(cap, 0) + 1
    return counts


def _pathways(servers: list[Ref]) -> list[Ref]:
    """Roadmap-marked quests serving the root that carry no axes."""
    return [
        r
        for r in servers
        if r.kind == "quest"
        and (r.meta or {}).get(QUEST_BODY_META_KEY) == QUEST_BODY_ROADMAP
        and not ledger.is_capability_quest(r)
    ]


def _benign_capability(capabilities: list[Ref]) -> Ref | None:
    for cap in capabilities:
        title = (cap.title or "").lower()
        if BENIGN_TOKEN in title:
            return cap
        if any(
            BENIGN_TOKEN in key.lower() for key, _s, _u in ledger.capability_axes(cap)
        ):
            return cap
    return None


def roadmap_role(store: Store, quest_id: int) -> RoleChoice | None:
    """Pick this tick's role from the root's gap set — a pure graph read.

    ``None`` when ``quest_id`` is not a live roadmap root, has no capability
    servers, or every axis is met/dead-ended/in flight (nothing to act on —
    the tick is then dry by construction).
    """
    root = _is_roadmap_root(store, quest_id)
    if root is None:
        return None
    servers = _live_servers(store, quest_id)
    capabilities = [r for r in servers if ledger.is_capability_quest(r)][
        : ledger.LEDGER_MAX_CAPABILITIES
    ]
    if not capabilities:
        return None
    rows = ledger.compute_ledger_for(store, capabilities)
    consumers = _consumer_counts([r.ref for r in _rungs_under(store, capabilities)])
    order = {
        _handle("quest", c.id): i for i, c in enumerate(capabilities)
    }  # serves link order
    id_of_cap = {_handle("quest", c.id): c.id for c in capabilities}

    def _rank(row: ledger.LedgerRow) -> tuple[int, int]:
        return (-consumers.get(row.capability, 0), order.get(row.capability, 1 << 30))

    ranked = sorted(enumerate(rows), key=lambda t: (_rank(t[1]), t[0]))
    unmet_gaps = {
        g.handle: g
        for g in quest_gaps(store, quest_id, servers=servers)
        if g.kind == "unmet-capability"
    }

    def _choice(role: str, row: ledger.LedgerRow, gap: Gap) -> RoleChoice:
        return RoleChoice(
            role=role,
            capability_id=id_of_cap[row.capability],
            capability=row.capability,
            capability_title=row.capability_title,
            key=row.key,
            sense=row.sense,
            unit=row.unit,
            demanded=row.demanded,
            best_supply=row.best_supply,
            best_supply_evidence=row.best_supply_evidence,
            gap=gap,
            capability_statement=row.capability_statement or row.capability_title,
        )

    # 1. bridge — unmet with a cited supply to compare against.
    for _i, row in ranked:
        if (
            row.state == ledger.STATE_UNMET
            and row.demanded is not None
            and row.best_supply is not None
        ):
            gap = unmet_gaps.get(row.capability) or Gap(
                kind="unmet-capability",
                detail=f"{row.key}: demanded {row.demanded:g}, best supply "
                f"{row.best_supply:g} — {row.capability_title}",
                handle=row.capability,
            )
            return _choice(ROLE_BRIDGE, row, gap)
    # 2. supply — demand present, nothing cited yet.
    for _i, row in ranked:
        if (
            row.state == ledger.STATE_UNMET
            and row.demanded is not None
            and row.best_supply is None
        ):
            unit = f" {row.unit}" if row.unit else ""
            gap = Gap(
                kind="no-supply",
                detail=(
                    f"{row.key}: demanded {row.demanded:g}{unit}, no cited "
                    f"supply — {row.capability_title}"
                ),
                handle=row.capability,
            )
            return _choice(ROLE_SUPPLY, row, gap)
    # 3. demand — an axis nobody has put a number on.
    for _i, row in ranked:
        if row.demanded is None:
            gap = Gap(
                kind="no-demand",
                detail=f"{row.key}: no demand set — {row.capability_title}",
                handle=row.capability,
            )
            return _choice(ROLE_DEMAND, row, gap)
    return None


# ── prompts ───────────────────────────────────────────────────────────

_SYS_COMMON = (
    "You are the roadmap body of a bootstrap striving: a chain of assemblers, "
    "each built by the one before it. You act on ONE capability axis per "
    "tick and reply with STRICT JSON only — no prose outside the JSON. Every "
    "number you write must be traceable to a handle (se:<slug>, pa<id>, "
    "fi<id>, td<id>, qu<id>). No number, no rung."
)


def _statement(choice: RoleChoice) -> str:
    """The capability text a role prompt shows: the full statement, never the
    ledger's 60-character stub (a hand-built choice without one falls back)."""
    return choice.capability_statement or choice.capability_title


def _axis_line(choice: RoleChoice) -> str:
    unit = f" [{choice.unit}]" if choice.unit else ""
    arrow = "lower is better" if choice.sense == "min" else "higher is better"
    return f"axis: `{choice.key}`{unit} ({arrow})"


def _se_measures_text(store: Store, ref: Ref) -> str:
    """The se part's ``view='measures'`` render, via the plugin handler when
    it is installed; degrades to the part's title otherwise (the prompt
    still names the part, the model just has no numbers to read)."""
    head = f"### se:{ref.slug} — {(ref.title or '').splitlines()[0]}"
    try:
        from precis.dispatch import Hub
        from precis_se.handler import SeHandler  # plugin — may be absent

        body = SeHandler(hub=Hub(store=store)).get(id=str(ref.slug), view="measures")
        return f"{head}\n{body.body}"
    except Exception:
        log.debug(
            "roadmap_tick: se measures unavailable for %s", ref.slug, exc_info=True
        )
        return f"{head}\n(measures view unavailable)"


def _served_se_parts(store: Store, root_id: int, choice: RoleChoice) -> list[Ref]:
    """se parts serving the root or the capability, plus any ``se:<slug>``
    named in the capability's statement."""
    parts: list[Ref] = []
    seen: set[int] = set()
    for owner in (root_id, choice.capability_id):
        for r in _live_servers(store, owner):
            if r.kind == "se" and r.id not in seen:
                seen.add(r.id)
                parts.append(r)
    cap = store.get_ref(kind="quest", id=choice.capability_id)
    for slug in _SE_SLUG_RE.findall((cap.title if cap else "") or ""):
        named: Ref | None
        try:
            named = store.get_ref(kind="se", id=slug)
        except Exception:
            named = None
        if named is not None and named.id not in seen:
            seen.add(named.id)
            parts.append(named)
    return parts


def _demand_prompt(store: Store, root: Ref, choice: RoleChoice) -> str:
    parts = _served_se_parts(store, root.id, choice)
    part_text = (
        "\n\n".join(_se_measures_text(store, p) for p in parts)
        if parts
        else "(no se part serves this root or capability yet — say so in "
        "`reason` and derive the number from the capability statement alone)"
    )
    return (
        f"## Root striving\n{root.title.strip()}\n\n"
        f"## Capability {choice.capability}\n{_statement(choice)}\n"
        f"{_axis_line(choice)}\n\n"
        "## Target part measures (top-down: what the part demands)\n"
        f"{part_text}\n\n"
        "## Task\n"
        f"Compute what the part(s) above require on `{choice.key}` and "
        "show the calculation. Return STRICT JSON:\n"
        '{"value": <number>, "source": "se:<slug> | td<id> | qu<id>", '
        '"reason": "<one sentence: why the part needs this>", '
        '"calculation": "<the arithmetic, handles only, a few lines>"}'
    )


def _paper_servers(store: Store, capability_id: int) -> list[Ref]:
    """Paper servers, NEWEST link first. ``_live_servers`` is oldest-first, and
    both supply prompts cap at :data:`_SUPPLY_MAX_PAPERS` — oldest-first meant
    the papers this tick's own searches just linked never reached the
    extraction call once a capability held more than the cap, so every later
    supply tick re-read the same first papers (qu453869, 2026-10-02: 33
    servers, both supply numbers from the first-linked paper)."""
    papers = [r for r in _live_servers(store, capability_id) if r.kind == "paper"]
    papers.reverse()
    return papers


def _supply_search_prompt(store: Store, root: Ref, choice: RoleChoice) -> str:
    papers = _paper_servers(store, choice.capability_id)
    held = (
        "\n".join(
            f"- {_handle('paper', p.id)} {(p.title or '').splitlines()[0][:100]}"
            for p in papers[:_SUPPLY_MAX_PAPERS]
        )
        or "(none yet)"
    )
    unit = f" {choice.unit}" if choice.unit else ""
    demanded = f"{choice.demanded:g}{unit}" if choice.demanded is not None else "?"
    return (
        f"## Root striving\n{root.title.strip()}\n\n"
        f"## Capability {choice.capability}\n{_statement(choice)}\n"
        f"{_axis_line(choice)}\ndemanded: {demanded}\n\n"
        f"## Papers already serving this capability\n{held}\n\n"
        "## Task (bottom-up: what the literature delivers today)\n"
        f"Propose 1–3 literature searches that would surface the best "
        f"reported value of `{choice.key}` for this capability. Return "
        'STRICT JSON:\n{"searches": ["<query>", ...]}'
    )


@dataclass(frozen=True)
class SupplyCard:
    """One paper as the findings prompt shows it: the exact ``text`` the model
    read and the ``chunk_id`` it came from. ``text`` empty = a title-only
    card (no body chunk), which the findings prompt leaves out."""

    paper: Ref
    text: str
    chunk_id: int | None

    def render(self) -> str:
        head = (
            f"- {_handle('paper', self.paper.id)} "
            f"{(self.paper.title or '').splitlines()[0][:120]}"
        )
        return f"{head}\n  {self.text}" if self.text else head


def _paper_card(store: Store, paper: Ref) -> SupplyCard:
    text, chunk_id = "", None
    try:
        for b in store.chunks.list_chunks_for_ref(paper.id):
            if b.chunk_kind == _LOG_KIND:
                continue
            text = (b.text or "").strip()[:_SUPPLY_CARD_CHARS]
            if text:
                chunk_id = b.id
                break
    except Exception:
        text, chunk_id = "", None
    return SupplyCard(paper=paper, text=text, chunk_id=chunk_id)


def _supply_cards(store: Store, papers: list[Ref]) -> list[SupplyCard]:
    """Cards for the findings prompt: papers with body text only (a title-only
    card invites a number recalled from memory — pa459574, 2026-10-02), the
    :data:`_SUPPLY_MAX_PAPERS` cap applied AFTER that drop, order kept."""
    cards = (_paper_card(store, p) for p in papers)
    return [c for c in cards if c.text][:_SUPPLY_MAX_PAPERS]


def _supply_findings_prompt(
    store: Store, choice: RoleChoice, cards: list[SupplyCard]
) -> str:
    rendered = "\n".join(c.render() for c in cards)
    unit = f" {choice.unit}" if choice.unit else ""
    return (
        f"## Capability {choice.capability}\n{_statement(choice)}\n"
        f"{_axis_line(choice)}\n\n"
        f"## Papers\n{rendered}\n\n"
        "## Task\n"
        f"From these papers only, extract every quantified claim that reports "
        f"a value of `{choice.key}`{unit}. Each claim is ONE sentence carrying "
        "the number and the paper handle it comes from. Count a value only "
        "when the paper reports it as a measurement of this quantity; a "
        "measurement method's own uncertainty, resolution or error bar is not "
        "a value of the axis. Return STRICT JSON:\n"
        '{"findings": [{"claim": "<one sentence with the number>", '
        '"value": <number>, "paper": "pa<id>", '
        '"quote": "<the sentence from the paper text above that states the '
        'number, copied verbatim>"}]}\n'
        "An empty list is a valid answer when no paper reports the axis."
    )


# ── supply quote check ────────────────────────────────────────────────

#: Axis-unit suffixes of a key (``placement_error_nm`` → ``nm``) when the
#: capability carries no explicit unit.
_KEY_UNIT_SUFFIXES = frozenset({"nm", "s", "hours", "days", "um", "pm"})

#: Length-unit spellings → nanometres. One table: a number written in any of
#: these is compared to the value after conversion when the axis is a length.
_LENGTH_TO_NM: dict[str, float] = {
    "nm": 1.0,
    "å": 0.1,
    "angstrom": 0.1,
    "angstroms": 0.1,
    "pm": 0.001,
    "µm": 1000.0,
    "μm": 1000.0,
    "um": 1000.0,
}

_NUMBER_RE = re.compile(r"(?<![\w.])\d+(?:,\d{3})*(?:\.(\d+))?")
_PLUS_MINUS = ("±", "+/-", "+-", "∓")
_UNCERTAINTY_CUE_RE = re.compile(
    r"\b(?:uncertaint|error|resolution|precision)\w*", re.IGNORECASE
)
_CUE_WINDOW = 60


@dataclass(frozen=True)
class SupplyQuoteCheck:
    verdict: Literal["ok", "refuse", "hold"]
    reason: str = ""


def _collapse_ws(text: str) -> str:
    return " ".join(text.split())


def _axis_unit(key: str, unit: str | None) -> str | None:
    if unit and unit.strip():
        return unit.strip()
    suffix = key.rsplit("_", 1)[-1] if "_" in key else ""
    return suffix if suffix in _KEY_UNIT_SUFFIXES else None


def _follows_unit(rest: str, unit: str) -> bool:
    """True when ``rest`` (text after a number) starts, after optional
    whitespace, with ``unit`` as a whole word (``nm`` is not ``nmol``)."""
    tail = rest.lstrip().lower()
    u = unit.lower()
    return tail.startswith(u) and not (len(tail) > len(u) and tail[len(u)].isalpha())


def _following_length_unit(rest: str) -> str | None:
    for u in sorted(_LENGTH_TO_NM, key=len, reverse=True):
        if _follows_unit(rest, u):
            return u
    return None


def check_supply_quote(
    quote: str, card_text: str, value: float, unit: str | None
) -> SupplyQuoteCheck:
    """Deterministic gate between a model-extracted supply value and the paper
    text it claims to come from. ``unit`` is the resolved axis unit
    (:func:`_axis_unit`); ``None`` skips the unit rule. Rules, in order:
    quote must be verbatim in ``card_text`` (whitespace-collapsed); a number
    in it must equal ``value`` within its written rounding (length units
    converted); the unit must follow it; a ``±`` number is an uncertainty
    (refuse); an uncertainty cue word near the number holds for review."""
    q = _collapse_ws(quote or "")
    if not q or q not in _collapse_ws(card_text or ""):
        return SupplyQuoteCheck("refuse", "quote not in the paper text")

    axis = unit.lower() if unit else None
    to_nm = _LENGTH_TO_NM.get(axis) if axis else None
    # (start, end, unit_ok) of every number in the quote that equals ``value``
    # within the rounding it was written at.
    matches: list[tuple[int, int, bool]] = []
    for m in _NUMBER_RE.finditer(q):
        n = float(m.group(0).replace(",", ""))
        tol = 0.5 * 10 ** -len(m.group(1) or "")
        rest = q[m.end() :]
        follow = _following_length_unit(rest)
        if to_nm is not None and follow is not None:
            f = _LENGTH_TO_NM[follow]
            if abs(n * f - value * to_nm) > tol * f + 1e-9:
                continue
        elif abs(n - value) > tol + 1e-9:
            continue
        if unit is None:
            unit_ok = True
        else:
            unit_ok = _follows_unit(rest, unit) or (
                to_nm is not None and follow is not None
            )
        matches.append((m.start(), m.end(), unit_ok))
    if not matches:
        return SupplyQuoteCheck("refuse", "value not stated in the quote")
    matches = [m for m in matches if m[2]]
    if not matches:
        return SupplyQuoteCheck("refuse", "unit not stated with the value")
    plain = [m for m in matches if not q[: m[0]].rstrip().endswith(_PLUS_MINUS)]
    if not plain:
        return SupplyQuoteCheck("refuse", "value is an uncertainty (±)")
    for start, end, _ in plain:
        window = q[max(0, start - _CUE_WINDOW) : end + _CUE_WINDOW]
        cue = _UNCERTAINTY_CUE_RE.search(window)
        if cue:
            return SupplyQuoteCheck("hold", f"uncertainty cue: {cue.group(0).lower()}")
    return SupplyQuoteCheck("ok")


def _bridge_prompt(
    store: Store,
    root: Ref,
    choice: RoleChoice,
    *,
    pathways: list[Ref],
    rungs: list[Ref],
    statuses: dict[int, str],
    benign: Ref | None,
) -> str:
    unit = f" {choice.unit}" if choice.unit else ""
    demanded = f"{choice.demanded:g}{unit}" if choice.demanded is not None else "?"
    supply = (
        f"{choice.best_supply:g}{unit} [{', '.join(choice.best_supply_evidence)}]"
        if choice.best_supply is not None
        else "none"
    )
    path_lines = (
        "\n".join(
            f"- {_handle('quest', p.id)} {(p.title or '').splitlines()[0][:100]}"
            for p in pathways
        )
        or "(no pathway quest serves the root yet — a new one is the only option)"
    )
    rung_lines = []
    for r in rungs[:_BRIDGE_MAX_RUNGS]:
        produces = "; ".join(
            f"{e.get('capability')}.{e.get('key')}={e.get('value')}"
            for e in _rung_entries(r, "produces")
        )
        rung_lines.append(
            f"- {_handle('todo', r.id)} [{statuses.get(r.id, 'open')}] "
            f"{(r.title or '').splitlines()[0][:90]}"
            + (f" — produces {produces}" if produces else "")
        )
    benign_text = "(no benign capability under this root)"
    if benign is not None:
        axes = ", ".join(
            f"`{k}`" + (f" [{u}]" if u else "")
            for k, _s, u in ledger.capability_axes(benign)
        )
        benign_text = (
            f"{_handle('quest', benign.id)} {(benign.title or '').splitlines()[0][:80]} "
            f"— axes {axes}. A TERMINAL rung (its product is consumed by no other "
            "rung) must treat this capability as required: its `consumes` will "
            "be extended with the benign axes. For an intermediate rung it is a "
            "preference only."
        )
    return (
        f"## Root striving\n{root.title.strip()}\n\n"
        f"## Capability {choice.capability}\n{_statement(choice)}\n"
        f"{_axis_line(choice)}\ndemanded: {demanded}\nbest cited supply: {supply}\n\n"
        f"## Pathways (bets) serving the root\n{path_lines}\n\n"
        f"## Existing rungs on this capability\n"
        f"{chr(10).join(rung_lines) or '(none)'}\n\n"
        f"## Benign gate\n{benign_text}\n\n"
        "## Task\n"
        "Compare demand vs best supply and pick ONE outcome:\n"
        "(a) supply meets or can be made to meet demand → `rung`: a completable "
        "step whose `produces` cites the number (evidence = the finding "
        "handles above, or new ones you name);\n"
        "(b) shortfall a pathway plausibly covers → `rung` whose deliverable is "
        "the shortfall, served by that pathway;\n"
        "(c) shortfall no existing pathway covers → `pathway`: a new bet "
        "serving the root;\n"
        "(d) nothing plausible → `dead-end` carrying the numbers.\n"
        "Return STRICT JSON:\n"
        '{"outcome": "rung" | "pathway" | "dead-end",\n'
        ' "rung": {"title": "<short imperative title>", "deliverable": "<2-4 '
        'sentences>", "pathway": "qu<id>", "consumes": [{"capability": '
        '"qu<id>", "key": "<axis>", "value": <number>}], "produces": '
        '[{"capability": "qu<id>", "key": "<axis>", "value": <number>, '
        '"evidence": ["fi<id>", ...]}]},\n'
        ' "pathway": {"title": "<short title>", "statement": "<the bet, 2-4 '
        'sentences>"},\n'
        ' "dead_end": {"reason": "<why no pathway can close the gap>"}}\n'
        "Fill only the branch matching `outcome`."
    )


def _bridge_context(
    store: Store, root_id: int, choice: RoleChoice
) -> tuple[list[Ref], list[Ref], dict[int, str], Ref | None, list[Ref]]:
    servers = _live_servers(store, root_id)
    capabilities = [r for r in servers if ledger.is_capability_quest(r)]
    pathways = _pathways(servers)
    cap = store.get_ref(kind="quest", id=choice.capability_id)
    under = _rungs_under(store, [cap]) if cap is not None else []
    rungs = [r.ref for r in under]
    statuses = {r.ref.id: r.status for r in under}
    return pathways, rungs, statuses, _benign_capability(capabilities), capabilities


def build_role_prompt(store: Store, root: Ref, choice: RoleChoice) -> str:
    """The first (or only) model prompt for ``choice`` — what ``--dry-run``
    prints."""
    if choice.role == ROLE_DEMAND:
        return _demand_prompt(store, root, choice)
    if choice.role == ROLE_SUPPLY:
        return _supply_search_prompt(store, root, choice)
    pathways, rungs, statuses, benign, _caps = _bridge_context(store, root.id, choice)
    return _bridge_prompt(
        store,
        root,
        choice,
        pathways=pathways,
        rungs=rungs,
        statuses=statuses,
        benign=benign,
    )


# ── model call ────────────────────────────────────────────────────────


def _ask(client: Any, prompt: str) -> dict[str, Any]:
    """One completion → parsed JSON object (``{}`` on an unparseable reply)."""
    out = client.complete(
        [
            {"role": "system", "content": _SYS_COMMON},
            {"role": "user", "content": prompt},
        ]
    )
    data = getattr(out, "data", None)
    if isinstance(data, dict) and data:
        return data
    parsed = extract_json_object(getattr(out, "text", "") or "")
    return parsed if isinstance(parsed, dict) else {}


def _quest_handler(store: Store) -> Any:
    from precis.dispatch import Hub
    from precis.handlers.quest import QuestHandler

    return QuestHandler(hub=Hub(store=store))


def _todo_handler(store: Store) -> Any:
    from precis.dispatch import Hub
    from precis.handlers.todo import TodoHandler

    return TodoHandler(hub=Hub(store=store))


def _merged_axis_map(store: Store, capability_id: int, field: str) -> dict[str, Any]:
    cap = store.get_ref(kind="quest", id=capability_id)
    existing = (cap.meta or {}).get(field) if cap is not None else None
    return dict(existing) if isinstance(existing, dict) else {}


# ── the three roles ───────────────────────────────────────────────────


def _run_demand(
    store: Store, client: Any, root: Ref, choice: RoleChoice, prompt: str
) -> dict[str, Any]:
    reply = _ask(client, prompt)
    value = num(reply.get("value"))
    source = str(reply.get("source") or "").strip()
    reason = str(reply.get("reason") or "").strip()
    calculation = str(reply.get("calculation") or "").strip()
    if value is None or not source or not reason:
        return {"note": "demand: model gave no usable {value, source, reason}"}
    demand = _merged_axis_map(store, choice.capability_id, "demand")
    demand[choice.key] = {"value": value, "source": source, "reason": reason}
    # Through the handler so the shape gate fires — never raw stamp_ref_meta.
    _quest_handler(store).edit(id=choice.capability_id, meta={"demand": demand})
    unit = f" {choice.unit}" if choice.unit else ""
    append_entry(
        store,
        choice.capability_id,
        text=(
            f"demand on `{choice.key}` set to {value:g}{unit} from {source}: "
            f"{reason}" + (f"\n\n{calculation}" if calculation else "")
        ),
        entry_type="decision",
        by="agent",
    )
    return {
        "note": f"demand: {choice.capability}.{choice.key} = {value:g}{unit} ({source})",
        "demand_written": {"key": choice.key, "value": value, "source": source},
    }


def _hold_supply_finding(
    store: Store,
    choice: RoleChoice,
    hub_id: int,
    paper: str,
    value: float,
    reason: str,
    quote: str,
) -> None:
    """A held finding keeps its hub (grounded) but is tagged for review and
    logged on the capability; it never counts toward the supply value."""
    from precis.store import Tag

    try:
        store.add_tag(hub_id, Tag.open(_SUPPLY_HELD_TAG), set_by="agent")
    except Exception:
        log.exception("roadmap_tick: could not tag held hub fi%s", hub_id)
    unit = f" {choice.unit}" if choice.unit else ""
    append_entry(
        store,
        choice.capability_id,
        text=(
            f"supply on `{choice.key}` HELD for review ({reason}): "
            f"{value:g}{unit} from {paper}, hub {_handle('finding', hub_id)}; "
            f"quote: {quote.strip()}"
        ),
        entry_type="observation",
        by="agent",
    )


def _run_supply(
    store: Store,
    client: Any,
    root: Ref,
    choice: RoleChoice,
    prompt: str,
    *,
    search_fn: Any,
    embedder: Any,
) -> dict[str, Any]:
    """One supply tick, with per-key escalation (measured in prod 2026-10-02:
    every query got 10 local hits, so local-first never searched outside and
    every tick came back dry). Each tick appends one ``supply_outcome``
    observation on the capability's logbook, read back via
    :func:`~precis.quest.roadmap_ledger.supply_history`. After a dry tick the
    next one runs with ``search_fn.force_external`` set — the attribute is
    flipped for this call only and restored in ``finally`` (the caller owns
    ``search_fn`` and reuses it across ticks and keys; a constructed copy
    would have to re-plumb hub and embedder; non-:class:`AcquiringSearch`
    fakes are left alone). Once two ticks that searched outside have both come
    back dry, escalation stops (local-first as usual) and ONE
    ``supply_not_found_outside`` entry is logged per dry run, which the
    ``unmet-capability`` gap line then shows. Escalated ticks whose outside
    search FAILED (429, outage, no S2 client) are bounded too: after two of
    them in a dry run escalation stops the same way and ONE
    ``supply_outside_failed`` entry is logged — it names the error and says
    nothing about the literature. Both stops last until a supply lands."""
    from precis.quest.search import AcquiringSearch

    hist = ledger.supply_history(store, choice.capability_id, choice.key)
    escalate = False
    if hist.ext_dry >= 2:
        if hist.not_found_queries is None:
            append_entry(
                store,
                choice.capability_id,
                text=(
                    f"`{choice.key}`: not found outside after {hist.ext_dry} "
                    f"escalated supply ticks; queries: "
                    f"{'; '.join(hist.ext_queries) or 'none'}"
                ),
                entry_type="observation",
                by="agent",
                extra_meta={
                    ledger.SUPPLY_NOT_FOUND_META: {
                        "key": choice.key,
                        "queries": list(hist.ext_queries),
                    }
                },
            )
    elif hist.ext_failed >= _OUTSIDE_FAIL_BOUND:
        if hist.outside_failed_ticks is None:
            append_entry(
                store,
                choice.capability_id,
                text=(
                    f"`{choice.key}`: outside search failed on {hist.ext_failed} "
                    f"escalated supply ticks ({hist.ext_failed_error}); "
                    f"escalation stopped until a supply lands"
                ),
                entry_type="observation",
                by="agent",
                extra_meta={
                    ledger.SUPPLY_OUTSIDE_FAILED_META: {
                        "key": choice.key,
                        "error": hist.ext_failed_error,
                        "ticks": hist.ext_failed,
                    }
                },
            )
    elif hist.streak >= 1 and isinstance(search_fn, AcquiringSearch):
        escalate = True

    queries: list[str] = []
    prior = getattr(search_fn, "force_external", False)
    if escalate:
        search_fn.force_external = True
    try:
        result = _supply_tick(
            store,
            client,
            choice,
            prompt,
            search_fn=search_fn,
            embedder=embedder,
            queries=queries,
        )
    finally:
        if escalate:
            search_fn.force_external = prior

    # Only an outside search that returned counts toward the two-tick bound:
    # a failed one (429, outage, missing S2 client) says nothing about the
    # literature, so it must not end in "not found outside".
    external = False
    external_error: str | None = None
    for q in queries:
        rep = getattr(search_fn, "report_for", lambda _q: None)(q)
        if rep is None or not rep.external_ran:
            continue
        if rep.external_error:
            external_error = external_error or rep.external_error
        else:
            external = True
    written = result.get("supply_written")
    unit = f" {choice.unit}" if choice.unit else ""
    if written:
        text = f"supply on `{choice.key}` wrote {written['value']:g}{unit}"
    elif external_error and not external:
        text = f"supply on `{choice.key}` dry (outside search failed: {external_error})"
    else:
        text = (
            f"supply on `{choice.key}` dry (outside searched: "
            f"{'yes' if external else 'no'})"
        )
    append_entry(
        store,
        choice.capability_id,
        text=text,
        entry_type="observation",
        by="agent",
        extra_meta={
            ledger.SUPPLY_OUTCOME_META: {
                "v": ledger.SUPPLY_OUTCOME_VERSION,
                "key": choice.key,
                "dry": not written,
                "external": external,
                "queries": queries,
                **({"external_error": external_error} if external_error else {}),
            }
        },
    )
    return result


def _supply_tick(
    store: Store,
    client: Any,
    choice: RoleChoice,
    prompt: str,
    *,
    search_fn: Any,
    embedder: Any,
    queries: list[str],
) -> dict[str, Any]:
    """The supply tick proper; appends the query strings it runs to
    ``queries`` for :func:`_run_supply`'s outcome entry."""
    from precis.quest.search import MAX_QUERIES, _parse_search_entry, run_search_step

    reply = _ask(client, prompt)
    raw_searches = reply.get("searches")
    searches = (
        [s for s in raw_searches if isinstance(s, (str, dict))]
        if isinstance(raw_searches, list)
        else []
    )
    searches_run = papers_linked = 0
    if searches:
        for raw in searches[:MAX_QUERIES]:
            parsed = _parse_search_entry(raw)
            if parsed is not None:
                queries.append(parsed.query)
        step = run_search_step(
            store,
            choice.capability_id,
            searches,
            by="agent",
            search_fn=search_fn,
            embedder=embedder,
        )
        searches_run, papers_linked = step.queries_run, step.papers_linked

    papers = _paper_servers(store, choice.capability_id)
    hubs: list[str] = []
    best: float | None = None
    best_evidence: list[str] = []
    refused = held = 0
    # Papers with no body text never reach the findings prompt: a title-only
    # card invites a number recalled from memory (pa459574, 2026-10-02).
    cards = _supply_cards(store, papers)
    if cards:
        reply2 = _ask(client, _supply_findings_prompt(store, choice, cards))
        raw_findings = reply2.get("findings")
        findings = (
            [f for f in raw_findings if isinstance(f, dict)]
            if isinstance(raw_findings, list)
            else []
        )
        by_handle = {_handle("paper", c.paper.id): c for c in cards}
        axis_unit = _axis_unit(choice.key, choice.unit)
        from precis.dispatch import Hub
        from precis.handlers.finding import FindingHandler

        fh = FindingHandler(hub=Hub(store=store))
        for f in findings:
            claim = str(f.get("claim") or "").strip()
            value = num(f.get("value"))
            paper = str(f.get("paper") or "").strip()
            card = by_handle.get(paper)
            if not claim or value is None or card is None:
                continue
            quote = str(f.get("quote") or "")
            check = check_supply_quote(quote, card.text, value, axis_unit)
            if check.verdict == "refuse":
                refused += 1
                log.info(
                    "roadmap_tick: supply finding refused: paper=%s value=%g: %s",
                    paper,
                    value,
                    check.reason,
                )
                continue
            supporter: dict[str, Any] = {"paper": paper}
            if card.chunk_id is not None:
                supporter["source_handle"] = handle_registry.format_handle(
                    "paper", card.chunk_id, chunk=True
                )
            try:
                # dedup=False: the semantic-dedup cascade needs an embedder
                # this worker-side hub does not carry; a repeated claim
                # sentence still converges onto the same hub (pub_id).
                resp = fh.put(title=claim, supporters=[supporter], dedup=False)
            except Exception:
                log.exception("roadmap_tick: hub mint failed for %r", claim[:80])
                continue
            m = _HUB_ID_RE.search(resp.body or "")
            if m is None:
                continue
            hub_id = int(m.group(1))
            hub = _handle("finding", hub_id)
            hubs.append(hub)
            if check.verdict == "hold":
                held += 1
                _hold_supply_finding(
                    store, choice, hub_id, paper, value, check.reason, quote
                )
                continue
            if _improved(best, value, choice.sense):
                best, best_evidence = value, [hub]
            elif value == best and hub not in best_evidence:
                best_evidence.append(hub)

    note = f"supply: {searches_run} search(es), {papers_linked} paper(s) linked, {len(hubs)} hub(s)"
    if refused:
        note += f", {refused} refused"
    if held:
        note += f", {held} held"
    counts = {"refused": refused, "held": held}
    if best is None:
        return {
            "note": note + ", no quantified claim → supply not written",
            "searches_run": searches_run,
            "papers_linked": papers_linked,
            "hubs": hubs,
            **counts,
        }
    # Only write when it beats (or first fills) the stored supply — a worse
    # citation must not overwrite a better one.
    stored = ledger.best_supply(store, choice.capability_id, choice.key, choice.sense)
    if not _improved(stored[0], best, choice.sense):
        return {
            "note": note + f", best {best:g} does not beat stored {stored[0]:g}",
            "searches_run": searches_run,
            "papers_linked": papers_linked,
            "hubs": hubs,
            **counts,
        }
    supply = _merged_axis_map(store, choice.capability_id, "supply")
    supply[choice.key] = {"value": best, "evidence": best_evidence}
    _quest_handler(store).edit(id=choice.capability_id, meta={"supply": supply})
    unit = f" {choice.unit}" if choice.unit else ""
    append_entry(
        store,
        choice.capability_id,
        text=(
            f"supply on `{choice.key}` cited at {best:g}{unit} "
            f"[{', '.join(best_evidence)}] from {len(hubs)} finding hub(s)"
        ),
        entry_type="observation",
        by="agent",
    )
    return {
        "note": note + f", supply {choice.key} = {best:g}{unit}",
        "searches_run": searches_run,
        "papers_linked": papers_linked,
        "hubs": hubs,
        "supply_written": {"key": choice.key, "value": best, "evidence": best_evidence},
        **counts,
    }


def rung_is_terminal(store: Store, rung_id: int, root_id: int) -> bool:
    """Derived (never annotated): a rung whose ``produces`` appears in no
    OTHER rung's ``consumes`` under the same root is terminal. An explicit
    ``meta.rung.benign == "required"`` overrides upward only."""
    rung = store.get_ref(kind="todo", id=rung_id)
    if rung is None:
        return False
    if ((rung.meta or {}).get("rung") or {}).get("benign") == "required":
        return True
    capabilities = ledger.capability_servers(store, root_id)
    others = [r.ref for r in _rungs_under(store, capabilities) if r.ref.id != rung_id]
    return _produces_unconsumed(rung, others)


def _produces_unconsumed(rung_or_meta: Ref | dict[str, Any], others: list[Ref]) -> bool:
    if isinstance(rung_or_meta, dict):
        produces = [
            e for e in rung_or_meta.get("produces") or [] if isinstance(e, dict)
        ]
    else:
        produces = _rung_entries(rung_or_meta, "produces")
    produced = {(e.get("capability"), e.get("key")) for e in produces}
    if not produced:
        return True
    consumed = {
        (e.get("capability"), e.get("key"))
        for other in others
        for e in _rung_entries(other, "consumes")
    }
    return not (produced & consumed)


def _clean_entries(raw: Any, *, produces: bool) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not isinstance(raw, list):
        return out
    for e in raw:
        if not isinstance(e, dict):
            continue
        value = num(e.get("value"))
        cap = e.get("capability")
        key = e.get("key")
        if value is None or not isinstance(cap, str) or not isinstance(key, str):
            continue
        entry: dict[str, Any] = {
            "capability": cap.strip(),
            "key": key.strip(),
            "value": value,
        }
        if produces:
            ev = e.get("evidence")
            entry["evidence"] = (
                [h.strip() for h in ev if isinstance(h, str) and h.strip()]
                if isinstance(ev, list)
                else []
            )
        out.append(entry)
    return out


def _run_bridge(
    store: Store, client: Any, root: Ref, choice: RoleChoice, prompt: str
) -> dict[str, Any]:
    pathways, rungs, statuses, benign, capabilities = _bridge_context(
        store, root.id, choice
    )
    reply = _ask(client, prompt)
    outcome = str(reply.get("outcome") or "").strip().lower()
    unit = f" {choice.unit}" if choice.unit else ""

    if outcome == "dead-end":
        reason = str((reply.get("dead_end") or {}).get("reason") or "").strip()
        if not reason:
            return {"note": "bridge: dead-end with no reason — nothing written"}
        supply = (
            f"{choice.best_supply:g}{unit} [{', '.join(choice.best_supply_evidence)}]"
            if choice.best_supply is not None
            else "none"
        )
        demanded = f"{choice.demanded:g}" if choice.demanded is not None else "?"
        text = (
            f"dead-end on `{choice.key}`: demanded "
            f"{demanded}{unit}, best supply {supply} — {reason}"
        )
        append_entry(
            store, choice.capability_id, text=text, entry_type="dead-end", by="agent"
        )
        append_entry(
            store,
            root.id,
            text=f"{choice.capability} {text}",
            entry_type="observation",
            by="agent",
        )
        return {
            "note": f"bridge: dead-end on {choice.capability}.{choice.key}",
            "dead_end": reason,
        }

    if outcome == "pathway":
        raw_spec = reply.get("pathway")
        spec: dict[str, Any] = raw_spec if isinstance(raw_spec, dict) else {}
        title = str(spec.get("title") or "").strip()
        statement = str(spec.get("statement") or "").strip()
        if not title:
            return {"note": "bridge: pathway with no title — nothing minted"}
        text = f"{title}\n\n{statement}" if statement else title
        resp = _quest_handler(store).put(text=text, tags=["STATUS:dormant"])
        pid = _parse_quest_handle(_first_handle(resp.body, "qu"))
        if pid is None:
            return {"note": "bridge: pathway mint returned no handle"}
        mark_roadmap_quest(store, pid)
        store.add_link(
            src_ref_id=pid, dst_ref_id=root.id, relation="serves", set_by="agent"
        )
        append_entry(
            store,
            root.id,
            text=(
                f"new pathway {_handle('quest', pid)} ({title}) minted dormant to "
                f"cover the shortfall on {choice.capability}.{choice.key}"
            ),
            entry_type="decision",
            by="agent",
        )
        return {
            "note": f"bridge: new pathway {_handle('quest', pid)} (dormant)",
            "pathway_id": pid,
        }

    if outcome != "rung":
        return {"note": f"bridge: unrecognised outcome {outcome!r} — nothing written"}

    raw_rung = reply.get("rung")
    spec = raw_rung if isinstance(raw_rung, dict) else {}
    title = str(spec.get("title") or "").strip()
    if not title:
        return {"note": "bridge: rung with no title — nothing minted"}
    pathway_id = _parse_quest_handle(spec.get("pathway"))
    valid_pathways = {p.id for p in pathways}
    if pathway_id not in valid_pathways:
        if len(valid_pathways) == 1:
            pathway_id = next(iter(valid_pathways))
        else:
            return {
                "note": (
                    f"bridge: rung names pathway {spec.get('pathway')!r}, not one "
                    "serving this root — nothing minted"
                )
            }
    assert pathway_id is not None
    consumes = _clean_entries(spec.get("consumes"), produces=False)
    produces = _clean_entries(spec.get("produces"), produces=True)
    if not produces:
        produces = [
            {
                "capability": choice.capability,
                "key": choice.key,
                "value": choice.best_supply
                if choice.best_supply is not None
                else choice.demanded,
                "evidence": list(choice.best_supply_evidence),
            }
        ]
    # A produces entry citing nothing may borrow the best supply's evidence
    # only when it claims no more than that supply delivers; a better number
    # with no citation is exactly what "no number, no rung" refuses.
    for e in produces:
        if e["evidence"]:
            continue
        borrowable = (
            e["capability"] == choice.capability
            and e["key"] == choice.key
            and choice.best_supply is not None
            and choice.best_supply_evidence
            and not _improved(choice.best_supply, e["value"], choice.sense)
        )
        if borrowable:
            e["evidence"] = list(choice.best_supply_evidence)
        else:
            return {
                "note": (
                    f"bridge: rung produces {e['capability']}.{e['key']}="
                    f"{e['value']:g} with no evidence — no number, no rung"
                )
            }

    # Near-dup gate over sibling rung titles (token-Jaccard measure), BEFORE
    # the mint. AttemptNode is only the measure's input shape here — nothing
    # is written to any attempt ledger.
    siblings = [
        AttemptNode(text=(r.title or "").splitlines()[0], status="open", seq=r.id)
        for r in rungs
    ]
    dup = _find_near_dup_node(siblings, title)
    if dup is not None:
        return {
            "note": f"bridge: rung {title!r} is a near-dup of {_handle('todo', dup.seq)} — not minted",
            "near_dup_of": dup.seq,
        }

    # Terminal-rung benign gate: derived from what the OTHER rungs consume.
    rung_meta: dict[str, Any] = {
        "pathway": _handle("quest", pathway_id),
        "consumes": consumes,
        "produces": produces,
    }
    all_rungs = [r.ref for r in _rungs_under(store, capabilities)]
    terminal = _produces_unconsumed(rung_meta, all_rungs) or (
        str(spec.get("benign") or "").strip().lower() == "required"
    )
    benign_added: list[str] = []
    if terminal and benign is not None:
        b_handle = _handle("quest", benign.id)
        b_demand = (benign.meta or {}).get("demand")
        have = {(c["capability"], c["key"]) for c in consumes}
        for key, _sense, _unit in ledger.capability_axes(benign):
            entry = b_demand.get(key) if isinstance(b_demand, dict) else None
            value = num(entry.get("value")) if isinstance(entry, dict) else None
            if value is None or (b_handle, key) in have:
                continue
            consumes.append({"capability": b_handle, "key": key, "value": value})
            benign_added.append(key)

    deliverable = str(spec.get("deliverable") or "").strip()
    text = f"{title}\n\n{deliverable}" if deliverable else title
    resp = _todo_handler(store).put(
        text=text, meta={"rung": rung_meta}, tags=list(RUNG_TAGS)
    )
    rid = _first_id(resp.body)
    if rid is None:
        return {"note": "bridge: rung mint returned no id"}
    store.add_link(
        src_ref_id=rid, dst_ref_id=pathway_id, relation="serves", set_by="agent"
    )
    store.add_link(
        src_ref_id=rid,
        dst_ref_id=choice.capability_id,
        relation="serves",
        set_by="agent",
    )
    append_entry(
        store,
        root.id,
        text=(
            f"rung {_handle('todo', rid)} ({title}) minted open, waiting-for:reto, "
            f"on {choice.capability}.{choice.key} via {_handle('quest', pathway_id)}"
            + (" — terminal: benign axes added to consumes" if benign_added else "")
        ),
        entry_type="decision",
        by="agent",
    )
    return {
        "note": f"bridge: rung {_handle('todo', rid)} minted ({'terminal' if terminal else 'intermediate'})",
        "rung_id": rid,
        "terminal": terminal,
        "benign_added": benign_added,
    }


def _first_handle(body: str, code: str) -> str | None:
    m = re.search(rf"\b{code}(\d+)\b", body or "")
    return f"{code}{m.group(1)}" if m else None


def _first_id(body: str) -> int | None:
    m = re.search(r"\bid=(\d+)", body or "")
    if m is not None:
        return int(m.group(1))
    h = _first_handle(body, "td")
    return int(h[2:]) if h else None


# ── pinned ledger chunk + deed ────────────────────────────────────────


def _ledger_chunk(store: Store, dossier_id: int) -> Any | None:
    return _find_pinned_chunk(store.drafts.reading_order(dossier_id), LEDGER_PINNED)


def previous_ledger_signature(store: Store, root_id: int) -> dict[str, float] | None:
    """The signature the root's current pinned ledger chunk carries, or
    ``None`` when no chunk exists yet (first tick)."""
    from precis.quest.dossier import dossier_ref_id

    did = dossier_ref_id(store, root_id)
    if did is None:
        return None
    chunk = _ledger_chunk(store, did)
    if chunk is None:
        return None
    raw = (chunk.meta or {}).get("signature")
    if not isinstance(raw, dict):
        return {}
    return {str(k): float(v) for k, v in raw.items() if num(v) is not None}


def update_capability_ledger_chunk(
    store: Store, root_id: int, rows: list[ledger.LedgerRow]
) -> str:
    """Regenerate the root dossier's ONE ``meta.pinned='capability-ledger'``
    chunk from ``rows`` — rewritten in place, never appended — and stamp
    the ledger signature on its meta for the next tick's deed diff. Pattern
    of :func:`precis.quest.dossier._ensure_frontier_tree_chunk_for_ref`."""
    did = ensure_dossier(store, root_id)
    found = _ledger_chunk(store, did)
    if found is not None:
        handle = str(found.handle)
    else:
        created = store.drafts.add_chunks(
            ref_id=did, chunk_kind="paragraph", text=_LEDGER_SEED, split=False
        )
        handle = str(created[0].handle)
        store.drafts.patch_chunk_meta(handle, {"pinned": LEDGER_PINNED})
    store.drafts.edit_text(
        handle,
        "## Capability ledger\n\n" + ledger.render_ledger_markdown(rows) + "\n",
        source={"reason": "quest-capability-ledger"},
        meta_patch={"signature": ledger.ledger_signature(rows)},
    )
    return handle


def _improved(old: float | None, new: float, sense: str) -> bool:
    if old is None:
        return True
    return new < old if sense == "min" else new > old


def ledger_improvements(
    before: dict[str, float] | None, rows: list[ledger.LedgerRow]
) -> dict[str, tuple[float | None, float]]:
    """``{"qu<id>:<key>": (old, new)}`` for every axis whose best supply
    improved in its ``sense`` since ``before``. ``before=None`` (no prior
    ledger chunk) yields nothing — there is no previous ledger to have
    improved since."""
    if before is None:
        return {}
    out: dict[str, tuple[float | None, float]] = {}
    for r in rows:
        if r.best_supply is None:
            continue
        sig = f"{r.capability}:{r.key}"
        old = before.get(sig)
        if _improved(old, r.best_supply, r.sense):
            out[sig] = (old, r.best_supply)
    return out


def stamp_deeds(
    store: Store,
    root_id: int,
    rows: list[ledger.LedgerRow],
    delta: dict[str, tuple[float | None, float]],
) -> int:
    """One ``milestone`` (by ``system``) on the capability AND on the root
    per improved axis. Returns the number of entries written."""
    by_sig = {f"{r.capability}:{r.key}": r for r in rows}
    n = 0
    for sig, (old, new) in delta.items():
        row = by_sig.get(sig)
        if row is None:
            continue
        cap_id = _parse_quest_handle(row.capability)
        if cap_id is None:
            continue
        unit = f" {row.unit}" if row.unit else ""
        was = f"{old:g}" if old is not None else "none"
        cite = (
            f" [{', '.join(row.best_supply_evidence)}]"
            if row.best_supply_evidence
            else ""
        )
        text = f"supply on `{row.key}` improved {was} → {new:g}{unit}{cite}"
        append_entry(store, cap_id, text=text, entry_type="milestone", by="system")
        append_entry(
            store,
            root_id,
            text=f"{row.capability} {text}",
            entry_type="milestone",
            by="system",
        )
        n += 2
    return n


# ── the tick ──────────────────────────────────────────────────────────


def roadmap_tick(
    store: Store,
    client: Any,
    quest_id: int,
    *,
    dry_run: bool = False,
    search_fn: Any | None = None,
    embedder: Any | None = None,
) -> dict[str, Any]:
    """Run one roadmap tick against root ``quest_id``.

    Returns the weave-shaped dict the coordinator reads — ``ok``,
    ``applied``, ``note`` — plus ``role`` (``None`` when no gap maps to
    one), ``gap`` (the acted-on :class:`Gap`, as a dict), ``ledger_delta``
    (``{"qu<id>:<key>": [old, new]}`` improvements since the previous
    ledger chunk), ``improved`` (engagement), ``dry`` (no gap-count change
    and no ledger change), ``gap_count`` (``[before, after]``), ``deeds``
    (milestone entries stamped) and the role's own fields (``rung_id`` /
    ``pathway_id`` / ``hubs`` / ``demand_written`` / ``supply_written`` /
    ``dead_end``). ``dry_run`` selects the role and assembles the prompt
    but makes no model call and no write (``prompt`` is returned).

    ``search_fn``/``embedder`` feed the supply role's lit-search
    (:func:`precis.quest.search.run_search_step`); ``None`` uses the
    held-corpus lexical default.
    """
    root = _is_roadmap_root(store, quest_id)
    if root is None:
        return {"ok": False, "error": "not_a_roadmap_root", "role": None, "gap": None}

    choice = roadmap_role(store, quest_id)
    start_sig = ledger.ledger_signature(ledger.compute_ledger(store, quest_id))
    # The deed baseline is the PREVIOUS pinned ledger chunk (so a rung a
    # human completed between ticks lands its deed here); on the very first
    # tick there is none, so the tick's own start state stands in — what
    # already stood before this body ever ran is not a deed, what this
    # tick itself improves is.
    prev_sig = previous_ledger_signature(store, quest_id)
    before_sig = prev_sig if prev_sig is not None else start_sig
    gaps_before = len(quest_gaps(store, quest_id))

    if choice is None:
        if dry_run:
            return {
                "ok": True,
                "applied": False,
                "role": None,
                "gap": None,
                "prompt": "",
                "ledger_delta": {},
                "note": "no capability axis needs demand, supply or a bridge",
            }
        rows = ledger.compute_ledger(store, quest_id)
        delta = ledger_improvements(before_sig, rows)
        deeds = stamp_deeds(store, quest_id, rows, delta)
        update_capability_ledger_chunk(store, quest_id, rows)
        return {
            "ok": True,
            "applied": True,
            "role": None,
            "gap": None,
            "ledger_delta": {k: list(v) for k, v in delta.items()},
            "improved": bool(delta),
            "dry": not delta and ledger.ledger_signature(rows) == start_sig,
            "gap_count": [gaps_before, len(quest_gaps(store, quest_id))],
            "deeds": deeds,
            "note": "no capability axis needs demand, supply or a bridge",
        }

    prompt = build_role_prompt(store, root, choice)
    if dry_run:
        return {
            "ok": True,
            "applied": False,
            "role": choice.role,
            "gap": asdict(choice.gap),
            "choice": choice.as_dict(),
            "tier": role_tier(choice.role),
            "prompt": prompt,
            "ledger_delta": {},
            "note": f"dry run — role {choice.role} on {choice.capability}.{choice.key}",
        }

    try:
        if choice.role == ROLE_DEMAND:
            result = _run_demand(store, client, root, choice, prompt)
        elif choice.role == ROLE_SUPPLY:
            result = _run_supply(
                store,
                client,
                root,
                choice,
                prompt,
                search_fn=search_fn,
                embedder=embedder,
            )
        else:
            result = _run_bridge(store, client, root, choice, prompt)
    except Exception as exc:
        log.exception("roadmap_tick: role %s raised on quest %s", choice.role, quest_id)
        return {
            "ok": False,
            "error": f"{choice.role} role raised: {exc}",
            "role": choice.role,
            "gap": asdict(choice.gap),
        }

    rows = ledger.compute_ledger(store, quest_id)
    delta = ledger_improvements(before_sig, rows)
    deeds = stamp_deeds(store, quest_id, rows, delta)
    update_capability_ledger_chunk(store, quest_id, rows)
    gaps_after = len(quest_gaps(store, quest_id))

    return {
        "ok": True,
        "applied": True,
        "role": choice.role,
        "gap": asdict(choice.gap),
        "tier": role_tier(choice.role),
        "ledger_delta": {k: list(v) for k, v in delta.items()},
        "improved": bool(delta),
        "dry": (
            not delta
            and gaps_after == gaps_before
            and ledger.ledger_signature(rows) == start_sig
        ),
        "gap_count": [gaps_before, gaps_after],
        "deeds": deeds,
        **result,
    }


def render_role_report(result: dict[str, Any]) -> str:
    """The CLI's ``--dry-run`` print: role, gap, then the assembled prompt."""
    gap = result.get("gap") or {}
    lines = [
        f"role: {result.get('role') or '(none)'}",
        f"tier: {result.get('tier') or '-'}",
        f"gap: {gap.get('kind', '-')}: {gap.get('detail', result.get('note', ''))}"
        + (f"  [{gap['handle']}]" if gap.get("handle") else ""),
        "",
        "── prompt ──",
        result.get("prompt") or "(no prompt — nothing to act on)",
    ]
    return "\n".join(lines)


__all__ = [
    "BENIGN_TOKEN",
    "LEDGER_PINNED",
    "ROLE_BRIDGE",
    "ROLE_DEMAND",
    "ROLE_SUPPLY",
    "ROLE_TIERS",
    "RUNG_TAGS",
    "RoleChoice",
    "build_role_prompt",
    "ledger_improvements",
    "previous_ledger_signature",
    "render_role_report",
    "roadmap_role",
    "roadmap_tick",
    "role_tier",
    "rung_is_terminal",
    "stamp_deeds",
    "update_capability_ledger_chunk",
]
