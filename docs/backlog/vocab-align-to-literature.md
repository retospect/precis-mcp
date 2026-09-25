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
