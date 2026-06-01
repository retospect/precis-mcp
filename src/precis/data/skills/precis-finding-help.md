---
id: precis-finding-help
title: precis — track an empirical claim back to its primary source
status: shipped
tier: 1
floor: any
applies-to: put / get / search (kind='finding')
last-updated: 2026-06-01
---

# precis-finding-help — track empirical claims to their primary source

A `finding` is a **retrievable empirical claim with explicit setup
context and a provenance chain back to its primary source**. The
chase worker walks the chain one hop per pass — the agent creates
the finding, drops the returned `pub_id` in their draft as a
placeholder, and `precis resolve` substitutes the primary
`cite_key` once the chase establishes the chain.

This skill is for **agents writing papers / reports / answers**
who need to track which numerical or empirical claims have
which primary sources, *under what setup*.

## The workflow shape

```
1. agent drafts a claim                : "the device was held at 2.4 kV for 30 s"
2. agent finds a supporting citation   : miller2020 ~ §3
3. agent SEARCHES for an existing      : search(kind='finding', q='2.4 kV gate dielectric 30 s')
   finding                               (search by claim text + scope)
4. (a) reuse if a setup-matching       : note the existing pub_id, skip to step 7
       finding exists                    
   (b) otherwise create a new finding  : put(kind='finding', ...)
5. agent drops the pub_id placeholder  : "…the device was held at 2.4 kV for 30 s [ab12c3]"
   in their draft
6. precis worker --only chase advances : chase walks miller → fischer → … → primary
   the chain over subsequent passes      (asynchronous; takes 1–N passes)
7. at document finalisation            : precis resolve manuscript.tex --format latex
   precis resolve substitutes            → \cite{fischer13} (primary cite_key)
```

The chase is **asynchronous**. The agent doesn't wait for it; the
placeholder is the contract that connects "I wrote this claim" with
"the system will figure out where it came from."

## When to create a finding

A finding is a quantitative or empirical claim whose **setup
context** matters to anyone re-using it. Create one when you find
yourself writing *"X = 2.4 kV"*, *"the experiment used 0.1 mol/L
NaCl"*, *"only 12% of patients responded"* — anything where the next
reader will sooner or later ask "says who, under what conditions,
and how was it measured?"

### Before creating, ALWAYS search

```python
search(kind='finding', q='2.4 kV gate dielectric 30 s')
```

Read the `setup` column of every hit. If one matches your setup
(same instrument / electrode / ambient / technique), **reuse its
pub_id** — append your own context as a `kind='memory'` linked
back to the finding rather than spawning a parallel chase.
**Alternate setups need different findings**, even when the bare
number is identical: a 2.4 kV measurement on Cu / N₂ is *not* the
same finding as one on Ag / Ar.

### DO NOT create findings for

- Opinions or qualitative impressions ("the figure is striking",
  "the result is robust").
- Definitions or terminology ("we call this the gate-bias regime").
- Claims without a measurable quantity ("the device worked well").
- Speculation, hypothesis, or proposed future work.
- Claims you are stating *for the first time* — those are
  findings of the document you're writing. Make them citable by
  publishing them, not by recording them here.

## Create a finding

```python
put(kind='finding',
    title='gate-bias 2.4 kV / 30 s on Si/SiO2',
    body=('Device prep: 2.4 kV applied across the 50 nm gate oxide '
          'for 30 s on Si/SiO2 MOSCAPs with a Cu top contact '
          '(sputtered), N2 ambient, room temp.'),
    scope={'electrode': 'Cu', 'ambient': 'N2',
           'technique': 'DC ramp', 'geometry': 'planar',
           'substrate': 'Si/SiO2'},
    cited_in='miller23a~42')
# → created finding id=42 pub_id=ab12c3
#   title: gate-bias 2.4 kV / 30 s on Si/SiO2
#   frontier: paper:miller23a~42
#   status: STATUS:tracing
#   placeholder: [ab12c3] (use in text; precis resolve substitutes
#                          the primary cite_key once STATUS:established)
```

**Required**: `title` (one-line short claim), `body` (claim + setup
as prose), `cited_in` (the starting frontier of the chase).

**Recommended**: `scope` (structured setup as a dict — used for
search filtering and for *two-agents-collapse* dedup; two
`put`s with identical `(body, scope, cited_in)` produce the same
deterministic `pub_id` and the second call returns the first
call's finding rather than creating a duplicate).

**`cited_in` grammar** (block-level optional):

```
cited_in='miller23a'           # ref-level (paper kind implied)
cited_in='miller23a~42'        # chunk ord=42
cited_in='paper:miller23a~42'  # explicit kind
cited_in='doi:10.1234/xyz'     # via DOI
cited_in='ab12c3'              # via pub_id (programmatic callers)
```

## Read a finding

```python
get(kind='finding', id=<pub_id>)
# →  # finding 42
#    _pub_id: ab12c3  (placeholder for precis resolve)_
#
#    title: gate-bias 2.4 kV / 30 s on Si/SiO2
#    claim:
#      Device prep: 2.4 kV applied across the 50 nm gate oxide
#      for 30 s on Si/SiO2 MOSCAPs with Cu top contact, N2 ambient.
#    scope:
#      ambient: N2
#      electrode: Cu
#      ...
#    primary: fischer13
#    begat by:                     (oldest → newest)
#      fischer13
#      miller23a  (primary)
#    status: STATUS:established
```

```python
get(kind='finding', id=<pub_id>, view='log')
# →  log: 3 event(s) for ref_id=42 (source='chase')
#      22:00:01  chase  advanced    miller23a~42 → fischer13 (stub)
#      22:00:02  chase  waiting     fischer13~? (stub has no chunks yet)
#      22:10:15  chase  terminated  fischer13~17 (primary)
```

```python
search(kind='finding', q='2.4 kV gate dielectric 30 s')
# Default filters to STATUS:established. To see in-flight findings:
search(kind='finding', q='...', status='tracing')
search(kind='finding', q='...', status='*')   # all states
```

## Use a finding in your draft

Drop the `pub_id` in square brackets — exactly as `put` returned:

> The gate was held at 2.4 kV for 30 s [ab12c3].

At document-finalisation time, run `precis resolve`:

```bash
precis resolve manuscript.tex --format latex --strict
# →  \cite{fischer13} substituted where established
#     in-flight placeholders kept as \cite{ab12c3}\,\textsuperscript{⏳}
#     --strict exits 3 if anything still in flight (CI-gate friendly)
```

`--keep-id` keeps the placeholder annotated for dead-chain findings
(the chase couldn't reach a primary source); `--ascii` swaps the
unicode ⏳ for `*` if your LaTeX engine isn't xetex/luatex.

## The chase and the fetcher

Findings advance via two worker passes:

- **`precis worker --only chase`** — walks the citation graph,
  creates stub paper refs for cited works that aren't in the
  corpus yet, terminates the chain when it hits a primary
  measurement.
- **`precis worker --only fetch`** — Unpaywall + arXiv + S2
  cascade that downloads OA PDFs for stubs the chase created;
  the watcher's normal ingest path picks them up and the chase
  resumes.

Default `precis worker` runs both passes alongside embed +
summarize + segments. The chase is **deterministic** by default
(regex inline-cite detection, S2 references list lookups); pass
`--with-llm` to enable claude-p-driven verification,
disambiguation, and chunk-localisation hooks (default off — costs
real money per call).

## Common questions

**Q: My finding is `STATUS:multi_candidate`. What now?**
The chase saw inline `[12,13]` style multi-cites in the frontier
chunk and can't pick automatically. Use `edit(kind='finding',
id=<pub_id>, pick_candidate='miller23a')` to resolve. (Or pass
`--with-llm` on the next chase pass — the LLM disambiguator can
collapse the candidates.)

**Q: `STATUS:dead_chain` with `meta.dead_reason='no_resolvable_cite'`.
Why didn't the chase advance?**
The frontier chunk had no inline citation the chase could resolve
(no S2 references for the source paper; no `[N]` brackets in the
chunk text). Either the source genuinely is the primary
measurement (use `edit(... pick_candidate='self')` to mark
terminal) or the inline cite is in an unusual form the regex
missed (file a bug with the chunk text).

**Q: My finding has `meta.caveats` from the LLM verifier — what
do I do with them?**
Caveats accumulate on the chain entry where the verifier hit
them and surface in `get(kind='finding', view='log')` and the
rendered detail. They're notes for human review; render them in
your document if they qualify the claim, or spawn a sibling
finding (`put(kind='finding', ..., cited_in='<caveat-cited-paper>')`)
to chase the qualification's primary source.

**Q: Can I cite a finding externally (`\cite{finding-pub_id}`)?**
No. Findings are internal certainty records. `cite(kind='finding',
...)` raises `Unsupported`. The placeholder → primary substitution
via `precis resolve` is the only way findings reach published text.

## See also

- `precis-citation-help` — the single-hop verified claim → quote
  primitive (citations are user / verifier-subagent records;
  findings are chain heads over chases).
- `precis-search-help` — the search layer, discovery hints.
- `precis-bibliography-help` — `get(kind='paper', view='bibliography')`
  surfaces citations citing a paper; complements the finding
  chain's view from the other direction.
- `precis-overview` — kind-list and address grammar.
