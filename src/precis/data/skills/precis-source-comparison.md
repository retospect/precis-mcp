---
id: precis-source-comparison
title: precis — compare corpus sources into an agreement matrix
summary: source-comparison runbook — cross-kind gather, agreement/disagreement/confidence matrix, contradicts links, one draft
flavor: runbook
status: active
applies-to: search (cross-kind); put (kind='draft'); link (rel='contradicts')
---

# precis-source-comparison — compare multiple sources on a topic

Adapted from companion-inc/feynman (MIT). Builds a source-grounded
matrix of what different corpus sources say about one topic, and
where they disagree.

## Gather across kinds

```python
search(q="<topic>")  # cross-kind fan-out: paper, web, wikipedia, news, ...
search(kind="paper,web,wikipedia,news", q="<topic>")
```

Each hit is tagged with its source kind — read the chunk before
summarising its position.

## Build the matrix

Columns: source, key claim, evidence type, caveats, confidence. One
row per source; quote the claim, don't paraphrase past what it says.

```python
put(kind="draft", id="<topic>-comparison", project=<todo-id>, title="<Topic> — source comparison")
put(
    kind="draft",
    id="<topic>-comparison",
    chunk_kind="table",
    table={
        "header": ["source", "claim", "evidence", "caveats", "confidence"],
        "rows": [["<...>"] * 5],
    },
    at={"last": True},
)
```

## Record disagreements on the graph, not just in prose

```python
link(kind="paper", id="<slug-a>", target="pa<id-b>", rel="contradicts")
```

Two sources disagreeing is itself worth carrying past this document —
the link survives independent of the matrix.

## Diagram only when the structure earns it

```python
put(kind="mermaid", id="<topic>-comparison-map", title="<Topic> comparison", project=<todo-id>)
```

Reach for `kind='mermaid'` only when the comparison is genuinely
structural (a decision tree, a method lineage) — a table covers most
agreement/disagreement comparisons on its own.

## See also

```python
get(kind="skill", id="precis-search-help")  # cross-kind fan-out mechanics
get(
    kind="skill", id="precis-relations"
)  # rel='contradicts' and the rest of the vocabulary
get(kind="skill", id="precis-draft-help")  # the document kind for the matrix
get(kind="skill", id="precis-mermaid-help")  # when a diagram is warranted
get(kind="skill", id="precis-paper-code-audit")  # claim-vs-code, a narrower comparison
```
