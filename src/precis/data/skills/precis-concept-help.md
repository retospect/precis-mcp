---
id: precis-concept-help
family: memory
title: precis — concept kind (personal knowledge-graph nodes)
summary: capture, read and link a concept — one node per term in the learner's knowledge graph: a canonical name, a definition, aliases, a mastery field the anki cards feed, and typed edges (has-prerequisite / analogy-of / contrasts-with); most nodes are promoted from paper glossaries
answers:
  - how do I create or capture a concept (kind='concept') with a name and definition?
  - what does get(kind='concept') show — name, definition, aliases, mastery, state?
  - how do I link two concepts (prerequisite, analogy, contrast)?
  - where do concepts come from, and what is meta.cohorts?
  - how do I rename or reword a concept?
  - how does a concept's mastery or state change?
applies-to: get/search/put/delete/tag/link (kind='concept')
status: active
tags: [workflow]
kinds: [concept]
---

# precis-concept-help — the `concept` kind

A `concept` (`cn<id>`) is one node in the learner's personal knowledge
graph — a term or idea with a canonical name, a one-line definition and a
continuous `mastery` field the study loop maintains. Objectives are
concepts: link them `serves` to a quest ([[precis-quest-help]]). The
shared, agreed-on term taxonomy is the `taxon` kind
([[precis-taxon-help]]) — a taxon is a term the corpus curates, a concept
is a term *you* are learning.

## Capture a concept

```python
put(kind="concept", text="overpotential — the extra voltage beyond equilibrium a reaction needs")
put(kind="concept", text="activity — the thermodynamic effective concentration", dry_run=True)
```

`text` is `<name> — <definition>` (em-dash, en-dash or a newline; a
colon does not split). The name is canonical — `meta.name`, with a
lowercased whitespace-collapsed `meta.norm_name` as the dedup anchor —
and the definition is the searchable card: name + definition + aliases
embed as one text, so retrieval is definition-anchored, not
name-anchored. `dry_run` writes nothing and lists the nearest live
concepts to that card — read it before minting a near-duplicate.

A term the human keeps asking about can also arrive tagged
`twin:asked` — the card-forge pass mints asked concepts first
([[precis-anki-help]]).

## Read and search

```python
get(kind="concept", id="cn12")
search(kind="concept", q="the extra voltage a reaction needs")
get(kind="concept", id="cn12", extent="fisheye+1hop")
```

`get` renders name, definition, `aka:` aliases, `mastery` (0–1) and the
derived `state`. `search` matches the card text. The fisheye ladder
renders the typed edges as a `Concepts:` ring ([[precis-fisheye-help]]).

## meta keys a concept carries

| key | value |
|---|---|
| `name`, `norm_name` | the canonical name and its normalized dedup anchor |
| `definition` | the one-line gloss the card embeds |
| `aliases` | alternate names folded into the card text (`aka:` on get); present on richer promoted nodes |
| `mastery`, `mastery_updated_at` | the 0–1 field and its stamp — written by the mastery pass, never by `put` |
| `state` | derived: `candidate` → `active` → `mastered` (mastery ≥ 0.7) |
| `cohorts` | the reading cohorts the node serves — a list; one node serves several |
| `source_glossary_version` | provenance stamp on a glossary-promoted node |
| `asked_count` | how often the human asked "what is X?" for this term |

## Link concepts — the typed edges

```python
link(kind="concept", id="cn9", target="cn4", rel="has-prerequisite")  # learn cn4 before cn9
link(kind="concept", id="cn9", target="cn7", rel="contrasts-with")    # a confusable pair
link(kind="concept", id="cn9", target="cn8", rel="analogy-of")        # structurally analogous
link(kind="concept", id="cn9", target="quest:7", rel="serves")        # an objective
```

`has-prerequisite` is the learning DAG (`prerequisite-of` reads the
same edge from the other end); `analogy-of` and `contrasts-with` are
symmetric. `represents` / `represented-by` binds the concept to the
anki cards that render it — the mastery pass folds those cards' review
stats back into `mastery`. `derived-from` points at the paper a
promoted node was extracted from.

## Where concepts come from — glossary promotion

Most nodes arrive through the reading-prep promotion pass, not by hand:
a paper's glossary terms become concepts, deduped corpus-wide on
`norm_name` — one node per term, not one per paper. A name the corpus
already holds gains the new paper as a `derived-from` edge and joins
`meta.cohorts`; its definition and mastery stay untouched. Two senses
of one name therefore share a node today — disambiguate in the name
itself (`overpotential (electrode)` vs `overpotential (cell)`) when the
senses must stay separate.

## Change a concept — there is no edit door

Bodies are immutable and `meta.mastery` moves only through the mastery
pass. To reword a definition or fix a name, `delete` the node and
re-`put` it; carry the old name inside the new definition or aliases if
the old spelling should keep resolving — a rename preserves nothing on
its own. Retire a wrong node with `delete(kind='concept', id='cn…')`.

## See also

- [[precis-anki-help]] — the card forge: `represents` links, mastery refresh, leech rework
- [[precis-quest-help]] — a concept as an objective (`serves`)
- [[precis-taxon-help]] — the shared term taxonomy (the other term kind)
- [[precis-fisheye-help]] — the `Concepts:` link ring
- [[precis-search-help]] — search modes and `under=`
