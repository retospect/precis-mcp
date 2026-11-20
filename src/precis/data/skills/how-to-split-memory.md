---
id: how-to-split-memory
family: memory
title: Split a large memory without losing claims or links
summary: Make a memory terser or split its claims into linked detail nodes before trimming the original.
answers:
  - how do I split a memory that exceeds the 4000 character goal?
  - should I shorten a memory or make summary and detail nodes?
  - how do I preserve evidence and links when splitting notes?
applies-to: get/put/edit/link (kind='memory')
tags: [workflow, drafting]
kinds: [memory]
status: active
---

# Split a large memory without losing claims or links

The fisheye body goal is 4000 characters. An oversize hint is advice after
a successful write, not a refusal. Preserve every still-true claim,
qualification and source while making each node readable on its own.
Follow [[precis-memory-help]] for reconciliation and authority; splitting
does not grant permission to change a human-origin memory.

## Read the full memory before choosing a split

Read the body and metadata, then inspect all links. Replace the example
handle with the memory being split. Follow any continuation until complete;
a clipped body or bounded eye is insufficient evidence for a rewrite.

```python
get(kind='memory', id='me12', view='body')
get(kind='memory', id='me12', view='meta')
get(kind='memory', id='me12', view='links')
```

List its claims, their evidence, scope and dependencies before editing.
Search for each proposed claim first: reuse an existing equivalent node
rather than minting a duplicate.

## Choose terseness or a logical split

- **Make it terser** when it expresses one claim. Cut repetition and
  transcript scaffolding; retain conditions, uncertainty, provenance and
  live anchors. Do not shorten by silently dropping qualifications.
- **Extract one logical detail** when the original still has a useful
  central claim. Move a separable explanation, example or result into a
  child, leaving the original's claim and necessary context intact.
- **Keep a summary and detail nodes** when the topic is coherent but the
  explanation is long. Keep the original handle as a short overview;
  give each child a unique atomic claim title and enough context to stand
  alone. Use graph links rather than a hand-maintained children list.
- **Split per claim** when independent subjects have accumulated. Give
  each claim its own node and appropriate subject hub. Keep the original
  as an overview only if it still serves a coherent purpose; do not force
  unrelated claims under it merely because they shared a note.

Prefer semantic boundaries over equal-sized character slices. Keep a claim
with its supporting evidence and exceptions. Aim below 4000 characters per
body, with room for future corrections.

## Create and verify children before trimming the original

For a summary/detail split, create each child with its own claim, context,
source anchor and UTC date. Use the original's space and relevant topic
tags. In a hub-shaped space, inherit the parent's section type (use
`section:reference` for results) and give each detail exactly one
`part-of` edge to the summary. It inherits the summary's scope; do not
copy its `qualifies` edges onto children.

```python
put(kind='memory', title='<atomic claim, at most 12 words>',
    text='<claim, context, evidence anchor, UTC date>',
    tags=['SPACE:repo-dev', 'section:reference'],
    link='me12', rel='part-of')
```

Use the returned handle, not an invented one, to read each new child in
full and check its links. If interrupted, resume from those handles rather
than creating them again. Only after every moved passage is preserved,
trim the original with a unique anchored edit; keep unrelated content.

```python
edit(kind='memory', id='me12', mode='find-replace',
     find='<exact passage now preserved in a child>',
     text='<remaining claim or concise overview>')
get(kind='memory', id='me12', view='body')
```

For terseness alone, use the same anchored edit and full readback without
creating a child. Keep the title and hook accurate after either change.

## Repair links and verify the resulting graph

Check both incoming and outgoing links against the claims they describe.
Keep overview-wide links on the summary. Add evidence links to the child
that now holds the supported claim; redirect claim-specific links only
after verifying the replacement. Preserve relation direction and metadata.
Do not copy every edge indiscriminately or remove a link still needed by
the parent. Inspect dependants before changing references others rely on.

```python
get(kind='memory', id='me12', view='dependants', args={'depth': 2})
link(kind='memory', id='<returned child handle>', target='<source handle>',
     rel='cites')
get(kind='memory', id='me12', extent='fisheye+1hop')
```

Check every child's body and links too. Confirm one parent per detail,
the parent chain reaches its hub, and no claims or evidence were lost.
Follow the write acknowledgements' remaining placement/type hints. Check
processing readiness before testing recall as [[precis-memory-help]]
describes. Retirement of an unneeded original requires preservation and
authority review; it is not a routine step of splitting.
