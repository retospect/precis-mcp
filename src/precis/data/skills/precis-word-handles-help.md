---
id: precis-word-handles-help
family: core
title: precis — spoken descriptions and word handles
summary: name references in voice chat and podcasts, retain exact handles in transcripts, and resolve spoken descriptions without stored aliases
answers:
  - how do I name references when talking by voice?
  - how do I recover a handle from a spoken name?
applies-to: voice chat, general agent chat, and draft or podcast narration
tags: [addressing]
kinds: [handle]
status: active
---

# Spoken descriptions and word handles

## Talking by voice: name things, keep handles recoverable

When talking to Reto by voice, say the description first and never read the
exact handle aloud. Use the same rules in general agent chat and podcast drafts.
For a paper, use first-author surname, year and a topic word: “Miller 2023,
the ammonia one”. For a gripe, todo or memory, use a two-to-four-word
description such as “the endpoint split gripe”. These descriptions are derived
from the current summary and metadata; they are not stored aliases or unique IDs.

On first mention, retain the exact handle once after the description in the
transcript: `the endpoint split gripe [gr593139]`. Brackets do not guarantee
silence in every voice client. Verify once in the client; if it reads brackets,
use a Markdown link with the handle as target: `[the endpoint split gripe](gr593139)`.
Draft narration already strips inline handles; keep the description in prose.
Use a word handle only as a fallback when there is nothing useful to describe:
`get(kind='handle', id=648264)` converts a number to words and
`get(kind='handle', q='amber brave dog')` converts words back to a number.

To recover a reference from a spoken phrase or transcript snippet, start with
`search(kind='paper', q='Miller ammonia')` (substitute the reference kind).
For a compact candidate list, use
`get(kind='handle', q='the Miller paper', args={'kind':'paper'})`.
Supported scopes are paper, gripe, todo and memory. The resolver returns up to
five `handle | spoken name | summary` lines. Compare candidates using the
conversation's topic, year and author; when ambiguous, ask which one rather than
guessing. No hits means retry search with more context. Descriptions are search
cues, so use the returned exact handle for subsequent reads and writes.
