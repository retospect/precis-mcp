---
id: precis-finding-help
title: precis — register a citation target so the worker can chase it
applies-to: put / get / search (kind='finding')
status: active
---

# precis-finding-help — register a citation target and let the chase pull it

A `finding` is a citation target: a claim you want sourced plus a
pointer to where you read it. The worker fetches the cited paper
(via Unpaywall / arXiv / S2 for DOIs, or OPS for patents) and walks
the chain back toward the primary source. You get a numeric `id`
back; drop it in your draft as a placeholder and run
`precis resolve` at finalisation to substitute the primary
`cite_key`.

## Register a finding so the worker can chase its source
## Create a finding to track an empirical claim
## I have a claim and a citation — log it for sourcing

```python
put(kind='finding',
    title='gate-bias 2.4 kV / 30 s on Si/SiO2',
    body=('Device prep: 2.4 kV applied across the 50 nm gate oxide '
          'for 30 s on Si/SiO2 MOSCAPs with a Cu top contact '
          '(sputtered), N2 ambient, room temp.'),
    scope={'electrode': 'Cu', 'ambient': 'N2',
           'technique': 'DC ramp', 'substrate': 'Si/SiO2'},
    cited_in='miller23a~42')
# → created finding id=42
#   placeholder: [42]   (drop in draft; precis resolve substitutes
#                        the primary cite_key once established)
```

Required: `title`, `body`, `cited_in`. Recommended: `scope` (dict —
filters search and dedups identical `(body, scope, cited_in)`
re-submissions so two agents writing the same claim collapse).

## What `cited_in=` accepts
## Pointer formats for the source of the claim
## How do I reference the paper or patent I'm citing?

Four equivalent forms — bare forms assume `paper:` kind:

```python
cited_in='miller23a'                   # bare cite_key (paper)
cited_in='miller23a~42'                # bare cite_key + chunk
cited_in='paper:miller23a~42'          # explicit-kind prefix
cited_in='doi:10.1234/xyz'             # fully-qualified URI
cited_in='patent:ep1234567b1'          # patent target
```

## When to create a finding

Quantitative or empirical claims whose **setup context** matters to
the next reader: *"X = 2.4 kV"*, *"0.1 mol/L NaCl"*, *"12% of
patients responded"*. Skip opinions, definitions, speculation, and
claims you're stating for the first time.

Different setups need different findings even when the bare number
is identical: 2.4 kV on Cu / N₂ is not the same finding as 2.4 kV on
Ag / Ar.

## Find an existing finding before creating one
## Search findings to avoid duplicates
## Has someone already chased this claim?

```python
search(kind='finding', q='2.4 kV gate dielectric 30 s')
```

Read the `setup` column of every hit. If one matches, reuse its
`id` rather than spawning a parallel chase; attach your own context
with `put(kind='memory', link='finding:<id>')`.

## Read a finding
## Look up a finding by id
## What does finding 42 say?

Both forms work — bare `id=42` and `id='finding:42'`:

```python
get(kind='finding', id=42)
get(kind='finding', id='finding:42')
get(kind='finding', id=42, view='log')      # chase event history
```

```text
# finding 42
title: gate-bias 2.4 kV / 30 s on Si/SiO2
claim:
  Device prep: 2.4 kV applied across the 50 nm gate oxide
  for 30 s on Si/SiO2 MOSCAPs with Cu top contact, N2 ambient.
scope:
  ambient: N2
  electrode: Cu
primary: fischer13
begat by:                     (oldest → newest)
  fischer13
  miller23a  (primary)
status: STATUS:established
```

```python
search(kind='finding', q='...')              # default: established only
search(kind='finding', q='...', status='tracing')
search(kind='finding', q='...', status='*')  # all states
```

## Use a finding in your draft

Drop the id in square brackets:

> The gate was held at 2.4 kV for 30 s [42].

At finalisation:

```bash
precis resolve manuscript.tex --format latex --strict
# → \cite{fischer13} substituted where established
#   in-flight placeholders kept as \cite{42}\,\textsuperscript{⏳}
#   --strict exits 3 if anything still in flight (CI gate)
```

`--keep-id` annotates dead-chain findings; `--ascii` swaps the
unicode ⏳ for `*` on non-xetex/luatex engines.

## Resolve a multi-candidate finding

`STATUS:multi_candidate` means the source chunk had `[12,13]`-style
multi-cites the chase can't disambiguate. Pick the right one:

```python
edit(kind='finding', id=42, pick_candidate='miller23a')
edit(kind='finding', id=42, pick_candidate='self')   # mark terminal
```

If the chase stalls with `STATUS:dead_chain`, the frontier chunk had
no resolvable inline citation. Mark it terminal with
`pick_candidate='self'`, or — if a fetch never ran — ask the user to
run `precis worker --only fetch`.

## See also

```python
get(kind='skill', id='precis-citation-help')   # verifier-write side of citations
get(kind='skill', id='precis-paper-help')      # chunk-handle grammar (~N, ~A..B)
get(kind='skill', id='precis-search-help')     # query mechanics
get(kind='skill', id='precis-bibliography-help')  # who cites this paper
get(kind='skill', id='precis-overview')        # verbs and kinds
```
