# Run 3 protocol: processes (S6)

Skeleton: `<drafts>/run3/skeleton.yaml`. Base formats: `<drafts>/pilot/PROTOCOL.md`
(Shared vocabulary; Stage 1 record format; Stage 2 verdicts) and `<drafts>/run2/PROTOCOL.md`
(dump_by_query, PDF extraction, excerpt-only). Where they conflict, this file wins.

## Which nodes

Every skeleton entry with a `batch:` is one query. Two kinds:
- a **topic** under a deep process (e.g. `process/additive/desktop-fdm/holes-and-clearances`): a rule
  topic of that process; record `role: leaf`, `coverage: deep`.
- a **shallow process** (e.g. `process/cutting/edm-wire`): one node for the whole process; record
  `role: process`, `coverage: shallow`.
Node id = the skeleton path from `process` (children/topics keys dropped), e.g.
`process/additive/desktop-fdm/holes-and-clearances`. Slug = id with `/` replaced by `__`.

## Stage 1: expand (one agent per batch). Never call more().

Per node:
1. Write the query text (template below) to `<drafts>/run3/q/<slug>.txt` with the Write tool.
2. Call `get(kind='perplexity-reasoning', q=<exactly the same text>)`. Note the `[cost: ~$X]` trailer if
   visible. Do NOT call more().
3. Run (one plain command):
   `python3 <drafts>/pilot/dump_by_query.py <repo> <drafts>/run3 <slug>`
   It writes the full answer to `<drafts>/run3/raw/<slug>.md`.
4. Read the raw file in full with the Read tool and write the node record from it. Map every `[N]`
   to the answer's own source list. Claims: every number or hard rule, one each.

Template (NAME first so cache keys differ; NAME = "<process name> <topic name>" for topics, e.g.
"Desktop FDM holes and clearances"; the process name for shallow nodes, e.g. "Wire EDM"):

> <NAME>: design for manufacturing (<CONTEXT>). Give concisely, with a citation for every number:
> (1) the design rules a part must meet, each with a number and unit (minimums, maximums, ratios,
> tolerances, radii, angles); (2) what each rule depends on (material, machine class, tool or nozzle
> size, part size); (3) common design mistakes and their fixes; (4) cost drivers, and when to choose
> a different process; (5) defects or weaknesses the process leaves in the part (anisotropy, residual
> stress, porosity, sink, warpage, notches, burrs) and how to design them out; (6) the standard, maker
> design guide or textbook each number comes from. Prefer standards (ISO 2768, ISO 286, ISO 20457,
> DIN 6935, ISO 8062) and maker or service-bureau design guides (Protolabs, Xometry, Fictiv, Hubs);
> for desktop 3D printing prefer the Prusa knowledge base and Bambu Lab wiki over Stratasys.

Record = pilot Stage 1 format with these changes:
- add `role`, `coverage`, `pplx`, `cost_usd`.
- drop `axes` (process nodes do not declare axes); keep `issues` with `failure_mode` from the shared
  list plus these process modes: anisotropy, residual-stress, porosity, sink, warpage, notch, burr,
  dimensional-error.
- shallow process nodes: `summary`, `when`, `avoid_when`, `variants`, `tradeoffs`, `howto`, `issues`,
  `claims`. Topic nodes: `summary`, `howto`, `issues`, `claims` (`when`/`avoid_when` only if the
  answer gives a real choice, e.g. heat-set insert vs captive nut).
- each claim: `equipment` from the vocabulary (the node's own class unless the answer says
  otherwise; a Stratasys number is `industrial-fdm`, never `desktop-fdm`), and
  `machine_specific: true` when the number depends on the particular printer/machine/tool (hole
  compensation, accuracy, min feature) — S1 moves those to `se_capabilities.json` via `table_ref`.

Output: `<drafts>/run3/draft/<batch>.yaml` (a YAML list of records) and `<batch>.log`
(`<node id>\t<pplx id>\t<cost>`). Quote every YAML string that contains `:` `,` `[` `]` `{` `}` or `#`;
run `uv run --with pyyaml python3 -c "import yaml,sys; yaml.safe_load(open(sys.argv[1]))" <file>`.
Hard rules: the only precis calls allowed are get(kind='perplexity-reasoning', q=...). No test calls.
No put/edit/tag/link/delete, no deep research, no git. Our own words only.

## Stage 2: verify (a different agent per batch)

Pilot Stage 2 without Step 0, plus run 2's raw-first source mapping, PDF extraction (into
`<drafts>/run3/scratch/`) and the `excerpt-only` verdict. No precis MCP calls at all.
Extra: for `machine_specific` claims, also note whether the source names the machine/material it was
measured on (`measured_on:`).

Output: `<drafts>/run3/verified/<batch>.yaml`, same records plus per-claim
`verdict, quote, source_quality, alt_url, note`, and a footer with counts per verdict and the 5
failures that matter most. Must load with yaml.safe_load (run the check).
