---
id: precis-gap-analysis
family: drafting
title: precis — survey a project against precis, mesh first and prose last
summary: gap-analysis / prior-art survey runbook — hold the sources, mint a finding hub per claim, one memory node per surveyed project, inspired-by links on what the survey spawns, then a draft citing [fi] and [me] only
answers:
  - how do I compare an external project or paper against precis?
  - what do I file before I write a gap analysis or a prior-art survey?
  - what should a survey sub-agent hand back?
  - which glossary terms from a survey become anki cards?
flavor: runbook
status: active
applies-to: get (kind='paper','web'); put (kind='finding','memory','draft','anki'); link (rel='inspired-by','rejected-after')
tags: workflow, external-sources
kinds: finding, memory, draft, anki
---

# precis-gap-analysis — survey a project against precis, mesh first

A survey, gap analysis or prior-art comparison is a graph job before it
is a document. Prose written first freezes on one passage, hides
disagreement and leaves every code-level fact with no held source.

## Build the mesh first

Six steps, in order. Each of the first five leaves a handle; the draft
is step 6 and cites those handles.

```python
# 1. Surveyed before? A hit: extend that node, do not survey twice.
search(kind="memory", tags=["SPACE:repo-dev"], args={"under": "me608724"}, q="<project or group>")

# 2. Hold the sources: the paper, and each repo file a code fact rests on.
get(kind="paper", id="<slug or DOI>", view="toc")
get(kind="web", id="<repo-url>")  # LICENSE, README, manifests, file listing
get(kind="web", id="web:<id>~<path>")  # one held file

# 3. One finding hub per claim the write-up will state. Search, then mint.
search(kind="finding", q="<the claim sentence>", status="*", mode="semantic")
put(
    kind="finding",
    title="<one self-contained claim sentence>",
    args={"supporters": [{"paper": "pa<id>", "source_handle": "pc<id>"}]},
)  # → claim hub fi<id>

# 4. One memory node per surveyed project, under the prior-art hub.
put(
    kind="memory",
    title="<Project>: <what it is, as one claim>",
    text=(
        "What: <one line>. Licence: <SPDX id>. Handles: pa<id>, web:<id>, "
        "fi<id>, fi<id>. Surveyed: <UTC date>. Take: <what, gr<id>>. "
        "Reject: <what, and why>."
    ),
    tags=["SPACE:repo-dev", "section:reference"],
    link="me608724",
    rel="part-of",
)  # → me<id>
link(kind="memory", id="me<id>", target="me<topical hub>", rel="part-of")  # and its topical hub

# 5. Each gripe or backlog item the survey spawns names its source.
link(kind="gripe", id="<gripe id>", target="pa<id>", rel="inspired-by")
link(kind="gripe", id="<gripe id>", target="web:<id>", rel="rejected-after")

# 6. Only now the draft.
put(kind="draft", id="<project>-gap-analysis", project="<todo id>", title="<Project> — gap analysis")
put(
    kind="draft",
    id="<project>-gap-analysis",
    chunk_kind="paragraph",
    text="<Project> reports <claim> [fi<id>]; precis has no counterpart [me<id>].",
    at={"last": True},
)
```

Draft prose cites `[fi<id>]` hubs and `[me<id>]` nodes only, never bare
`[pc<id>]` or `[pa<id>]`: a chunk grounds a hub, it is not the cite
([[precis-citation-help]]).

- **Step 3.** Write the sentence to the admissibility test in
  [[precis-taproot-mint-help]]. A hub that already states the claim at
  the same scope takes your passage as one more supporter; do not mint
  a second one. Where the project's paper and another source disagree,
  mint both and link them `disputes`.
- **Step 4.** A hub takes paper supporters only. A fact read in the
  repo (a default, a licence, what a module does) goes in the project's
  memory node next to the held file's `web:<id>~<path>` handle, and the
  prose cites that node. The node lists every `fi<id>` the write-up
  rests on. The second call hangs it off its topical hub as well as
  `me608724`.
- **Step 5.** `inspired-by` records what was taken, `rejected-after`
  what was read and turned down; the reason goes in the gripe or the
  memory node ([[precis-relations]]).

## Turn the glossary terms into anki cards

Terms the draft defines (`chunk_kind="term"`) that a learner wants cold
become cloze cards in the same session.

```python
search(kind="anki", q="<term>")  # once per cluster: skip what is already a card
put(
    kind="anki",
    text="""\
<Sentence with {{c1::<term A>::<hint>}} and {{c2::<term B>::<hint>}}.>
---
<ul><li><term A> = <meaning></li><li><term B> = <meaning></li></ul><hr><paper handle, or the precis feature the term touches>""",
    tags=["deck-<topic>"],
    link="pc<id>",
    rel="derived-from",
)
```

- Gate each term with rule 0 of [[precis-cloze]]: no card for a file
  name, a licence, an institution acronym or a library name, unless the
  mechanism behind it matters.
- Two or three related terms per card, each with a hint.
- Back Extra lists each symbol, variable and acronym on the card as an
  HTML list, then `<hr>`, then the paper handle or the precis feature
  the term touches ([[precis-cloze]], rule 7).
- `link=` with `rel="derived-from"` only when the card states a result
  from that chunk.
- Report the terms you skipped; the user picks from that list.

## Brief a survey sub-agent to return handles, not prose

A prose report holds nothing: each fact in it has to be read and sourced
again before it can be cited. Put this in the brief:

```text
Survey <project> for <question>. Hold each source you read: the paper
and each repo file. For every claim worth stating, search the finding
hubs, then mint a hub on the passage you read, or attach the passage to
the hub that already states the claim. Return one line per claim:
  fi<id> | <claim sentence> | pc<id>
then the web handle of each file you held, then what you could not
source. No prose report.
```

A sub-agent that cannot write returns the same lines with the `fi<id>`
column empty, and the root mints from that list
([[precis-taproot-mint-help]], mint waves). A returned fact with no
handle is not usable: send it back.

## See also

- [[precis-taproot-mint-help]] — admissibility test, dedup, the mint call
- [[precis-citation-help]] — why prose cites hubs, and the adversarial search
- [[precis-research-help]] — the quality bar for a findings list
- [[precis-memory-help]] — the prior-art hub, `part-of`, the shape of a node
- [[precis-relations]] — `inspired-by`, `rejected-after`, `disputes`
- [[precis-cloze]] — the gate for a card worth reviewing
- [[precis-source-comparison]] — an agreement matrix across sources
- [[precis-paper-code-audit]] — a paper's claims against its code
