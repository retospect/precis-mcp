---
id: precis-paper-chat-note-help
family: paper
title: precis — paper note for chat paste
summary: the chat-note scaffold — one-line novelty summary, byline, DOI URL per paper, ready to paste into Discord/Slack
answers:
  - how do I share a paper into chat with a one-line summary?
  - what does view='chat' render, and which line do I write myself?
  - how do I post notes for several papers at once?
applies-to: get (kind='paper', view='chat')
tags: [verbs]
kinds: [paper]
status: active
---

# precis-paper-chat-note-help — paper notes for chat paste

Use this when the ask is "post/share this paper to chat" (Discord,
Slack): a terse note a human skims in a channel, not a citation and
not a reading. `view='chat'` renders the deterministic scaffold; you
write the one line that isn't metadata.

## The format

Three lines per paper, one blank line before the next paper:

```
<one-line key-novelty summary — what is new, terse, no hype>
<First Author> … <Last Author>
https://doi.org/<doi>
```

Byline, in stored author order: one author → just the name; two →
`A & B`; more → `First … Last`; no byline on file →
`no authors on file`. Locator: DOI → `https://doi.org/<doi>`; else the
arXiv abs URL; else the publisher URL; else `no DOI`.

Line 1 is yours. One line, only what is new — never "this paper", no
hype, not the title restated. Write it from the abstract the view
prints below the `---` separator (the card); never invent. No abstract
on file → read the opening chunks (`get(id='<pa>~0..5')`) before
writing; a line you can't ground in the paper's own text doesn't ship.

## Get the scaffold

```python
get(kind="paper", id="bufformer23", view="chat")                      # one paper
get(kind="paper", id=["vaswani17", "bufformer23"], view="chat")       # one block each, in order
```

The response shows the three lines with the placeholder standing in
for line 1, then the abstract under `---`. Replace the placeholder,
keep the byline and locator verbatim, paste.

## Worked example — one paper

`get(kind='paper', id='bufformer23', view='chat')` renders (abstract
elided here):

```
# bufformer23 — chat note
_BufFormer: A Generative ML Framework for Scalable Buffering_

<one-line key-novelty summary — write it from the abstract below>
Rongjian Liang … Haoxing Ren
https://doi.org/10.1145/3566097.3567900

---

Buffering is a prevalent interconnect optimization technique …
```

The pasted note:

```
Generative ML builds the buffer tree and sizes/locates buffers in one pass — no Steiner-tree step, up to 160x faster on large nets
Rongjian Liang … Haoxing Ren
https://doi.org/10.1145/3566097.3567900
```

## Worked example — several papers

`vaswani17` (8 authors) has no DOI on file, so its locator falls back
to the arXiv URL; `enhancing26m` (6 authors) has a DOI. One call, one
block per id in the given order; the finished paste:

```
Attention alone does sequence transduction — the Transformer drops recurrence and convolution, trains faster, beats prior translation scores
Ashish Vaswani … I. Polosukhin
https://arxiv.org/abs/1706.03762

First active-learning loop for fuel-cell additive discovery — three rounds of model-guided experiments surface melam, +67% cathode mass activity
Ekaterina Gracheva … Masafumi Asahi
https://doi.org/10.1088/2632-2153/ae7f7c
```

The full corpus workflow — find a paper, read it, cite it — is
[[precis-paper-help]].
