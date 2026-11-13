---
id: precis-make-memory
family: memory
title: precis — reconcile free text before remembering it
summary: extract atomic claims, resolve subjects, batch-recall existing knowledge, then write only supported changes
answers:
  - how do I remember several facts without duplicating memories?
  - how do I resolve people and pronouns before saving a memory?
  - can MakeMemory suggest edits to existing memories?
  - how do I decide whether a pet needs its own record?
applies-to: get/search/put/edit/link (kind='memory')
tags: [drafting, workflow]
kinds: [memory]
status: active
---

# precis-make-memory — reconcile before writing

Use when asked to remember free text, especially several subjects or claims.
Keep incoming claims transient while comparing them with the graph. A request
for a critique or preview returns the proposed changes without writing.
Runtime mechanics and repo-dev filing: [[precis-memory-help]]. Background
maintenance: [[precis-gardener-help]].

## Read, decide, write

1. Extract independently maintainable claims from the supplied text and keep
   their source sentence, time/scope and pronoun interpretation. Infer no extra
   facts. Choose the caller's memory space and preserve that scope in retrieval.
2. Batch independent identity searches, then read the plausible candidates.
   Retrieve several bodies with `get(kind='memory', id=['me12', 'me34'])`;
   use a single-node fisheye when links or neighbouring context are needed.
3. Follow resolved subjects' links and batch remaining claim searches by
   subject/property. Search results propose candidates; read the evidence.
   An empty or weak result needs a more distinctive query or lexical check
   before concluding absence. Batch errors mean unknown, not absent.
4. Apply the shared policy below. For each claim keep a compact decision:
   source and interpretation → matched handles and excerpt → action and why.
   Keep unresolved identity or conflict local; continue independent resolved
   work. If missing information changes a write, ask that specific question.
5. In normal remember mode, execute supported edits/puts, then links, following
   existing authority and the acknowledgments' `Next:` calls. Read back changed
   records and verify retrieval. In preview mode show the exact commands and
   before/after text instead. Report changes, existing matches and unresolved
   items concisely; retrying the same input should add nothing. An existing
   claim can still receive a missing link to genuinely new supplied evidence.

Notation: follow [[precis-notation-canon]] (UTF-8 units/exponents, no TeX outside quotes or derivations); if the canon does not fit what you are writing, file a gripe linked to skill:precis-notation-canon instead of improvising.

One call can ask several independent questions, preserving labelled results:

```python
search(kind='memory', page_size=5, args={'batch': [
    {'key': 'jane', 'q': 'Jane'},
    {'key': 'bill', 'q': 'Bill'},
]})
```

Apply the intended `SPACE:` tags to the outer search when known; a child's
tags add restrictions. A batch holds at most eight items, each with at most ten
hits. Results stay separate; inspect each `is_error`. Full child fields and
error behavior: [[precis-search-help]]. Dependent reads and writes follow their
inputs; do not batch a write before its subject has been resolved.

## Shared reconciliation policy

{{include doc:precis-memory-help#memory-reconciliation-policy}}

## Jane and Bill

"Jane's dog is red, Bill's dog is brown, and his cat is black" yields three
claims. "His" naturally refers to Bill; there is no claim about Jane's cat.
Search Jane and Bill independently, then establish which pets are meant.

- A likely Jane Doe hit shows its handle, identifying evidence and relevant
  body excerpt. A matching red-dog claim is a no-op. "Mentions a dog" alone
  does not establish the same animal or a colour claim.
- Weak Bill matches leave identity unresolved. Present candidates and a scoped
  provisional identity proposal if useful; do not attach pets to a weak hit or
  assume `human:bill` is a globally unique identity.
- If the resolved Bill's dog is recorded as black, expose the discrepancy and
  source. A correction or dated change can justify an edit; the new value alone
  does not. His cat's colour can proceed independently if its identity is clear.
- A named cat with several attributes or its own history can become a separate
  entity with linked atomic claims. One black-cat claim needs no empty pet stub.
  Preserve claim provenance and links when splitting an existing record.
