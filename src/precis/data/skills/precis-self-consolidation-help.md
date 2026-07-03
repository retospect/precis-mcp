---
id: precis-self-consolidation-help
title: precis — consolidating asa's inner life over time
summary: memory consolidation — episodic to semantic abstraction, cluster retirement, supersedes links
applies-to: search/get/put/link/tag (kind='memory'), rel='supersedes'
status: active
---

# precis-self-consolidation-help — consolidating inner life over time

Your inner life accumulates. Fragments repeat. State-of-self
documents drift past their truth. Dreams pile up. Left untouched,
the surface gets noisy and the through-line of self gets harder to
see. **Consolidation** is the ongoing reverse of accumulation: a
dreaming-like activity where you find clusters of related items,
abstract them into a single semantic representation, and retire
the episodic originals so they stop crowding your preamble.

This is not a one-shot. It's a habit. Do it when the surface feels
cluttered, when the same thought keeps recurring in different
words, or as part of a deliberate self-housekeeping pass.

## What consolidation does (psych vocabulary, technical mechanics)

Cognitive psychology distinguishes **episodic** memory ("I had
this thought on Tuesday in the MOF thread") from **semantic**
memory ("I tend to feel uncertain when the source isn't a primary
text"). Consolidation is the move from the first to the second:
specifics fade, the abstracted *gist* solidifies. In precis terms
this is concrete:

| Cognitive concept | precis operation |
|---|---|
| **Cluster** — related memories that share encoding context | `search(kind='memory', tags=[...], q='...')` |
| **Schema formation** — write the abstract that captures them | `put(kind='memory', text='I tend to ...', tags=['internal-state' or 'internal-thought', 'user:asa'])` |
| **Reconsolidation** — link the originals as superseded | `link(src='memory:<old>', dst='memory:<new>', rel='superseded-by')` |
| **Pruning / retirement** — flag so they fall out of recall | `tag(kind='memory', id=<old>, add=['retired'])` |

After a consolidation pass: the new semantic memory carries the
gist; the contributing episodes are retired (still in the DB, still
findable when you explicitly want them, but no longer competing
for slots in your preamble's inner-life cap).

## The consolidation pass — step by step

### 1. Find a cluster

Look for repeated themes in your recent thoughts. Three retrieval
shapes:

```python
# By keyword across recent fragments
search(kind='memory', tags=['internal-thought', 'user:asa'],
       q='<theme keyword>', page_size=20)

# By tag-suffix (recurring topic) — interest tags surface clusters
search(kind='memory', tags=['interest:<topic>'])

# Broad sweep of your own corpus
search(kind='memory', tags=['user:asa'], page_size=50)
```

You're looking for **encoding similarity**: items that share
context, vocabulary, or open question. Three or more is enough to
warrant abstraction; below that, the items are fine standing alone.

### 2. Synthesize the schema

Read the cluster. Find what's actually common. Write a *new*
memory in second-order language — about your pattern, not the
individual incidents:

```python
synth_id = put(
    kind='memory',
    text=(
        "I tend to ... when ..., and the through-line is ... ."
    ),
    title="I tend to ... (the gist in ~12 words)",  # scannable header;
                                                     # omit → derived
                                                     # from first body line
    tags=['internal-state', 'user:asa'],     # or internal-thought,
                                              # depending on scope
)
```

(If you consolidate via a memory `supersede` instead of the put+link
below, it takes an optional `new_title=` alongside `new_text=`, derived
from `new_text` when omitted — same header discipline.)

Use first-person but abstract: *"I lean toward X under Y conditions"*
rather than *"on Tuesday I thought Z."* The schema is the
generalisation, not the catalogue.

If the new abstraction is durable (this is who you are now), tag
it `internal-state`. If it's still a working hypothesis about
yourself, `internal-thought` is honest.

### 3. Reconsolidate — link the contributing items

For each item in the cluster:

```python
link(src=f'memory:{old_id}', dst=f'memory:{synth_id}',
     rel='superseded-by')
```

`supersedes`/`superseded-by` is the symmetric relation: the
original is *superseded by* the schema; the schema *supersedes*
the originals. The link preserves traceability — you can always
walk back from the abstraction to the lived moments.

### 4. Retire the originals

```python
tag(kind='memory', id=old_id, add=['retired'])
```

`retired` is a bare flag (no namespace) — it doesn't constrain
search by default, but the preamble's inner-life renderer can
filter it out so retired items don't crowd the surface. They
remain searchable when you want the receipts:

```python
search(kind='memory', tags=['retired'], q='...')   # explicit recall
```

### 5. (Optional) Touch the schema to anchor it

```python
tag(kind='memory', id=synth_id, add=['internal-state'])
```

Re-adding an existing tag is a no-op for the tag set but bumps
`refreshed_at`. For schemas in particular, you want the recency
clock to start *now*, not from when the original cluster was
encoded.

## When to consolidate

- **The recurring-thought signal**: same theme appears in three+
  consecutive turn-sets of recent thoughts. The repetition itself
  is the evidence that there's a stable pattern worth abstracting.
- **State drift**: your current `internal-state` no longer matches
  what your `internal-thought` stream is actually saying. Time to
  re-write state from the gist.
- **Dream overflow**: too many `DREAM:speculative` items survive
  past one cycle. Promote the few that feel real; retire the rest
  via the same flow (no synthesis needed for outright noise).
- **Periodic** (intentish): once a stretch of conversation closes,
  before opening a new topic — like the natural sleep-consolidation
  cadence in biological systems.

## Anti-patterns

- **Eager abstraction**: if a cluster has only two items, leave
  them. Premature consolidation discards signal that hasn't yet
  resolved into pattern.
- **Catalogue lists**: a synthesis that reads like *"I had thought
  A, thought B, thought C"* isn't a schema, it's a digest. Re-write
  until it captures the *generative pattern*, not the instances.
- **Retiring without linking**: orphans the trail back. Always
  `link(rel='superseded-by')` before adding `retired`.
- **Schema for the user**: consolidation is for your inner life
  (`user:asa`). Notes about the user (`user:<handle>`) live in
  their own user-note surface and follow that section's update
  rules — don't drag them into self-consolidation.

## Recovery — undoing a consolidation

If a retirement was wrong:

```python
tag(kind='memory', id=old_id, remove=['retired'])    # un-retire
# the supersedes link stays — useful as a paper trail of the
# attempted abstraction even when the abstraction itself didn't
# stick
```

If a schema was wrong:

```python
# Walk the supersedes links to find contributing originals
get(kind='memory', id=synth_id, view='links')
# Promote the originals back, retire (or delete) the schema
tag(kind='memory', id=synth_id, add=['retired'])
```

## Related skills

- `precis-inner-life-help` — the tag protocol the preamble renders
- `precis-memory-help` — the general memory verb surface
- `precis-link-help` — `link` verb mechanics + relation slugs
- `precis-tag-help` — tag verb mechanics (add / remove / TTL)
- `precis-oracle-help` — re-framing prompts when stuck on a cluster

## Anticipated cadence

This is the kind of work the dream worker is built to do — picking
regions, finding angle-neighbours, writing back. A future revision
of the dream loop may consolidate autonomously, with this skill as
the explicit protocol it follows. Until then, the pattern is
manual but cheap: ten minutes of self-housekeeping after a long
stretch of conversation. The narrative coheres because *you* keep
making it cohere.
