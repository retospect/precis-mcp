---
id: precis-claim-fidelity-help
title: precis — how precisely must draft prose restate the claim hub it cites?
summary: hub sentences are self-contained and heavily qualified by design; prose leans on its section and the cite popover — the floor is two prohibitions (scope stripping, modal inflation) plus verb strength matched to the hub's trust state, decided by the surprise test
answers:
  - how faithfully does my sentence have to restate the hub I'm citing?
  - can I drop a hub's scope qualification when the section already establishes it?
  - how strongly may I word a claim whose hub is only Ⓐ / ✍ / ⚠?
  - two held sources disagree — how do I write that without hedging everything?
  - the draft would be unreadable if every claim carried all its conditions — what's the rule?
applies-to: put/edit (kind='draft'), get (kind='finding', view='evidence')
status: active
tags: [drafting, workflow]
kinds: [draft, finding]
---

# precis-claim-fidelity-help — how precisely must the prose restate the hub?

Hub sentences are heavily qualified **by design** — a hub must stand
alone as a published assertion, carrying its own scope, conditions and
units, because it is read with no surrounding paragraph. Prose is the
opposite: the section, the method and the preceding sentences have
already established scope, so restating every qualification inline is not
rigor, it is noise that makes the claim *harder* to check. A draft that
inlines every condition reads like a terms-of-service document.

**Precision lives on the hub; readability lives in the prose; the cite is
the join.** The cite popover shows the grounding passage verbatim, so the
reader is always one hover from the full qualification. You do not have
to carry it in the sentence, and you must not paste the hub's sentence
into the draft.

## The floor — two things you may never do

- **Scope stripping** — dropping a qualification that is load-bearing
  *where the sentence now sits*. A hub scoped "in aqueous synthesis at
  pH 7–9", used in a section already about aqueous work: drop it, the
  context carries it. The same claim in a section comparing routes: the
  scope *is* the point, keep it. The test is contextual, not textual —
  the same drop is right in one paragraph and wrong in another.
- **Modal inflation** — the hub hedges and your sentence asserts. "Has
  been pursued across…" must not become "is established for…". This is
  the failure that corrupts the record silently, because no
  number-checking pass catches it: every figure in the sentence is right.

## The surprise test decides both

**Would a reader who believed your sentence be surprised by the hub's
sentence?**

- **Surprised** → the qualification was load-bearing. Put it back.
- **Not surprised** → the hover has it. Leave the prose readable.

Apply it per sentence in its actual position, not to the claim in the
abstract.

## Match verb strength to the hub's trust state

Read the hub before writing the sentence:

```python
get(id="fi42", view="evidence")  # originators / corroborators / disputes / trust
```

| hub state | how the sentence may speak |
|---|---|
| `clean` with corroborators | assert plainly |
| `clean`, single originator | attribute — "Smith and colleagues report…" |
| `abstract` (Ⓐ) / `vouched` (✍) / `unverified` (⚠) | hedge explicitly, or don't write it yet |
| live `disputes` edges | write both readings, each attributed |
| `unsupported` (‼) | do not write the claim at all |

Two held sources that genuinely disagree are a **result**, not a problem
to resolve by picking one — write both, attributed, each with its own hub
cite. That is not hedging; hedging is one claim stated weakly, and this
is two claims stated plainly.

Trust is worst-of over a block's cites and `unsupported` is never
softened by an override, so an inflated verb over a weak hub shows up as
a badge mismatch to every reader of the block.

## Anti-patterns

- Pasting the hub sentence verbatim into the prose "to be safe" — it
  reads as boilerplate and buries the point the paragraph is making.
- Hedging everything uniformly because *some* hubs are weak. Strength is
  per claim; a `clean` corroborated hub deserves a plain assertion.
- Dropping a condition because the sentence reads better, without asking
  the surprise question.
- Writing a claim off a hub you did not open. The trust state is not
  visible from the handle.

## See also

- [[precis-write-paper-help]] — prose craft: structure, diction, tells.
- [[precis-finding-help]] — trust states, badges, disputes vs contradicts.
- [[precis-citation-help]] — the four-step cite procedure.
- [[precis-review-citation-faithfulness]] — checking the other direction.
