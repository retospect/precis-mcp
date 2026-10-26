# Run 2 protocol: rotate, close, principles

Skeleton: `<drafts>/run2/skeleton.yaml`. Shared vocabulary (criteria, failure modes, axes) and
the record format: `<drafts>/pilot/PROTOCOL.md` (read its "Shared vocabulary" and the Stage 1
record format). Changes from the pilot are below; where they conflict, this file wins.

## Stage 1: expand (one agent per batch). Never call more().

Per node, with slug = node path with `/` replaced by `__` (e.g. `rotate__plain-bearing__pin-in-hole`):

1. Write the query text (template below) to `<drafts>/run2/q/<slug>.txt` with the Write tool.
2. Call `get(kind='perplexity-reasoning', q=<exactly the same text>)`. Note the `[cost: ~$X]` trailer if
   visible. Do NOT call more(); the page is folded and you do not need it.
3. Run (one plain command):
   `python3 <drafts>/pilot/dump_by_query.py <repo> <drafts>/run2 <slug>`
   It prints `<slug> <pplx id> <bytes>` and writes the full answer to `<drafts>/run2/raw/<slug>.md`.
4. Read the raw file in full with the Read tool and write the node record from it. Map every `[N]`
   to the answer's own source list. Claims: every number or hard rule, one each.

Template (node name FIRST so cache keys differ):

> <NAME>: mechanical design (<CONTEXT>). Give concisely, with a citation for every number:
> (1) when to choose it and when to avoid it (load, speed, precision, separability, material,
> process, environment, quantity); (2) main variants; (3) tradeoffs vs alternatives; (4) common design
> mistakes and their fixes; (5) design numbers with units (dimensions, fits, clearances, ratios, limits)
> and the standard, manufacturer design guide or textbook they come from; for 3D printing prefer
> desktop FDM sources (Prusa knowledge base, Bambu Lab wiki) over industrial FDM (Stratasys); (6) failure
> modes (fatigue, wear, creep, corrosion, brittle fracture, loosening, thermal) and how to design them out.

For `principles/*` nodes use instead:

> <NAME>: mechanical design principle (<CONTEXT>). Give concisely, with a citation for every rule or
> number: (1) the principle stated as checks a designer can run on each part; (2) typical violations
> and their fixes, with concrete examples; (3) any numbers or rules of thumb with their source; (4) the
> established literature (books, standards) that defines it.

Record additions vs the pilot format:
- each claim gets `equipment:` from {desktop-fdm, industrial-fdm, sla, cnc-mill, lathe,
  injection-mould, sheet, cast, bought, any}.
- node gets `pplx:` (id printed by the script) and `cost_usd:` (from the trailer, or null).
- principles nodes: `checks:` list (each an imperative check run per part) instead of `howto`.

Output: `<drafts>/run2/draft/<batch>.yaml` and `<batch>.log` (`<node id>\t<pplx id>\t<cost>`).
Quote every YAML string that contains `:` `,` `[` `]` `{` `}` or `#`; the file must load with
`python3 -c "import yaml,sys; yaml.safe_load(open(sys.argv[1]))" <file>` (run it).
Hard rules: only precis calls allowed are get(kind='perplexity-reasoning', q=...). No test calls.
No put/edit/tag/link/delete, no deep research, no git. Our own words only.

## Stage 2: verify (a different agent per batch)

Same as the pilot Stage 2, without Step 0, plus:
- Read the raw answer `<drafts>/run2/raw/<slug>.md` to check each claim's `source_url`
  mapping first (fix and note `remapped`).
- PDFs: WebFetch often cannot read compressed PDFs. Download with `curl -sL -o <file> <url>` into
  `<drafts>/run2/scratch/` and extract with `gs -q -sDEVICE=txtwrite -o <file>.txt <file>`,
  then grep the text.
- Extra verdict `excerpt-only`: the value was seen only in a search-result excerpt, page not read.
- No precis MCP calls at all.

Output: `<drafts>/run2/verified/<batch>.yaml`, same records plus per-claim
`verdict, quote, source_quality, alt_url, note`, and a footer with counts per verdict and the 5
failures that matter most. Must load with yaml.safe_load (run the check).
