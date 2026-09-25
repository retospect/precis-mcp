# Align coined vocabulary to literature standard

**Why now.** The LLM-physical-model paper series (`td449706`, draft
`llm-physical-grounding`) presents this system as one implementation of a
known class. A paper cannot introduce a coined term where the literature
already has a good one: reviewers read it as either ignorance of the field or
a land-grab. Reto's ruling 2026-09-25: unify vocabulary to literature
standard **where the literature's word is good**, update `docs/glossary.md`,
and check MCP-facing names for consistency.

**Not a blanket rename.** Roughly two thirds of the coined set names something
the literature has no word for, and those stay. The work is picking the third
that has a standard equivalent, and distinguishing cheap changes from breaking
ones.

## Tier 1 — glossary gloss only, no rename (safe, do first)

Add the standard term to the existing entry as `(lit: <standard term>)`. No
code, no skills, no prod data touched. These are cases where the coined word
is fine in-house but a reader needs the bridge.

| coined | literature standard | source field |
|---|---|---|
| `fisheye` view, `eye` | fisheye view, **degree-of-interest function** | Furnas 1986, HCI — already the standard word; the DOI function is what `extent ladder` computes |
| `extent ladder` | degree-of-interest (DOI) function | Furnas 1986 |
| `frontier` | **Pareto frontier** | multi-objective optimisation — already standard, just say Pareto |
| `dossier` | **living review** / living systematic review | evidence synthesis |
| `termination node` | **stopping criterion** | numerical methods |
| `screening tier` | **screening** | design of experiments — already standard |
| `envelope` (se block) | **bounding volume** *if it is an outer bound* | collision detection; see polarity note below |
| `spray` / `dreamable` | **diversification** / exploration | search + recommender literature |
| `trust ladder`, `trust axis` | **provenance** vs **evidential support** | two distinct axes; the skill already separates them correctly |

## Tier 2 — rename in docs and skills (moderate; no DB migration)

The coined term is actively misleading or costs a reader real effort.

| coined | proposed | rationale |
|---|---|---|
| `abstraction ladder`, `rung 0..5` (se) | **conceptual / embodiment / detail design**, or FBS levels | Pahl & Beitz systematic design is the standard decomposition; Gero's function–behaviour–structure is the standard ontology. "Rung 3" carries no meaning to anyone outside this repo. See the unicycle finding below. |
| `fidelity ladder` | **fidelity levels** / model hierarchy | Peherstorfer, Willcox & Gunzburger (`pa449700`) is the canonical taxonomy (adaptation / fusion / filtering). "Ladder" implies a total order the literature does not assume. |
| `establishes` / `corroborates` / `contradicts` | align to **CiTO** (`cito:supports`, `cito:disagreesWith`) | CiTO is the deployed standard for citation intent; nanopublication (Groth et al.) is already the model for the hub itself, so the edge vocabulary should match rather than fork. |
| `atom` / `compound` claim | **atomic** / **composite** claim | "compound" collides with the chemistry sense in this very corpus — an overloaded-term problem of our own making |
| `brick` / `linker` (se atomic) | `linker` is already standard (MOF); **node / secondary building unit (SBU)** for `brick` | reticular chemistry vocabulary |

## Tier 3 — MCP-surface names (breaking; needs per-name sign-off)

Kind names are in prod rows, migrations, skills, agent prompts and a public
repo. **Do not rename without an explicit per-name decision.** Listed for the
decision, not scheduled.

- `se` — expands to what? If "structural element", the literature word for the
  graph it holds is **assembly** (assembly graph / liaison graph). `se` as a
  token is opaque at the MCP surface.
- `quest`, `gripe`, `tote`, `deed`, `striving` — the quest layer has no
  scientific-literature counterpart; these are fine as house words and should
  stay. Flagging only so the audit is complete.
- `oracle`, `make`, `conv` — check against their nearest standard sense before
  the paper describes them.

Most kind names (`paper`, `patent`, `finding`, `draft`, `figure`, `material`,
`protein`, `route`, `pathway`, `component`, `part`) are already the
literature's own nouns. The surface is in better shape than the prose.

## Polarity note — feeds the same paper

A separate finding from the pcb session (gr346004, gr449483) says any
geometric primitive that is an approximation must declare whether it is
`exact`, `outer` or `inner`, because a verdict is sound only in the direction
its approximation is sound in (sound over- vs under-approximation, Cousot &
Cousot 1977, stubbed as `pa449839`). This bears on `envelope` above: if
`envelope` is an outer bound it should say so in the name or the type, not
only in the glossary.

## Unicycle finding — the levels are not what is expensive (2026-09-25)

The question that prompted the tier-2 row: the live `se:unicycle-mk2` is
flat (15 blocks, no subassembly parents, wheel as one integral cylinder)
while `precis-se-design-help` teaches a conceptual → embodiment → detail
walk. Was the walk skipped because it costs an LLM author too much? No.

- The retired predecessor `unicycle-printed-v1` (29 blocks, 28 parented,
  laced wheel with 12 axial spokes) was LLM-authored 2026-09-11 in ~40 min
  and 13 revisions. `mk2` (2026-09-13) is a units-cutover port of it,
  re-authored three times over; no note, gripe or backlog item records a
  decision to author flat. The flatness is an artefact, not a ruling.
- Blind experiment 2026-09-25 (detail in gr450524): a Sonnet agent with no
  knowledge of either design, no repo access, a neutral brief (20 in wheel,
  80 kg rider, mostly FDM) and skills-only discovery produced
  `se:unicycle-c1` — 20 blocks, 19 parented, root box with `set_load`
  (1800 N saddle / 900 N pedal) and requirement measures, `validate`/`drc`
  run mid-authoring, a self-caught parent-relative pose bug, ~38 min. The
  pre-registered prediction (flat, no root, checks only at the end) was
  wrong on three of four counts.

Ruling for this item: keep the conceptual / embodiment / detail
decomposition as the taught workflow and rename it per tier 2; author cost
is not the obstacle. What still keeps the coarse levels from paying off in
full is tracked elsewhere and is not vocabulary work: gr334788 (rigid
connects excluded from the equilibrium matrix, so a rim ring or frame is
never checked as one body; folded into `se-feasibility-and-cost.md`
2026-09-25), gr450524 finding 1 (`view='order'`/`'bom'` drop a moded block the
moment it gets a child, so hierarchy silently costs printed parts), and the
unprofiled `validate`/`drc` slowness (30 s budget brushed at 20 blocks;
gr337045 was soft-deleted after only the server-death half was fixed).
`unicycle-c1` is the first live design with parent edges — the fixture all
three need.

Consequence for the skill rewrite: `precis-se-design-help`'s worked example
(541b417a) presents the flat `mk2`, i.e. the artefact. gr450093 rules that
skills name no live design at all, so the replacement should sketch the
hierarchical shape inline rather than repoint at `unicycle-c1`.

## Definition of done

- Tier 1 glosses added, one line each, in the existing entries.
- Tier 2 renamed in `docs/` and `src/precis/data/skills/`, with the old term
  kept as `(legacy: X)` inside the winner's glossary entry per the file's own
  retired-synonym convention.
- Tier 3 left open with a decision recorded per name.
- The paper draft uses the post-rename vocabulary throughout.

## Open

- CiTO alignment (tier 2, row 3) touches taproot verifier semantics — confirm
  with whoever owns the evidence-edge model before renaming.
- `envelope` → `bounding volume` is tier 1 only if envelopes really are outer
  bounds everywhere. Verify against `precis_se/geometry_plausibility.py`
  first; if some are exact and some are outer, this is tier 2 plus a type
  change.
