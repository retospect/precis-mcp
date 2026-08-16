---
id: precis-paper-code-audit
title: precis — audit a paper's claims against its public code
summary: claims-vs-code audit runbook — compare methods/defaults/metrics against the fetched repo, file a finding per mismatch, one consolidated draft
flavor: runbook
status: active
applies-to: search/get (kind='paper','web'); put (kind='finding','draft'); link (rel='contradicts')
---

# precis-paper-code-audit — compare a paper's claims against its codebase

Adapted from companion-inc/feynman (MIT). Checks whether a paper's
stated methods, defaults, and metrics match what its public
repository actually does.

## Read the paper's claims first

```python
get(kind="paper", id="<slug>", view="toc")
get(kind="paper", id="<slug>~lo..hi")  # methods, defaults, reported metrics
```

## Read the code the paper points at

```python
get(kind="web", id="<repo-file-url>")
search(kind="web", q="<repo> config default")
```

A repo that needs cloning to inspect is outside the seven verbs'
reach — mark the check `blocked` rather than guess what the code
does.

## Compare, claim by claim

Walk each claimed method, default, and metric against the fetched
code. Three outcomes: matches, mismatches, or ambiguous (the paper
doesn't say enough to tell).

## File a finding for every mismatch or omission

```python
put(
    kind="finding",
    title="<claim> vs code default mismatch",
    body="paper states <X>; repo default is <Y>",
    cited_in="pc<id>",
)
link(kind="finding", id=<finding-id>, target="pa<id>", rel="contradicts")
```

Every finding needs a real source chunk (`cited_in=`) — the paper
passage the claim came from.

## Write one consolidated audit

```python
put(kind="draft", id="<slug>-audit", project=<todo-id>, title="Code audit — <slug>")
```

One artifact per audit: the paper, the repo, and every finding above,
grouped mismatches / omissions / ambiguous.

## See also

```python
get(kind="skill", id="precis-finding-help")  # register a mismatch
get(
    kind="skill", id="precis-relations"
)  # rel='contradicts' and the rest of the vocabulary
get(
    kind="skill", id="precis-replication"
)  # check whether a result reproduces, not just matches code
get(kind="skill", id="precis-web-help")  # fetch repo files
get(kind="skill", id="precis-draft-help")  # the document kind for the audit artifact
```
