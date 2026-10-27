---
id: precis-datasheet-help
family: pcb
title: precis — reading datasheets for PCB design
summary: read a component datasheet as searchable chunks to pull pinouts, supply ranges, decoupling guidance and application circuits, then turn that into net classes and measures. The electronics sibling of the paper kind; ingested by the same Marker→chunks pipeline.
answers:
  - how do I read a component datasheet as searchable chunks?
  - where does a datasheet ref come from — do I have to upload it myself?
  - what should I pull out of a datasheet and where does it go?
  - how do I attach a datasheet PDF I found to a part?
applies-to: get/search/put (kind='datasheet'); feeds pcb net-class + measures
status: active
tags: [design]
kinds: [datasheet, pcb, part]
---

# precis-datasheet-help — turn a datasheet into design decisions

A `datasheet` is a component document (electronics sibling of `paper`),
ingested the same way — embed / keywords / TOC / semantic search for free.
It's **evidence** (you attach a PDF by URL, below; you don't author it) — you mine it for the
facts a `pcb` design needs, you don't author it.

## Read one — `get` / `search`

```python
get(kind='datasheet')                            # list ingested
get(kind='datasheet', id='<slug>')               # one document
get(kind='datasheet', id='<slug>', view='toc')   # section/keyword TOC
search(kind='datasheet', q='I2C address timing', ...)
```

Paper addressing applies (`<slug>~N`, `/toc`, per-chunk search). Datasheets
are scoped out of `search(kind='paper')` and vice-versa. A figure or
caption-only chunk (a `*Figure 211: …*` legend whose pixels never became
text) renders a pointer line after its body — `figure image: <dk…>` when
the crop was stored, else `page N of <da>` — naming the
`get(view='page_image', args={'ord': N})` / `args={'page': N}` call that
mints a signed URL to the image, which is how a schematic or land pattern
that ingested caption-only stays retrievable.

Sandbox scripts with `precis_access='read'` can read stored datasheets via
`precis_client.call('get', kind='datasheet', id='da123~7')` and inspect
`search(kind='datasheet', view='status', ids=['da123'])`. PCB `source=` takes
`ds:da123~7` (the suffix is a chunk ordinal). `Board.check()` reports missing
chunks as `source.unresolvable` and another catalog part's sheet as
`source.wrong_part`; transport failures remain `source.unchecked`.
Reads do not pull a missing PDF. Writes and the live DOI `health` check
must run outside the sandbox.

## What to pull (and where it goes)

| From the datasheet | Into the design |
|--------------------|-----------------|
| **Pinout / pin functions** | `pins:[{name,tags}]` + net/class ([[precis-net-class-help]]) |
| **Absolute-max / supply range** | supply net `class:'power'` + `current` estimate |
| **Recommended decoupling** ("100 nF per VDD + 10 µF bulk") | bypass caps + `proximity` measure ([[precis-decoupling-help]]) |
| **Bus details** (I²C address, max SCK) | bus net class + pull-ups ([[precis-i2c-help]], [[precis-spi-help]]) |
| **Typical application circuit** | reference topology to capture |
| **Thermal / current** | trace `current`, a `thermal` measure (phase 2) |

## Where a datasheet comes from

A datasheet is pulled and linked automatically (a `datasheet_pull` job) when
a part is looked up (`get(kind='part', id='C…')`) or a `pcb` design uses its
C-number, so it exists before the part is chosen. When the pull fails,
`get(kind='part', id='C…')` gives the reason.

**You found the PDF yourself** (the catalog has no URL, or its URL is
blocked): hand it over by URL.

```python
put(kind='datasheet', args={'url': 'https://…/x.pdf', 'part': 'C19191684'})
put(kind='datasheet', args={'part': 'C19191684'})   # re-pull the catalog's URL
```

`part` is required: a C-number, or an mpn that matches one catalog row. The
put checks the URL against the SSRF guard (private/loopback/non-http is
refused immediately) and queues a pull; the job downloads through
`safe_stream`, requires `%PDF` and at most 50 MB, ingests, and links
`datasheet-of` the part. Without `url` the catalog's URL is pulled again,
even after a failed attempt (a part that already has a datasheet is left
alone). Either form also queues the part's `datasheet_facts` read (below)
unless its facts are already extracted or queued. The outcome shows on the
`datasheet:` line of `get(kind='part')`. A
local sandbox path is not accepted (the worker cannot read it); host the
file or ask an operator to drop it (`docs/runbooks/datasheet-ops.md`). Set
vendor/subtype/part; they flow into the exported citation, and `part_lcsc`
also links the datasheet `datasheet-of` the part (its ref is minted on first
use, [[precis-part-select-help]]):

```python
edit(kind='datasheet', id='<slug>', vendor='Espressif Systems',
     subtype='app-note', part_lcsc='C2934569')
```

A family datasheet that covers several parts takes one more link per extra
part: `link(kind='datasheet', id='<slug>', rel='datasheet-of',
target='part:<C-number>')`.

**A datasheet got ingested as a `paper`**: convert it in place —
`edit(kind='paper', id='<slug|paN>', args={'convert_to': 'datasheet',
'subtype': 'app-note', 'reason': …})` flips the kind keeping body,
links and identifiers (`dry_run` previews); `pa<id>`/`pc<chunk>` keep
resolving via a redirect, `da<id>` mints. The cite_key slug is kept
as-is — a convert never renames it, so old links and bookmarks keep
working; the readable `<vendor>-<part>` scheme applies only at mint
time (pull, inbox drop, add). Details: [[precis-paper-ref-help]] "A
datasheet/manual was ingested as a paper".

**Every URL bot-walls** (`not_pdf` / `http_403` on the part's `datasheet:`
line — common for LCSC-hosted sheets): find the same PDF on a fetchable
host and hand that URL over. A paid search turns one up for ~$0.005;
another vendor's sheet for the same MPN counts when the exact one is
unreachable.

```python
get(kind='perplexity-reasoning', q='<mfr> <MPN> datasheet pdf')
put(kind='datasheet', args={'url': '<found PDF URL>', 'part': 'C883122'})
```

Slug and title come from the PDF's own metadata and can name an unrelated
document — check the stored ref's title against the part and fix it with
`edit(kind='datasheet', id='<slug>', title='<correct name>')`; don't
re-pull. A put on a part that already has a datasheet is a no-op.

## Stubs and pull status

**A human must supply the file** (no fetchable URL anywhere): mint a
stub — a bodiless datasheet already linked `datasheet-of` the part,
listed by `search(kind='datasheet', view='stubs')`. Fill it the paper
way: drop the PDF as `<inbox>/datasheets/da<N>.pdf` and the ingest folds
it into the stub in place.

```python
put(kind='datasheet', args={'part': 'C5358484', 'stub': True})
search(kind='datasheet', view='stubs')   # da<N> + part + fill instruction
```

**Did a pull work?** `search(kind='datasheet', view='status', ids=[…])`
mixes `da<N>` handles, cite_key slugs and C-numbers: a ref row shows its
body state (full-text / card-only / stub) plus its part's recorded pull
outcome; a C-number row shows the pull (`status`, `reason`, `url`,
`at` — where a `not_pdf` or dead-host failure is readable without SQL)
and the datasheet it links, if any. `search(kind='datasheet',
view='ingest', days=14)` lists what landed recently — handle, title,
part, live chunk count, source URL (also the `source:` line on `get`'s
overview), newest first. `view='chase-queue'` is paper-only.

```python
search(kind='datasheet', view='status', ids=['da519921', 'C2895249'])
search(kind='datasheet', view='ingest', args={'days': 7})
```

## The fact layer — `view='facts'`

After a pull lands (queued by a `pcb` put or a `datasheet` put), a
`datasheet_facts` job (one BIG-tier read) extracts
the part's facts onto the part ref: the pin table per package (number,
name, function, `etype`, `rail`, `max_voltage_v`, `vih`/`vil`,
`max_current_a`, `nc`), required externals ("1 µF X5R on IN within 5 mm")
and conditional tables (strap/address), each row citing its datasheet
chunk + page with a confidence. Read them, don't re-derive:

```python
get(kind='part', id='C25804', view='facts')   # the extracted block
put(kind='job', job_type='datasheet_facts',   # extract now (once per sheet)
    parent_id=<pcb ref>, params={'lcsc': 'C25804'})
```

A field the sheet doesn't print stays absent — the review asks the user
rather than guessing. Cite one pin's row with `link(target='part:C…~3')`:
the selector resolves through the pin table to the datasheet page chunk.
An unclear/contradictory read is a low `confidence`, not a refusal — quote
the cited page and correct it if the sheet says otherwise.

## The move

Encode what you read as net **classes** ([[precis-net-class-help]]) and
**measures** ([[precis-measures-help]]) on the `pcb` design
([[precis-pcb-help]]) — the datasheet is the *why*, the netlist is the
*what*.
