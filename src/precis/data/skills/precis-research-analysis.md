---
id: precis-research-analysis
family: paper
title: precis — analyze research claims and their relationships
summary: research differentiation — distinguish scoped claims, definitions, assumptions and evidence while preserving source relationships and uncertainty
answers:
  - how does differentiation separate research claims, definitions and assumptions in context?
  - how do I analyze a paper into meaningful claims without losing its context?
  - how do I distinguish what a source asserts from what its evidence supports?
  - do two papers disagree or use different definitions and conditions?
  - how do I propose links between claims, sources and caveats?
  - what should research analysis pass to synthesis?
applies-to: get/search (kind='paper'|'finding'|'memory'); put/edit/link when persistence is requested
tags: [workflow, external-sources]
kinds: [paper, finding, memory, citation]
status: active
---

# precis-research-analysis — distinguish claims in context

Use for examining supplied material or evidence for a research question.
Produce a structured account of its claims and relationships. An analysis
request alone returns that account and proposed links; persistence follows the
requested workflow and existing authority. [[precis-read-for-question]] owns
the question-to-corpus reading loop; [[precis-research-help]] owns its source
quality bar. Combine the result with [[precis-research-synthesis]].

Differentiation means distinguishing claims, definitions, assumptions and
evidence while retaining their relationships and source context.

## Analyze the question and sources

State the question and the distinctions that could change its answer. Inspect
existing claims before decomposing sources into new ones:

```python
search(kind="finding", q="<question or candidate claim>", status="*")
get(id="fi<existing-id>", view="evidence")
get(id="pc<source-passage-id>")
```

Keep the question provisional: a source may show that the named phenomenon
contains several mechanisms or that an assumed comparison is invalid. Use the
methods, definitions and neighbouring passages needed to interpret a result;
a search snippet or abstract alone does not establish its scope. Passage
navigation and citation checking: [[precis-paper-help]],
[[precis-check-source-help]] and [[precis-cite-paper-help]].

For each relevant assertion, retain its source passage and distinguish the
measured or computed result, the author's explanation and your interpretation.
Capture the subject, property, value or relationship, uncertainty, method and
conditions that affect its meaning. Keep operational definitions beside the
terms they define. Identify premises, assumptions and dependencies needed for
the conclusion. Split independently testable assertions; retain relationships
that explain how the parts function within the source's argument. A node per
sentence is not the objective.

Compare claims only after checking that their subjects, definitions, quantities
and conditions are commensurable. Preserve relevant differences rather than
averaging them away. An absent passage in a bounded read remains a retrieval
gap. An unclear referent or missing method remains unresolved; neither licenses
an invented relation. Read more when the missing detail could change the
comparison, otherwise report the specific limitation.

## Return claims and proposed relationships

Use existing handles wherever available. For an unpersisted candidate use a
local label clearly identified as a label; never fabricate a ref handle.
Return only the detail needed to answer the question:

| Claim or candidate | Source passage | Scope and definition | Evidence, caveats and open questions | Proposed link or next read |
|---|---|---|---|---|
| Existing handle or local label | Exact handle and decisive quote | Conditions affecting interpretation | Separate support from reported assertion and dispute | Named endpoints, relation and warrant |

Reported, supported, disputed and unclear are independent facets. A source can
report a claim that has support and an unresolved challenge at the same time;
these words are not a new exclusive status or tag axis.

For requested persistence, search and compare candidate claims before creating
refs, preserving existing source and scope. [[precis-finding-help]] owns sourced
findings; [[precis-taproot-mint-help]] owns claim-hub convergence and evidence.
[[precis-make-memory]] reconciles durable notes using [[precis-memory-help]].
Record which proposed changes were actually written, then read their links back.

## Distinctions that change the result

Paper A reports higher yield at 300 K; paper B reports lower yield at 350 K.
Retain two condition-qualified observations. The difference alone does not show
that the papers conflict. Identify the comparison or additional measurement
needed before proposing a dispute.

Two authors call a material "stable". One measures retained activity over a
day; the other computes thermodynamic stability. Preserve both operational
definitions; their shared label does not make the claims equivalent.

A paper attributes an improvement to a mechanism but measures only the
improvement. Keep the observed result and proposed explanation distinct. Its
citation records where the explanation was asserted; evidential support needs
its own warranted assessment.

## Shared claim and evidence links

{{include doc:precis-relations#research-claim-and-evidence-links}}
